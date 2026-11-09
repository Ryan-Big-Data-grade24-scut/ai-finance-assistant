from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend import db
from backend.ai import chat_service
from backend.ai.config import AIConfig
from backend.importer import import_rows
from backend.importer.base import Row
from backend.ledger import loader as ledger_loader
from backend.ledger import service as ledger_service


@pytest.fixture
def chat_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "chat-test.db")
    monkeypatch.setenv("AI_FINANCE_MASTER_KEY_FILE", str(tmp_path / "outside" / "master.key"))
    db.init_db(db.DB_PATH)
    yield tmp_path


def _seed():
    rows = [
        Row("alipay", "支付宝", "2026-10-01 12:00:00", "支出", 28.0,
            counterparty="STARBUCKS", item="拿铁咖啡", tx_type="其他", tx_no="chat-seed-001"),
        Row("alipay", "支付宝", "2026-10-02 12:00:00", "收入", 100.0,
            counterparty="工资发放", item="工资", tx_type="工资", tx_no="chat-seed-002"),
    ]
    import_rows(rows, "alipay", "chat-test.csv", batch_id="chat-seed")
    ledger_service.sync_live_book()


def test_chat_conversation_persists_messages_and_tools_are_called(monkeypatch, chat_db):
    _seed()
    config = AIConfig(
        "openai_compatible",
        "https://maas.qianwenaiapi.com/compatible-mode/v1",
        "qwen3.8-flash",
        "DUMMY_WEBSOCKET_API_KEY",
        False,
        0.90,
        True,
    )
    monkeypatch.setattr(chat_service, "get_secret_config", lambda: config)
    calls = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, timeout=90):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers, json):
            calls.append({"url": url, "headers": headers, "body": json})
            if len(calls) == 1:
                return FakeResponse({"choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "识别为月度收支分析，先读取本地统计。",
                        "tool_calls": [{
                            "id": "call_overview",
                            "type": "function",
                            "function": {"name": "get_financial_overview", "arguments": "{}"},
                        }],
                    }
                }]})
            return FakeResponse({"choices": [{
                "message": {
                    "role": "assistant",
                    "content": "最近一个月支出 28 元，收入 100 元，净额为 72 元。",
                }
            }]})

    monkeypatch.setattr(chat_service.httpx, "AsyncClient", FakeClient)

    conversation = chat_service.create_conversation()
    result = asyncio.run(chat_service.send_message(
        conversation["id"], "帮我分析最近一个月的收支。", {"page": "bill", "path": "/bill"}
    ))

    assert result["assistant_message"].startswith("最近一个月支出")
    assert len(calls) == 2
    assert calls[0]["body"]["enable_thinking"] is True
    assert calls[0]["body"]["preserve_thinking"] is True
    assert calls[0]["body"]["thinking_budget"] == 4096
    assert calls[0]["body"]["tools"]
    assert any(item["role"] == "tool" for item in calls[1]["body"]["messages"])
    assistant_tool_turn = next(
        item for item in calls[1]["body"]["messages"]
        if item.get("role") == "assistant" and item.get("tool_calls")
    )
    assert assistant_tool_turn["reasoning_content"] == "识别为月度收支分析，先读取本地统计。"

    detail = chat_service.get_conversation(conversation["id"])
    assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
    assert detail["conversation"]["title"].startswith("帮我分析最近一个月")


def test_chat_requires_explicit_external_processing_consent(monkeypatch, chat_db):
    conversation = chat_service.create_conversation()
    config = AIConfig(
        "openai_compatible", "https://example.test/v1", "test-model", "secret",
        False, 0.90, False,
    )
    monkeypatch.setattr(chat_service, "get_secret_config", lambda: config)

    with pytest.raises(chat_service.AIChatError, match="授权"):
        asyncio.run(chat_service.send_message(conversation["id"], "帮我看看账单"))

    detail = chat_service.get_conversation(conversation["id"])
    assert detail["messages"] == []


def test_chat_category_rule_requires_user_confirmation(chat_db):
    _seed()
    conversation = chat_service.create_conversation()
    action = chat_service._tool_propose_category_rule(
        {
            "match_field": "counterparty",
            "pattern": "STARBUCKS",
            "target_path": "其他",
            "reason": "用户希望把该商户统一归入该分类。",
        },
        conversation["id"],
    )
    assert action["status"] == "pending_confirmation"

    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM ai_classification_rules").fetchone()[0] == 0
    finally:
        conn.close()

    result = chat_service.confirm_action(conversation["id"], action["action_id"])
    assert result["status"] == "confirmed"
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT match_field,pattern,target_node_id FROM ai_classification_rules"
        ).fetchone()
        assert row["match_field"] == "counterparty"
        assert row["pattern"] == "STARBUCKS"
    finally:
        conn.close()


def test_chat_transaction_reclassification_uses_classification_service_and_ledger(chat_db):
    _seed()
    conversation = chat_service.create_conversation()
    action = chat_service._tool_propose_transaction_reclassification(
        {
            "tx_id": 1,
            "target_path": "工资",
            "reason": "专项测试：验证必须经过用户确认和 Ledger Engine。",
        },
        conversation["id"],
    )
    conn = db.get_conn()
    try:
        before = conn.execute("SELECT category FROM v_tx WHERE id=1").fetchone()["category"]
    finally:
        conn.close()
    assert before == "其他"

    result = chat_service.confirm_action(conversation["id"], action["action_id"])
    assert result["status"] == "confirmed"
    conn = db.get_conn()
    try:
        state = ledger_loader.read_state(conn, ledger_service.LIVE_BOOK_ID)
        node_id = state["placements"]["1"]
        node_names = {str(node["id"]): str(node["name"]) for node in state["nodes"]}
        review_count = conn.execute("SELECT COUNT(*) FROM ai_classification_reviews WHERE tx_id=1").fetchone()[0]
        assert node_names[node_id] == "工资"
        assert review_count == 1
    finally:
        conn.close()



def test_rejecting_chat_action_keeps_rules_unchanged(chat_db):
    _seed()
    conversation = chat_service.create_conversation()
    action = chat_service._tool_propose_category_rule(
        {
            "match_field": "counterparty",
            "pattern": "STARBUCKS",
            "target_path": "其他",
            "reason": "用户提出长期分类偏好。",
        },
        conversation["id"],
    )
    result = chat_service.reject_action(conversation["id"], action["action_id"])
    assert result["status"] == "rejected"
    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM ai_classification_rules").fetchone()[0] == 0
        assert conn.execute(
            "SELECT status FROM ai_chat_actions WHERE id=?", (action["action_id"],)
        ).fetchone()["status"] == "rejected"
    finally:
        conn.close()


def test_deleting_conversation_cascades_chat_messages(chat_db):
    conversation = chat_service.create_conversation()
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO ai_chat_messages(conversation_id,role,content) VALUES(?,?,?)",
            (conversation["id"], "user", "测试消息"),
        )
        conn.commit()
    finally:
        conn.close()

    result = chat_service.delete_conversation(conversation["id"])
    assert result["deleted"] is True
    conn = db.get_conn()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM ai_chat_messages WHERE conversation_id=?",
            (conversation["id"],),
        ).fetchone()[0] == 0
    finally:
        conn.close()
