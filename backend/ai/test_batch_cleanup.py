"""Safe tombstone cleanup for accidentally imported synthetic test batches.

This never deletes rows from transactions_raw. It creates tx_deletions tombstones,
disables only rules directly sourced from suggestions for this exact batch, and
resynchronizes the live Ledger book.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import db
from ..ledger import service as ledger_service

DEFAULT_BATCH_ID = "005458eba57f"
DEFAULT_FILENAME = "支付宝格式_引擎验收_虚构数据.csv"
DEFAULT_EXPECTED_IDS = list(range(269, 282))
DEFAULT_EXPECTED_COUNT = 13
CLEANUP_REASON = (
    "误导入引擎验收虚构数据批次 005458eba57f；"
    "保留原始凭证，仅通过删除墓碑从可见账本和 AI 学习中排除。"
)


def inspect_batch(
    batch_id: str = DEFAULT_BATCH_ID,
    *,
    expected_filename: str = DEFAULT_FILENAME,
    expected_ids: list[int] | None = None,
    expected_count: int = DEFAULT_EXPECTED_COUNT,
) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        batch = conn.execute(
            "SELECT batch_id,filename,inserted,total,skipped,failed "
            "FROM import_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if not batch:
            raise ValueError(f"找不到导入批次：{batch_id}")
        if str(batch["filename"] or "") != expected_filename:
            raise ValueError(
                f"批次文件名不匹配：实际={batch['filename']!r}，预期={expected_filename!r}"
            )
        rows = [
            dict(row) for row in conn.execute(
                "SELECT id,tx_no,counterparty,item,import_batch "
                "FROM transactions_raw WHERE import_batch=? ORDER BY id",
                (batch_id,),
            )
        ]
        ids = [int(row["id"]) for row in rows]
        if len(rows) != expected_count:
            raise ValueError(f"批次实际原始交易数为 {len(rows)}，预期 {expected_count}；停止清理。")
        if int(batch["inserted"]) != expected_count:
            raise ValueError(
                f"批次 inserted={batch['inserted']}，预期 {expected_count}；停止清理。"
            )
        if expected_ids is not None and ids != [int(value) for value in expected_ids]:
            raise ValueError(
                f"批次交易 ID 不匹配：实际={ids}，预期={expected_ids}；停止清理。"
            )

        tombstoned = {
            int(row["tx_id"]) for row in conn.execute(
                "SELECT tx_id FROM tx_deletions WHERE tx_id IN "
                f"({','.join('?' for _ in ids)})",
                ids,
            )
        } if ids else set()

        proposal_ids = {
            str(row["proposal_id"]) for row in conn.execute(
                "SELECT DISTINCT proposal_id FROM ai_classification_suggestions "
                "WHERE tx_id IN (" + ",".join("?" for _ in ids) + ")",
                ids,
            )
        } if ids else set()

        linked_rules: list[dict[str, Any]] = []
        if proposal_ids:
            placeholders = ",".join("?" for _ in proposal_ids)
            linked_rules = [
                dict(row) for row in conn.execute(
                    "SELECT id,pattern,pattern_norm,enabled,source_proposal_id "
                    "FROM ai_classification_rules WHERE source_proposal_id IN (" + placeholders + ")",
                    tuple(sorted(proposal_ids)),
                )
            ]

        manual_rules = [
            dict(row) for row in conn.execute(
                "SELECT id,pattern,pattern_norm,enabled,source_proposal_id "
                "FROM ai_classification_rules "
                "WHERE source_proposal_id IS NULL AND "
                "(pattern_norm LIKE '%示例%' OR pattern_norm LIKE '%虚构%' OR pattern_norm LIKE '%测试%')"
            )
        ]

        visible = {
            int(row["id"]) for row in conn.execute(
                "SELECT id FROM v_tx WHERE id IN (" + ",".join("?" for _ in ids) + ")",
                ids,
            )
        } if ids else set()
        live_members = {
            int(row["tx_id"]) for row in conn.execute(
                "SELECT tx_id FROM ledger_book_tx WHERE book_id=? AND tx_id IN (" +
                ",".join("?" for _ in ids) + ")",
                (ledger_service.LIVE_BOOK_ID, *ids),
            )
        } if ids else set()

        return {
            "batch": dict(batch),
            "rows": rows,
            "ids": ids,
            "already_tombstoned": sorted(tombstoned),
            "visible_ids": sorted(visible),
            "live_book_ids": sorted(live_members),
            "proposal_ids": sorted(proposal_ids),
            "linked_rules": linked_rules,
            "manual_suspect_rules": manual_rules,
            "raw_total": int(conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0]),
        }
    finally:
        conn.close()


def _backup_database() -> Path:
    source_path = Path(db.DB_PATH)
    if not source_path.exists():
        raise FileNotFoundError(f"数据库不存在：{source_path}")
    backup_dir = Path(db.DB_DIR) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"bills_before_fake_batch_cleanup_{stamp}.sqlite3"
    source = db.get_conn(source_path)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    return backup_path


def apply_cleanup(
    batch_id: str = DEFAULT_BATCH_ID,
    *,
    expected_filename: str = DEFAULT_FILENAME,
    expected_ids: list[int] | None = None,
    expected_count: int = DEFAULT_EXPECTED_COUNT,
) -> dict[str, Any]:
    preview = inspect_batch(
        batch_id,
        expected_filename=expected_filename,
        expected_ids=expected_ids,
        expected_count=expected_count,
    )
    backup_path = _backup_database()
    ids = preview["ids"]
    placeholders = ",".join("?" for _ in ids)

    conn = db.get_conn()
    disabled_rules = 0
    inserted_tombstones = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Recheck batch identity inside the write transaction.
        batch = conn.execute(
            "SELECT filename,inserted FROM import_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if not batch or batch["filename"] != expected_filename or int(batch["inserted"]) != expected_count:
            raise RuntimeError("批次在预览后发生变化；已取消写入。")
        current_ids = [
            int(row["id"]) for row in conn.execute(
                "SELECT id FROM transactions_raw WHERE import_batch=? ORDER BY id",
                (batch_id,),
            )
        ]
        if current_ids != ids:
            raise RuntimeError("批次交易集合在预览后发生变化；已取消写入。")

        cursor = conn.executemany(
            "INSERT OR IGNORE INTO tx_deletions(tx_id,deleted_by,reason) VALUES(?,?,?)",
            [(tx_id, "test_batch_cleanup", CLEANUP_REASON) for tx_id in ids],
        )
        inserted_tombstones = int(cursor.rowcount or 0)

        proposal_ids = [
            str(row["proposal_id"]) for row in conn.execute(
                "SELECT DISTINCT proposal_id FROM ai_classification_suggestions "
                f"WHERE tx_id IN ({placeholders})",
                ids,
            )
        ]
        if proposal_ids:
            proposal_placeholders = ",".join("?" for _ in proposal_ids)
            cursor = conn.execute(
                "UPDATE ai_classification_rules SET enabled=0,updated_at=datetime('now') "
                f"WHERE enabled=1 AND source_proposal_id IN ({proposal_placeholders})",
                proposal_ids,
            )
            disabled_rules = int(cursor.rowcount or 0)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # v_tx now hides the tombstoned rows; sync removes them from all live-book snapshots.
    sync_result = ledger_service.sync_live_book()

    conn = db.get_conn()
    try:
        remaining_visible = int(conn.execute(
            f"SELECT COUNT(*) FROM v_tx WHERE id IN ({placeholders})", ids
        ).fetchone()[0])
        remaining_live = int(conn.execute(
            "SELECT COUNT(*) FROM ledger_book_tx WHERE book_id=? AND tx_id IN (" +
            placeholders + ")",
            (ledger_service.LIVE_BOOK_ID, *ids),
        ).fetchone()[0])
        raw_total_after = int(conn.execute(
            "SELECT COUNT(*) FROM transactions_raw"
        ).fetchone()[0])
        tombstone_total = int(conn.execute(
            f"SELECT COUNT(*) FROM tx_deletions WHERE tx_id IN ({placeholders})", ids
        ).fetchone()[0])
    finally:
        conn.close()

    if remaining_visible or remaining_live or tombstone_total != expected_count:
        raise RuntimeError(
            "墓碑写入后验收未通过。请保留备份并联系开发者检查；"
            f"visible={remaining_visible}, live={remaining_live}, tombstones={tombstone_total}"
        )
    if raw_total_after != preview["raw_total"]:
        raise RuntimeError("原始流水总数发生变化，违反不可变约束。")

    return {
        "batch_id": batch_id,
        "ids": ids,
        "backup_path": str(backup_path),
        "inserted_tombstones": inserted_tombstones,
        "disabled_linked_rules": disabled_rules,
        "remaining_visible": remaining_visible,
        "remaining_live_book_members": remaining_live,
        "tombstone_count": tombstone_total,
        "raw_total_before": preview["raw_total"],
        "raw_total_after": raw_total_after,
        "sync": sync_result,
        "manual_suspect_rules_need_review": preview["manual_suspect_rules"],
    }
