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


def detect(path: Path, alipay_password: str | None = None) -> tuple[str, float]:
    """识别平台。返回 (platform_id, 置信度)。

    第一轮按文件名判，第二轮按扩展名兜底。
    """
    name = path.name
    if "微信" in name:
        return "wechat", 0.98
    if "支付宝" in name:
        return "alipay", 0.98
    if path.suffix.lower() == ".xlsx":
        return "wechat", 0.60
    return "alipay", 0.60


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


def import_file(path: Path, platform_id: str | None = None,
                alipay_password: str | None = None) -> ImportResult:
    """全事务导入。任何一行失败 → 整体回滚，库保持导入前状态。"""
    path = Path(path)
    pid, conf = detect(path, alipay_password)
    if platform_id:
        pid = platform_id

    issues = ParseIssues()
    try:
        rows = parse_file(path, pid, alipay_password, issues=issues)
    except Exception as e:
        raise ValueError(f"解析失败：{e}") from e

    if not rows:
        raise ValueError("解析成功但没有任何有效数据行")

    batch_id = uuid.uuid4().hex[:12]
    name = {
        "wechat": "微信支付",
        "alipay": "支付宝",
    }.get(pid, pid)

    dates = [r.date for r in rows]
    inserted = skipped = 0

    conn = db.get_conn()
    try:
        with db.transaction(conn) as c:
            ensure_platform(c, pid)

            for i, r in enumerate(rows, 1):
                try:
                    cur = c.execute(INSERT_SQL, r.to_sql_tuple(batch_id))
                    if cur.rowcount == 1:
                        inserted += 1
                    else:
                        skipped += 1
                except Exception as e:
                    raise ValueError(
                        f"第 {i} 行写入失败：{e}。已整体回滚，未写入任何数据"
                    ) from e

            c.execute(
                "INSERT INTO import_batches "
                "(batch_id, platform_id, filename, total, inserted, skipped,"
                " failed, date_from, date_to) VALUES (?,?,?,?,?,?,?,?,?)",
                (batch_id, pid, path.name, len(rows) + issues.failed, inserted, skipped,
                 issues.failed, min(dates), max(dates)),
            )
    except Exception:
        conn.close()
        raise
    conn.close()

    return ImportResult(
        batch_id=batch_id, platform_id=pid, platform_name=name,
        total=len(rows) + issues.failed, inserted=inserted, skipped=skipped,
        failed=issues.failed, first_error=issues.first_error,
        date_from=min(dates), date_to=max(dates),
    )


def list_batches() -> list[dict]:
    """导入批次记录（最近的在前）。

    放在这里而不是 main.py —— **main.py 不含任何 SQL**（第一轮 DNA）。
    """
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT b.*, p.name AS platform_name FROM import_batches b "
            "LEFT JOIN platforms p ON p.id = b.platform_id "
            "ORDER BY b.created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
