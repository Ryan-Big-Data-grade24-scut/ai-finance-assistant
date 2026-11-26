"""
backend/importer/alipay_wallet.py —— 支付宝小荷包余额收支明细 (.xlsx) 解析

小荷包账单和普通支付宝账单是两份不同的导出，按【独立平台】接入，不共享去重键、
不做跨平台冲销。

真实文件结构（已实测确认）：
    第 1~11 行   『支付宝小荷包名称』『支付宝小荷包账户ID：…』『创建时间』
                 账单周期、特别提示等元信息
    第 12 行     ★ 表头：订单号 / 交易时间 / 交易说明 / 备注 /
                 操作人昵称 / 操作人姓名 / 收入金额 / 支出金额
    第 13 行起   数据；没有尾注

两个必须处理的现实问题：
    1. 工作簿把工作表尺寸错报成 A1:A1，read_only 模式下不修正就只读到第 1 行
       → 扫描前调用 ws.reset_dimensions()，按表头而非固定行号识别
    2. 金额拆成『收入金额』『支出金额』两列，非空一侧才是方向；不能靠正负号猜
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import openpyxl

from .base import ParseIssues, Row, clean, norm_amount, norm_time

# 表头关键词 → 统一字段名
HEADER_MAP = {
    "订单号": "tx_no",
    "交易时间": "tx_time",
    "交易说明": "item",
    "备注": "remark",
    "收入金额": "income",
    "支出金额": "expense",
}

REQUIRED_COLUMNS = {"tx_no", "tx_time", "income", "expense"}

# 账户 ID 只用于平台内去重分域，不写入任何字段；这里只留它的摘要。
_ACCOUNT_ID_RE = re.compile(r"账户\s*ID\s*[：:]\s*([A-Za-z0-9]+)")


def _cell_text(value) -> str:
    """把单元格转换成可安全写入 raw_json 的字符串。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _reset_dimensions(ws) -> None:
    """修正错报的工作表尺寸（真实文件声明为 A1:A1）。

    read_only 的工作表按声明的尺寸迭代，尺寸错就只能读到首行。
    重置后按实际 XML 行数迭代，仅影响本次解析。
    """
    reset = getattr(ws, "reset_dimensions", None)
    if reset is not None:
        reset()


def _header_map(row) -> dict[str, int] | None:
    names = {clean(value): i for i, value in enumerate(row) if clean(value)}
    if not {"订单号", "收入金额", "支出金额"}.issubset(names):
        return None
    col: dict[str, int] = {}
    for name, index in names.items():
        key = HEADER_MAP.get(name)
        if key and key not in col:
            col[key] = index
    return col if REQUIRED_COLUMNS.issubset(col) else None


def _find_header(ws, max_scan: int = 50) -> tuple[int, list[str]]:
    """按列名定位表头，不依赖文件名或固定行号。"""
    _reset_dimensions(ws)
    for row_index, row in enumerate(
        ws.iter_rows(min_row=1, max_row=max_scan, values_only=True), start=1
    ):
        if _header_map(row):
            return row_index, [clean(value) for value in row]
    raise ValueError(
        "未找到支付宝小荷包 Excel 表头：需要包含订单号、交易时间、收入金额和支出金额。"
    )


def _wallet_domain(ws, header_row: int) -> str:
    """从表头上方的元信息里取账户标识，只返回摘要，绝不保存原文。"""
    for row in ws.iter_rows(min_row=1, max_row=header_row - 1, values_only=True):
        for value in row:
            text = _cell_text(value)
            if not text:
                continue
            match = _ACCOUNT_ID_RE.search(text)
            if match:
                digest = hashlib.sha256(match.group(1).encode("utf-8")).hexdigest()
                return digest[:8]
    return ""


def parse(path: Path, issues: ParseIssues | None = None) -> Iterator[Row]:
    """解析小荷包 XLSX，产出归一化 Row。"""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        selected = None
        header_row = None
        header = None
        col = None
        last_error = None
        for ws in wb.worksheets:
            try:
                found_row, found_header = _find_header(ws)
                col = _header_map(found_header)
            except ValueError as exc:
                last_error = exc
                continue
            selected, header_row, header = ws, found_row, found_header
            break
        if selected is None or header_row is None or header is None or col is None:
            raise ValueError(str(last_error or "小荷包 Excel 中没有可识别的交易明细工作表。"))

        domain = _wallet_domain(selected, header_row)

        def value_at(raw_row, key: str):
            idx = col.get(key)
            return raw_row[idx] if idx is not None and idx < len(raw_row) else None

        # 表头之后就是数据；有列名缺列时 REQUIRED_COLUMNS 已在表头阶段拒绝。
        for line_number, raw_row in enumerate(
            selected.iter_rows(min_row=header_row + 1, values_only=True),
            start=header_row + 1,
        ):
            if not raw_row or not any(value is not None for value in raw_row):
                continue

            tx_no = clean(value_at(raw_row, "tx_no"))
            if not tx_no:
                if issues is not None:
                    issues.add(line_number, "缺少订单号")
                continue

            tx_time = norm_time(value_at(raw_row, "tx_time"))
            if tx_time is None:
                if issues is not None:
                    issues.add(line_number, "交易时间无效")
                continue

            income = norm_amount(value_at(raw_row, "income"))
            expense = norm_amount(value_at(raw_row, "expense"))
            if income is not None and expense is not None:
                if issues is not None:
                    issues.add(line_number, "收入金额和支出金额同时为正，无法判断方向")
                continue
            if income is None and expense is None:
                if issues is not None:
                    issues.add(line_number, "金额无效或不大于零")
                continue

            item = clean(value_at(raw_row, "item"))
            yield Row(
                platform_id="alipay_wallet",
                source="支付宝小荷包",
                tx_time=tx_time,
                direction="收入" if income is not None else "支出",
                amount=income if income is not None else expense,
                item=item,
                tx_type=item,
                tx_no=tx_no,
                remark=clean(value_at(raw_row, "remark")),
                # 原始两列金额都留底，便于复核这一笔的方向判断。
                raw={
                    header[i]: _cell_text(raw_row[i] if i < len(raw_row) else None)
                    for i in range(len(header))
                    if header[i]
                },
                wallet_domain=domain,
            )
    finally:
        wb.close()
