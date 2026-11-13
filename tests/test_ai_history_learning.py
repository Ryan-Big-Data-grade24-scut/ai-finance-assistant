from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend import db
from backend.ai import history_workflow
from backend.ai.config import AIConfig
from backend.importer import import_rows
from backend.importer.base import Row
from backend.ledger import loader as ledger_loader
from backend.ledger import service as ledger_service


@pytest.fixture
def isolated_history_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "history-learning.db")
    monkeypatch.setenv("AI_FINANCE_MASTER_KEY_FILE", str(tmp_path / "outside" / "master.key"))
    db.init_db(db.DB_PATH)
    yield tmp_path


def _seed_history() -> list[int]:
    rows = [
        Row("alipay", "支付宝", "2026-09-01 10:00:00", "支出", 28.0,
            counterparty="STARBUCKS", item="拿铁咖啡", tx_type="其他", tx_no="history-001"),
        Row("alipay", "支付宝", "2026-09-02 11:00:00", "支出", 46.0,
            counterparty="STARBUCKS", item="咖啡豆", tx_type="其他", tx_no="history-002"),
    ]
    import_rows(rows, "alipay", "history.csv", batch_id="history-seed")
    ledger_service.sync_live_book()
    conn = db.get_conn()
    try:
        return [int(row[0]) for row in conn.execute(
            "SELECT id FROM transactions_raw ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


async def _fake_analysis(rows, *, config, existing_categories, rules, history_examples=None):
    return {
        "summary": "历史交易以咖啡消费为主，建议统一归入餐饮 / 咖啡饮品。",
        "taxonomy": [{
            "major": "餐饮", "minor": "咖啡饮品", "path": "餐饮 / 咖啡饮品",
            "description": "咖啡、茶饮和其他饮品", "is_new": True,
        }],
        "suggestions": [
            {
                "row_index": index, "target_major": "餐饮", "target_minor": "咖啡饮品",
                "target_path": "餐饮 / 咖啡饮品", "confidence": 0.97,
                "reason": "商户和商品说明均指向咖啡消费。", "needs_review": True,
                "question": None, "excluded": False, "is_new_category": True,
                "auto_apply_eligible": False, "matched_rule_ids": [],
                "counterparty": row.counterparty, "item": row.item, "amount": row.amount,
                "refunded_amount": 0.0, "effective_amount": row.amount, "direction": row.direction,
            }
            for index, row in enumerate(rows)
        ],
        "counts": {"total": len(rows), "excluded": 0, "auto_apply": 0,
                   "review": len(rows), "new_categories": 1},
    }


def test_history_analysis_is_preview_only_then_commit_uses_ledger(monkeypatch, isolated_history_db):
    tx_ids = _seed_history()
    config = AIConfig(
        "openai_compatible", "https://api.example.test/v1", "mock-model", "secret",
        auto_apply_rules=True, confidence_threshold=0.90, allow_external_processing=True,
    )
    monkeypatch.setattr(history_workflow, "get_secret_config", lambda: config)
    monkeypatch.setattr(history_workflow, "classify_import_batch", _fake_analysis)

    started = history_workflow.start_analysis()
    assert started["status"] == "queued"
    asyncio.run(history_workflow.run_analysis(started["session_id"]))
    session = history_workflow.get_session(started["session_id"])
    assert session["status"] == "preview"
    assert session["analysis"]["counts"]["suggested"] == 2
    assert {item["tx_id"] for item in session["analysis"]["suggestions"]} == set(tx_ids)

    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM ai_classification_reviews").fetchone()[0] == 0
    finally:
        conn.close()

    result = history_workflow.commit_session(
        started["session_id"],
        approved_category_paths=["餐饮 / 咖啡饮品"],
        approved_tx_ids=tx_ids,
        remember_rule_tx_ids=[tx_ids[0]],
    )
    assert result["status"] == "applied"
    assert result["applied_count"] == 2
    assert result["remembered_rules"] == 1

    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM ai_classification_reviews").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM ai_classification_rules WHERE enabled=1").fetchone()[0] == 1
        state = ledger_loader.read_state(conn, ledger_service.LIVE_BOOK_ID)
        nodes = {str(node["id"]): node for node in state["nodes"]}
        assigned = [nodes[state["placements"][str(tx_id)]] for tx_id in tx_ids]
        assert all(node["name"] == "咖啡饮品" for node in assigned)
        # AI-confirmed Ledger categories must also be visible to report queries.
        report_row = conn.execute(
            "SELECT category,subcat,is_overridden FROM v_tx WHERE id=?",
            (tx_ids[0],),
        ).fetchone()
        assert report_row["category"] == "餐饮"
        assert report_row["subcat"] == "咖啡饮品"
        assert report_row["is_overridden"] == 1
    finally:
        conn.close()


def test_history_analysis_does_not_silently_apply_high_confidence(monkeypatch, isolated_history_db):
    _seed_history()
    config = AIConfig(
        "openai_compatible", "https://api.example.test/v1", "mock-model", "secret",
        auto_apply_rules=True, confidence_threshold=0.90, allow_external_processing=True,
    )
    monkeypatch.setattr(history_workflow, "get_secret_config", lambda: config)
    monkeypatch.setattr(history_workflow, "classify_import_batch", _fake_analysis)
    started = history_workflow.start_analysis()
    asyncio.run(history_workflow.run_analysis(started["session_id"]))

    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM ai_classification_reviews").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ai_history_learning_sessions WHERE status='preview'").fetchone()[0] == 1
    finally:
        conn.close()


def test_history_analysis_protects_manual_override_rows(monkeypatch, isolated_history_db):
    tx_ids = _seed_history()
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO tx_overrides(tx_id,manual_category,manual_subcat,updated_by) VALUES(?,?,?,?)",
            (tx_ids[0], "个人偏好分类", "手动确认", "ui"),
        )
        conn.commit()
    finally:
        conn.close()

    config = AIConfig(
        "openai_compatible", "https://api.example.test/v1", "mock-model", "secret",
        auto_apply_rules=False, confidence_threshold=0.90, allow_external_processing=True,
    )
    monkeypatch.setattr(history_workflow, "get_secret_config", lambda: config)
    monkeypatch.setattr(history_workflow, "classify_import_batch", _fake_analysis)
    started = history_workflow.start_analysis()
    asyncio.run(history_workflow.run_analysis(started["session_id"]))
    session = history_workflow.get_session(started["session_id"])
    assert session["analysis"]["protected_manual"] == 1
    assert [item["tx_id"] for item in session["analysis"]["suggestions"]] == [tx_ids[1]]
