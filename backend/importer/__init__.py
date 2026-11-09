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
from dataclasses import dataclass
from pathlib import Path

import openpyxl
from typing import Callable, Iterator

from .. import db
from . import alipay, wechat
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
}

# platform_id → (显示名, 类别, 适配器)
PLATFORM_META = {
    "wechat": ("微信支付", "wallet", "wechat"),
    "alipay": ("支付宝", "wallet", "alipay"),
    "manual": ("手动记账", "other", "manual"),
}


def ensure_platform(conn, platform_id: str) -> None:
    """导入时自动注册平台。

    不单独做 seed.sql —— 加一个新平台不应该需要改两处地方。
    """
    name, kind, adapter = PLATFORM_META.get(
        platform_id, (platform_id, "other", "generic_csv")
    )
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
            for row in ws.iter_rows(min_row=1, max_row=50, values_only=True):
                names = {str(value).strip() for value in row if value is not None}
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

    if "微信" in name or "wechat" in name:
        return "wechat", 0.98
    if "支付宝" in name or "alipay" in name:
        return "alipay", 0.98
    if suffix in {".csv", ".zip"}:
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
) -> ImportResult:
    """把已经解析并审核过的归一化交易全事务写入数据库。

    普通 CSV/XLSX 导入和 AI 导入会话共用此入口，避免出现两套写库逻辑。
    """
    if not rows:
        raise ValueError("没有可写入的有效交易行")
    issues = issues or ParseIssues()
    pid = platform_id
    name = PLATFORM_META.get(pid, (pid, "other", "generic_csv"))[0]
    batch_id = batch_id or uuid.uuid4().hex[:12]
    dates = [row.date for row in rows]
    inserted = skipped = 0

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
        with db.transaction(conn) as c:
            ensure_platform(c, pid)
            for i, row in enumerate(rows, 1):
                try:
                    cur = c.execute(INSERT_SQL, row.to_sql_tuple(batch_id))
                    if cur.rowcount == 1:
                        inserted += 1
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
