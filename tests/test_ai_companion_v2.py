from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backend import db
from backend.ai import chat_service
from backend.importer import import_rows
from backend.importer.base import Row
from backend.ledger import loader as ledger_loader
from backend.ledger import service as ledger_service


@pytest.fixture
def v2_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "companion-v2.db")
    monkeypatch.setenv("AI_FINANCE_MASTER_KEY_FILE", str(tmp_path / "outside" / "master.key"))
    db.init_db(db.DB_PATH)
    yield tmp_path


def _seed_basic():
    rows = [
        Row("alipay", "支付宝", "2026-10-01 12:00:00", "支出", 28.0,
            counterparty="STARBUCKS", item="拿铁咖啡", tx_type="其他", tx_no="v2-seed-001"),
        Row("alipay", "支付宝", "2026-10-02 12:00:00", "收入", 100.0,
            counterparty="工资发放", item="工资", tx_type="工资", tx_no="v2-seed-002"),
    ]
    import_rows(rows, "alipay", "v2-seed.csv", batch_id="v2-seed")
    ledger_service.sync_live_book()


def test_spending_trends_returns_month_comparison_and_category_delta(v2_db):
    rows = [
        Row("alipay", "支付宝", "2026-09-01 10:00:00", "支出", 100.0,
            counterparty="商户A", item="消费", tx_type="餐饮", tx_no="trend-sep-1"),
        Row("alipay", "支付宝", "2026-10-01 10:00:00", "支出", 180.0,
            counterparty="商户A", item="消费", tx_type="餐饮", tx_no="trend-oct-1"),
    ]
    import_rows(rows, "alipay", "trend.csv", batch_id="trend-seed")
    result = chat_service._tool_spending_trends({"month": "2026-10", "direction": "支出"})
    assert result["current"]["amount"] == 180.0
    assert result["previous"]["amount"] == 100.0
    assert result["change"] == 80.0
    assert result["change_percent"] == 80.0
    assert result["category_changes"][0]["category"] == "餐饮"


def test_anomaly_detector_flags_large_transaction_and_possible_duplicate(v2_db):
    rows = []
    for index, amount in enumerate([20, 22, 21, 19, 23, 20], start=1):
        day = index
        rows.append(Row("alipay", "支付宝", f"2026-09-{day:02d} 10:00:00", "支出", float(amount),
                        counterparty=f"餐饮店{index}", item="午餐", tx_type="餐饮", tx_no=f"anom-h-{index}"))
    rows.extend([
        Row("alipay", "支付宝", "2026-10-01 12:00:00", "支出", 260.0,
            counterparty="餐饮店A", item="聚餐", tx_type="餐饮", tx_no="anom-large"),
        Row("alipay", "支付宝", "2026-10-02 12:00:00", "支出", 38.0,
            counterparty="便利店", item="零食", tx_type="零食", tx_no="anom-dup-1"),
        Row("alipay", "支付宝", "2026-10-02 12:06:00", "支出", 38.0,
            counterparty="便利店", item="零食", tx_type="零食", tx_no="anom-dup-2"),
    ])
    import_rows(rows, "alipay", "anomaly.csv", batch_id="anomaly-seed")
    result = chat_service._tool_detect_spending_anomalies({"month": "2026-10"})
    assert any(item["tx_id"] for item in result["large_spending_candidates"])
    assert len(result["possible_duplicate_candidates"]) == 1
    assert result["possible_duplicate_candidates"][0]["tx_ids"]
    assert "不代表欺诈" in result["caution"]


def test_bulk_reclassification_requires_confirmation_and_uses_ledger(v2_db):
    _seed_basic()
    conversation = chat_service.create_conversation()
    action = chat_service._tool_propose_bulk_reclassification({
        "match_field": "counterparty", "match_value": "STARBUCKS",
        "target_path": "工资", "reason": "专项测试批量分类。",
    }, conversation["id"])
    assert action["status"] == "pending_confirmation"
    conn = db.get_conn()
    try:
        before = conn.execute("SELECT category FROM v_tx WHERE id=1").fetchone()["category"]
        assert before == "其他"
        assert conn.execute(
            "SELECT status FROM ai_chat_actions WHERE id=?", (action["action_id"],)
        ).fetchone()["status"] == "pending"
    finally:
        conn.close()

    result = chat_service.confirm_action(conversation["id"], action["action_id"])
    assert result["status"] == "confirmed"
    conn = db.get_conn()
    try:
        state = ledger_loader.read_state(conn, ledger_service.LIVE_BOOK_ID)
        node_id = state["placements"]["1"]
        nodes = {str(node["id"]): node for node in state["nodes"]}
        assert nodes[node_id]["name"] == "工资"
        assert conn.execute(
            "SELECT status FROM ai_chat_actions WHERE id=?", (action["action_id"],)
        ).fetchone()["status"] == "confirmed"
    finally:
        conn.close()


def test_manual_transaction_is_only_inserted_after_user_confirmation(v2_db):
    _seed_basic()
    conversation = chat_service.create_conversation()
    action = chat_service._tool_propose_manual_transaction({
        "date": "2026-10-03", "direction": "支出", "amount": 36.5,
        "counterparty": "咖啡店", "item": "拿铁", "target_path": "其他",
        "reason": "用户要求补记咖啡消费。",
    }, conversation["id"])
    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == 2
    finally:
        conn.close()

    result = chat_service.confirm_action(conversation["id"], action["action_id"])
    assert result["status"] == "confirmed"
    assert result["result"]["imported"] == 1
    conn = db.get_conn()
    try:
        tx = conn.execute(
            "SELECT id,source,direction,amount,counterparty FROM v_tx WHERE source='手动记账'"
        ).fetchone()
        assert tx is not None
        assert tx["direction"] == "支出"
        assert tx["amount"] == pytest.approx(36.5)
        assert tx["counterparty"] == "咖啡店"
        assert conn.execute("SELECT COUNT(*) FROM ai_classification_reviews WHERE tx_id=?", (tx["id"],)).fetchone()[0] == 1
    finally:
        conn.close()


def test_legacy_chat_action_table_is_migrated_without_losing_rows(v2_db):
    conversation = chat_service.create_conversation()
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO ai_chat_actions(id,conversation_id,action_type,status,payload_json) "
            "VALUES('legacy-action',?,'create_category_rule','pending','{}')",
            (conversation["id"],),
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ALTER TABLE ai_chat_actions RENAME TO ai_chat_actions_v2_temp")
        conn.execute(
            """CREATE TABLE ai_chat_actions (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES ai_chat_conversations(id) ON DELETE CASCADE,
                action_type TEXT NOT NULL CHECK (action_type IN ('create_category_rule','reclassify_transaction')),
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','rejected')),
                payload_json TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                decided_at TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO ai_chat_actions SELECT * FROM ai_chat_actions_v2_temp"
        )
        conn.execute("DROP TABLE ai_chat_actions_v2_temp")
        conn.execute(
            "CREATE INDEX idx_ai_chat_actions_conversation ON ai_chat_actions(conversation_id,status,created_at)"
        )
        conn.commit()
    finally:
        conn.close()

    db.init_db(db.DB_PATH)
    conn = db.get_conn()
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_chat_actions'"
        ).fetchone()[0]
        assert "CHECK (action_type IN" not in sql
        action = conn.execute("SELECT action_type,status FROM ai_chat_actions WHERE id='legacy-action'").fetchone()
        assert action["action_type"] == "create_category_rule"
        assert action["status"] == "pending"
    finally:
        conn.close()
