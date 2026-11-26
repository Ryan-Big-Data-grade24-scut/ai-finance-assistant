"""
backend/importer/__init__.py —— 导入编排

职责：
    1. 识别平台（文件名 + 表头特征）
    2. 解析成归一化 Row
    3. 【全事务】写入，失败整体回滚
    4. 返回 inserted / skipped / failed

⚠️ skipped（重复笔数）必须返回给用户。
   兜底 dedupe_key 有误判风险，静默吞掉就是数据丢失。
"""
from __future__ import annotations

import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import openpyxl
from typing import Callable, Iterator

from .. import db
from . import alipay, alipay_wallet, statements, wechat
from .base import INSERT_SQL, ParseIssues, Row


@dataclass
class ImportResult:
    batch_id: str
    platform_id: str
    platform_name: str
    total: int
    inserted: int
    skipped: int
    failed: int
    first_error: str | None
    date_from: str
    date_to: str


ADAPTERS: dict[str, Callable[[Path], Iterator[Row]]] = {
    "wechat": wechat.parse,
    "alipay": alipay.parse,
    "alipay_wallet": alipay_wallet.parse,
    # 银行卡 / 电商 / 投资：同一套解析逻辑，按平台 ID 绑定各自的 platform_id。
    "bank_cmb_savings": lambda p, issues=None: statements.parse_bank_debit(
        p, "bank_cmb_savings", issues),
    "bank_cmb_credit": lambda p, issues=None: statements.parse_bank_credit(
        p, "bank_cmb_credit", issues),
    "bank_icbc": lambda p, issues=None: statements.parse_bank_debit(
        p, "bank_icbc", issues),
    "bank_ccb": lambda p, issues=None: statements.parse_bank_debit(
        p, "bank_ccb", issues),
    "bank_cgb_credit": lambda p, issues=None: statements.parse_bank_credit(
        p, "bank_cgb_credit", issues),
    "bank_debit": lambda p, issues=None: statements.parse_bank_debit(
        p, "bank_debit", issues),
    "bank_credit": lambda p, issues=None: statements.parse_bank_credit(
        p, "bank_credit", issues),
    "meituan": lambda p, issues=None: statements.parse_ecommerce(p, "meituan", issues),
    "taobao": lambda p, issues=None: statements.parse_ecommerce(p, "taobao", issues),
    "jd": lambda p, issues=None: statements.parse_ecommerce(p, "jd", issues),
    "ecommerce": lambda p, issues=None: statements.parse_ecommerce(p, "ecommerce", issues),
    "fund": lambda p, issues=None: statements.parse_fund(p, "fund", issues),
    "broker": lambda p, issues=None: statements.parse_broker(p, "broker", issues),
    "yield": lambda p, issues=None: statements.parse_yield(p, "yield", issues),
    "asset_snapshot": lambda p, issues=None: statements.parse_snapshot(p, "asset_snapshot", issues),
}

# platform_id → (显示名, 类别, 适配器)
PLATFORM_META = {
    "wechat": ("微信支付", "wallet", "wechat"),
    "alipay": ("支付宝", "wallet", "alipay"),
    # 小荷包是独立导出（两列金额、订单号空间独立），按独立平台接入。
    "alipay_wallet": ("支付宝小荷包", "wallet", "alipay_wallet"),
    "bank_cmb_savings": ("招商银行储蓄卡", "bank", "bank_debit"),
    "bank_cmb_credit": ("招商银行信用卡", "bank", "bank_credit"),
    "bank_icbc": ("工商银行", "bank", "bank_debit"),
    "bank_ccb": ("建设银行", "bank", "bank_debit"),
    "bank_cgb_credit": ("广发银行信用卡", "bank", "bank_credit"),
    "bank_debit": ("银行卡", "bank", "bank_debit"),
    "bank_credit": ("信用卡账单", "bank", "bank_credit"),
    "meituan": ("美团", "ecommerce", "ecommerce"),
    "taobao": ("淘宝天猫", "ecommerce", "ecommerce"),
    "jd": ("京东", "ecommerce", "ecommerce"),
    "ecommerce": ("电商订单", "ecommerce", "ecommerce"),
    "fund": ("基金交易", "investment", "fund"),
    "broker": ("证券成交", "investment", "broker"),
    "yield": ("理财收益", "investment", "yield"),
    "asset_snapshot": ("资产快照", "investment", "asset_snapshot"),
    "manual": ("手动记账", "other", "manual"),
}


def ensure_platform(conn, platform_id: str, source: str | None = None) -> None:
    """导入时自动注册平台。

    不单独做 seed.sql —— 加一个新平台不应该需要改两处地方。

    platforms.name 是 UNIQUE：同名但不同 ID 的平台如果直接 INSERT OR IGNORE，
    第二行会被静默跳过，随后 transactions_raw 的外键找不到平台行而整批回滚。
    所以这里先看名字是否已被别的 ID 占用，占用了就给新 ID 加后缀。
    """
    name, kind, adapter = PLATFORM_META.get(
        platform_id, (source or platform_id, "other", "generic_csv")
    )
    owner = conn.execute(
        "SELECT id FROM platforms WHERE name=? AND id<>?", (name, platform_id)
    ).fetchone()
    if owner is not None:
        name = f"{name}（{platform_id}）"
    conn.execute(
        "INSERT OR IGNORE INTO platforms (id, name, kind, adapter) "
        "VALUES (?,?,?,?)",
        (platform_id, name, kind, adapter),
    )


def _detect_xlsx_by_content(path: Path) -> str | None:
    """按工作簿表头识别平台，避免英文文件名的支付宝 Excel 被误判成微信。"""
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception:
        return None
    try:
        for ws in wb.worksheets:
            # 小荷包工作簿会把尺寸错报成 A1:A1；不重置就只迭代到第 1 行。
            if hasattr(ws, "reset_dimensions"):
                ws.reset_dimensions()
            for row in ws.iter_rows(min_row=1, max_row=50, values_only=True):
                names = {str(value).strip() for value in row if value is not None}
                # 小荷包：两列金额 + 订单号，没有 收/支 列。
                if {"订单号", "收入金额", "支出金额"}.issubset(names):
                    return "alipay_wallet"
                has_direction = "收/支" in names
                has_amount = bool(names.intersection({"金额（元）", "金额(元)", "金额"}))
                if not (has_direction and has_amount):
                    continue
                if "交易号" in names and names.intersection({"交易创建时间", "交易时间", "付款时间"}):
                    return "alipay"
                if "交易时间" in names and "交易类型" in names:
                    return "wechat"
    finally:
        wb.close()
    return None


def detect(path: Path, alipay_password: str | None = None) -> tuple[str, float]:
    """识别平台。优先识别 XLSX 内容，其次识别文件名和格式。

    返回 (platform_id, 置信度)。文件名只作为回退，不能覆盖明确的表头证据。
    """
    path = Path(path)
    name = path.name.casefold()
    suffix = path.suffix.lower()

    if suffix == ".xlsx":
        detected = _detect_xlsx_by_content(path)
        if detected:
            return detected, 0.99

    if suffix == ".csv":
        # 银行卡 / 电商 / 投资账单：先看表头证据，再退回文件名。
        detected = statements.detect_csv_platform(path)
        if detected:
            return detected, 0.95
        if "微信" in name or "wechat" in name:
            return "wechat", 0.90
        if "小荷包" in name:
            return "alipay_wallet", 0.90
        if "支付宝" in name or "alipay" in name:
            return "alipay", 0.90
        if "美团" in name:
            return "meituan", 0.90
        if "淘宝" in name or "天猫" in name:
            return "taobao", 0.90
        if "京东" in name:
            return "jd", 0.90
        if "基金" in name:
            return "fund", 0.90
        if "证券" in name or "成交" in name:
            return "broker", 0.90
        if "收益" in name:
            return "yield", 0.90
        # 认不出来的 CSV 保持旧行为：按支付宝主账单试，解析失败会明确报错。
        return "alipay", 0.60

    if "微信" in name or "wechat" in name:
        return "wechat", 0.98
    if "小荷包" in name:
        return "alipay_wallet", 0.98
    if "支付宝" in name or "alipay" in name:
        return "alipay", 0.98
    if suffix == ".zip":
        return "alipay", 0.60

    raise ValueError(
        f"无法识别账单平台：{path.name}。请检查文件内容，或在导入时明确指定 platform_id。"
    )


def parse_file(path: Path, platform_id: str | None = None,
               alipay_password: str | None = None,
               issues: ParseIssues | None = None) -> list[Row]:
    """解析文件为 Row 列表。不写库。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在：{path}")

    pid = platform_id
    if pid is None:
        pid, _ = detect(path)

    parser = ADAPTERS.get(pid)
    if parser is None:
        raise ValueError(f"未支持的平台：{pid}")

    if pid == "alipay":
        return list(alipay.parse(path, password=alipay_password, issues=issues))

    return list(parser(path, issues=issues))


def import_rows(
    rows: list[Row],
    platform_id: str,
    filename: str,
    issues: ParseIssues | None = None,
    batch_id: str | None = None,
    *, conn=None,
) -> ImportResult:
    """写入归一化交易。默认自管事务，也可参加调用者已有的写事务。"""
    if conn is not None and not conn.in_transaction:
        raise ValueError("传入的连接必须已开始事务")
    if not rows:
        raise ValueError("没有可写入的有效交易行")
    issues = issues or ParseIssues()
    pid = platform_id
    name = PLATFORM_META.get(pid, (rows[0].source, "other", "generic_csv"))[0]
    batch_id = batch_id or uuid.uuid4().hex[:12]
    dates = [row.date for row in rows]
    inserted = skipped = 0

    own = conn is None
    if own:
        conn = db.get_conn()
    try:
        existing_batch = conn.execute(
            "SELECT * FROM import_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if existing_batch:
            row = dict(existing_batch)
            return ImportResult(
                batch_id=batch_id, platform_id=row["platform_id"], platform_name=name,
                total=int(row["total"]), inserted=int(row["inserted"]),
                skipped=int(row["skipped"]), failed=int(row["failed"]),
                first_error=issues.first_error, date_from=row["date_from"], date_to=row["date_to"],
            )
        with (db.transaction(conn) if own else nullcontext(conn)) as c:
            ensure_platform(c, pid, name)
            # 跨导入器的去重：同一张银行账单可能被专用解析器和通用解析器各读一次，
            # 两边的 source 不同、dedupe_key 也就不同，于是同一笔流水被插入两次。
            # 平台内的可靠交易标识（单号 + 金额 + 方向）能跨来源对上。
            # 只对银行平台启用：钱包类平台（如支付宝小荷包）多个账户共用订单号空间，
            # 靠 wallet_domain 分域，跨账户撞号是正常业务，不能按单号去重。
            # 没有单号的行仍然只按 dedupe_key 去重，避免误吞真实的两笔。
            cross_source = pid.startswith('bank')
            seen_tx: set[tuple[str, float, str]] = set()
            if cross_source and any(row.tx_no for row in rows):
                for existing in c.execute(
                    "SELECT tx_no, amount, direction FROM transactions_raw "
                    "WHERE platform_id=? AND tx_no<>''", (pid,)
                ):
                    seen_tx.add((existing[0], float(existing[1]), existing[2]))
            for i, row in enumerate(rows, 1):
                tx_key = (row.tx_no, float(row.amount), row.direction) if cross_source and row.tx_no else None
                if tx_key is not None and tx_key in seen_tx:
                    skipped += 1
                    continue
                try:
                    cur = c.execute(INSERT_SQL, row.to_sql_tuple(batch_id))
                    if cur.rowcount == 1:
                        inserted += 1
                        if tx_key is not None:
                            seen_tx.add(tx_key)
                    else:
                        skipped += 1
                except Exception as exc:
                    raise ValueError(
                        f"第 {i} 行写入失败：{exc}。已整体回滚，未写入任何数据"
                    ) from exc
            c.execute(
                "INSERT INTO import_batches "
                "(batch_id, platform_id, filename, total, inserted, skipped,"
                " failed, date_from, date_to) VALUES (?,?,?,?,?,?,?,?,?)",
                (batch_id, pid, filename, len(rows) + issues.failed, inserted, skipped,
                 issues.failed, min(dates), max(dates)),
            )
    finally:
        if own:
            conn.close()

    return ImportResult(
        batch_id=batch_id, platform_id=pid, platform_name=name,
        total=len(rows) + issues.failed, inserted=inserted, skipped=skipped,
        failed=issues.failed, first_error=issues.first_error,
        date_from=min(dates), date_to=max(dates),
    )


def import_file(path: Path, platform_id: str | None = None,
                alipay_password: str | None = None) -> ImportResult:
    """全事务导入。任何一行失败 → 整体回滚，库保持导入前状态。"""
    path = Path(path)
    pid, _confidence = detect(path, alipay_password)
    if platform_id:
        pid = platform_id
    issues = ParseIssues()
    try:
        rows = parse_file(path, pid, alipay_password, issues=issues)
    except Exception as exc:
        raise ValueError(f"解析失败：{exc}") from exc
    return import_rows(rows, pid, path.name, issues)
