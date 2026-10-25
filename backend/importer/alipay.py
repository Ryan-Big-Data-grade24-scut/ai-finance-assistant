"""
backend/importer/alipay.py —— 支付宝账单 (.zip / .csv) 解析

两个必须处理的现实问题：
    1. 支付宝导出的 zip 【带密码】，密码是用户自定义的（常见为身份证后 6 位）
       → 需要密码才能解压，这是绕不过去的
    2. CSV 编码可能是 GBK / GB18030 / UTF-8，需要逐个尝试

CSV 结构（有 20+ 行的元信息头 + 真正的表头行）：
    前 N 行  '支付宝交易记录明细查询' / 账号 / 时间范围 / 导出说明 ...
    第 N 行  收/支, 交易时间, 交易分类, 交易对方, 对方账号, 商品说明,
              收/付款方式, 交易状态, 交易订单号, 商家订单号, 备注
"""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from typing import Iterator

from .base import ParseIssues, Row, clean, norm_amount, norm_direction, norm_time, should_drop

# 支付宝列名变体 → 统一字段名
HEADER_MAP = {
    "收/支": "direction",
    "交易时间": "tx_time",
    "交易创建时间": "tx_time",
    "付款时间": "tx_time",
    "交易分类": "tx_type",
    "类型": "tx_type",
    "交易对方": "counterparty",
    "对方账号": "counterparty_account",
    "商品说明": "item",
    "商品名称": "item",
    "商品": "item",
    "金额": "amount",
    "金额(元)": "amount",
    "金额（元）": "amount",
    "收/付款方式": "pay_method",
    "收付款方式": "pay_method",
    "付款方式": "pay_method",
    "交易状态": "status",
    "交易订单号": "tx_no",
    "交易号": "tx_no",
    "商家订单号": "merchant_no",
    "商户订单号": "merchant_no",
    "备注": "remark",
}

ENCODINGS = ("utf-8-sig", "gbk", "gb18030", "utf-8")

# 支付宝 CSV 在交易表后会追加统计尾注。这些内容不是坏账单，不能计入
# ParseIssues；遇到分隔线或已知汇总项即说明交易表已经结束。
FOOTER_PREFIXES = (
    "共", "已收入:", "待收入:", "已支出:", "待支出:", "导出时间:",
)


def decode_csv(data: bytes) -> str:
    """按优先级尝试解码。支付宝导出多为 GBK。"""
    for enc in ENCODINGS:
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法解码 CSV，已尝试：{ENCODINGS}")


def find_header_row(lines: list[str], max_scan: int = 40) -> int:
    """搜索表头行：同时含 '收/支' 和 '交易时间'。"""
    for i, ln in enumerate(lines[:max_scan]):
        if "收/支" in ln and ("交易时间" in ln or "交易创建时间" in ln):
            return i
    raise ValueError("未找到支付宝表头行")


def is_footer_row(raw_row: list[str]) -> bool:
    """识别支付宝交易表后的分隔线、汇总和导出信息。"""
    if not raw_row:
        return False
    first = clean(raw_row[0])
    if not first:
        return False
    compact = first.replace(" ", "")
    if len(compact) >= 8 and set(compact) <= {"-", "=", "_"}:
        return True
    return any(compact.startswith(prefix) for prefix in FOOTER_PREFIXES)


def parse(path: Path, password: str | None = None,
          issues: ParseIssues | None = None) -> Iterator[Row]:
    """解析支付宝 .zip 或 .csv。"""
    path = Path(path)

    # ── 1. 取 CSV 字节内容 ──────────────────────────────────
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as zf:
                entries = [info for info in zf.infolist()
                           if not info.is_dir() and info.filename.lower().endswith(".csv")]
                if not entries:
                    raise ValueError("支付宝 ZIP 中没有 CSV 文件")
                info = entries[0]
                if info.file_size > 30 * 1024 * 1024:
                    raise ValueError("ZIP 中 CSV 文件解压后超过 30 MB 限制")
                encrypted = bool(info.flag_bits & 0x1)
                if encrypted and not password:
                    raise ValueError("支付宝 ZIP 已加密，请填写解压密码")
                try:
                    data = zf.read(info, pwd=password.encode("utf-8") if password else None)
                except (RuntimeError, NotImplementedError) as exc:
                    if encrypted:
                        raise ValueError("支付宝 ZIP 密码错误或加密方式不受支持") from exc
                    raise ValueError("ZIP 解压失败，压缩方式可能不受支持") from exc
                except zipfile.BadZipFile as exc:
                    if encrypted:
                        raise ValueError("支付宝 ZIP 密码错误或压缩包损坏") from exc
                    raise ValueError("ZIP 内的 CSV 文件已损坏") from exc
        except zipfile.BadZipFile as exc:
            raise ValueError("ZIP 文件已损坏或不是有效的 ZIP 格式") from exc
    else:
        data = path.read_bytes()

    text = decode_csv(data)
    lines = text.splitlines()
    hdr = find_header_row(lines)

    reader = csv.reader(io.StringIO("\n".join(lines[hdr:])))
    header = [clean(h) for h in next(reader)]
    col: dict[str, int] = {}
    for i, name in enumerate(header):
        key = HEADER_MAP.get(name)
        if key and key not in col:
            col[key] = i

    missing = {"tx_time", "direction", "amount"} - set(col)
    if missing:
        raise ValueError(f"支付宝账单缺少必需列 {missing}，实际表头：{header}")

    for raw_row in reader:
        if not raw_row or not clean(raw_row[0]):
            continue
        if is_footer_row(raw_row):
            break

        def get(key: str):
            idx = col.get(key)
            return raw_row[idx] if idx is not None and idx < len(raw_row) else None

        status = clean(get("status"))
        if should_drop(status):
            continue

        tx_time = norm_time(get("tx_time"))
        amount = norm_amount(get("amount"))
        if tx_time is None or amount is None:
            if issues is not None:
                reason = "交易时间无效" if tx_time is None else "金额无效或不大于零"
                issues.add(hdr + reader.line_num, reason)
            continue

        yield Row(
            platform_id="alipay",
            source="支付宝",
            tx_time=tx_time,
            direction=norm_direction(get("direction")),
            amount=amount,
            pay_method=clean(get("pay_method")),
            counterparty=clean(get("counterparty")),
            item=clean(get("item")),
            tx_type=clean(get("tx_type")),
            status=status,
            tx_no=clean(get("tx_no")),
            merchant_no=clean(get("merchant_no")),
            remark=clean(get("remark")),
            raw={
                header[i]: (raw_row[i] if i < len(raw_row) else "")
                for i in range(len(header))
            },
        )
