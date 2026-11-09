from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import db
from backend.ai import chat_service, history_workflow, import_classifier, test_batch_cleanup
from backend.ai.config import AIConfig
from backend.importer import import_rows
from backend.importer.base import Row
from backend.ledger import service as ledger_service


@pytest.fixture
def isolated_v21_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "trustworthy-v21.db")
    monkeypatch.setenv("AI_FINANCE_MASTER_KEY_FILE", str(tmp_path / "outside" / "master.key"))
    db.init_db(db.DB_PATH)
    yield tmp_path


def test_date_tool_uses_selected_date_and_direction(isolated_v21_db):
    rows = [
        Row("alipay", "支付宝", "2026-09-03 10:00:00", "收入", 120.0,
            counterparty="工资", item="工资到账", tx_type="工资", tx_no="date-income"),
        Row("alipay", "支付宝", "2026-09-04 10:00:00", "支出", 30.0,
            counterparty="咖啡店", item="咖啡", tx_type="餐饮", tx_no="date-expense"),
    ]
    import_rows(rows, "alipay", "date-context.csv", batch_id="date-context")
    ledger_service.sync_live_book()

    empty_expense = chat_service._tool_transactions_by_date(
        {}, {"page": "transactions", "path": "/transactions", "date": "2026-09-03", "type": "expense"}
    )
    assert empty_expense["count"] == 0
    assert empty_expense["effective_total_amount"] == 0.0
    assert empty_expense["transactions"] == []

    income = chat_service._tool_transactions_by_date(
        {}, {"page": "transactions", "date": "2026-09-03", "type": "income"}
    )
    assert income["count"] == 1
    assert income["effective_income_total"] == 120.0
    assert income["transactions"][0]["counterparty"] == "工资"


def test_date_tool_rejects_missing_or_invalid_date(isolated_v21_db):
    with pytest.raises(chat_service.AIChatError, match="明确日期"):
        chat_service._tool_transactions_by_date({}, {"page": "transactions"})
    with pytest.raises(chat_service.AIChatError, match="有效的日历日期"):
        chat_service._tool_transactions_by_date({"date": "2026-02-30"}, {})


def test_history_analysis_filter_hides_tombstoned_suggestions(isolated_v21_db):
    rows = [
        Row("alipay", "支付宝", "2026-09-01 10:00:00", "支出", 10.0,
            counterparty="真实商户", item="真实消费", tx_type="其他", tx_no="visible-one"),
        Row("alipay", "支付宝", "2026-09-02 10:00:00", "支出", 20.0,
            counterparty="示例商户", item="虚构测试", tx_type="其他", tx_no="hidden-two"),
    ]
    imported = import_rows(rows, "alipay", "history-filter.csv", batch_id="history-filter")
    assert imported.inserted == 2
    ledger_service.sync_live_book()

    conn = db.get_conn()
    try:
        ids = [int(row["id"]) for row in conn.execute(
            "SELECT id FROM transactions_raw ORDER BY id"
        )]
        conn.execute(
            "INSERT INTO tx_deletions(tx_id,deleted_by,reason) VALUES(?,?,?)",
            (ids[1], "test", "synthetic row"),
        )
        conn.commit()
        analysis = {
            "suggestions": [
                {"tx_id": ids[0], "target_path": "餐饮 / 咖啡", "confidence": 0.95, "excluded": False},
                {"tx_id": ids[1], "target_path": "其他 / 未细分", "confidence": 0.99, "excluded": False},
            ],
            "counts": {"total": 2, "suggested": 2, "excluded": 0, "needs_review": 0, "high_confidence": 2},
        }
        filtered = history_workflow._filter_analysis_to_visible(conn, analysis)
        assert [item["tx_id"] for item in filtered["suggestions"]] == [ids[0]]
        assert filtered["counts"]["total"] == 1
        assert filtered["counts"]["suggested"] == 1
        assert filtered["hidden_deleted_transactions"] == 1
    finally:
        conn.close()


def test_cleanup_uses_tombstones_and_keeps_raw_rows(isolated_v21_db):
    rows = [
        Row("alipay", "支付宝", "2026-09-01 10:00:00", "支出", 10.0,
            counterparty="示例餐馆", item="虚构午餐", tx_type="餐饮", tx_no="synthetic-1"),
        Row("alipay", "支付宝", "2026-09-02 10:00:00", "支出", 20.0,
            counterparty="示例便利店", item="虚构日用品", tx_type="其他", tx_no="synthetic-2"),
    ]
    imported = import_rows(rows, "alipay", test_batch_cleanup.DEFAULT_FILENAME, batch_id="synthetic-batch")
    assert imported.inserted == 2
    ledger_service.sync_live_book()

    conn = db.get_conn()
    try:
        ids = [int(row["id"]) for row in conn.execute(
            "SELECT id FROM transactions_raw ORDER BY id"
        )]
    finally:
        conn.close()

    result = test_batch_cleanup.apply_cleanup(
        "synthetic-batch",
        expected_filename=test_batch_cleanup.DEFAULT_FILENAME,
        expected_ids=ids,
        expected_count=2,
    )
    assert result["remaining_visible"] == 0
    assert result["remaining_live_book_members"] == 0
    assert result["raw_total_before"] == result["raw_total_after"] == 2
    assert Path(result["backup_path"]).exists()

    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM tx_deletions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM v_tx").fetchone()[0] == 0
    finally:
        conn.close()


def test_cleanup_refuses_wrong_ids_or_filename(isolated_v21_db):
    rows = [
        Row("alipay", "支付宝", "2026-09-01 10:00:00", "支出", 10.0,
            counterparty="示例餐馆", item="虚构午餐", tx_type="餐饮", tx_no="synthetic-safe"),
    ]
    import_rows(rows, "alipay", test_batch_cleanup.DEFAULT_FILENAME, batch_id="synthetic-safe")
    with pytest.raises(ValueError, match="ID 不匹配"):
        test_batch_cleanup.inspect_batch(
            "synthetic-safe",
            expected_filename=test_batch_cleanup.DEFAULT_FILENAME,
            expected_ids=[999],
            expected_count=1,
        )


def test_pending_history_tool_returns_only_visible_unconfirmed_rows(isolated_v21_db):
    rows = [
        Row("alipay", "支付宝", "2026-09-01 10:00:00", "支出", 10.0,
            counterparty="真实商户A", item="商品A", tx_type="其他", tx_no="pending-a"),
        Row("alipay", "支付宝", "2026-09-02 10:00:00", "支出", 20.0,
            counterparty="示例商户", item="虚构测试", tx_type="其他", tx_no="pending-b"),
    ]
    import_rows(rows, "alipay", "pending.csv", batch_id="pending-seed")
    ledger_service.sync_live_book()
    conn = db.get_conn()
    try:
        state = __import__("backend.ledger.loader", fromlist=["read_state"]).read_state(
            conn, ledger_service.LIVE_BOOK_ID
        )
        ids = [int(row["id"]) for row in conn.execute(
            "SELECT id FROM transactions_raw ORDER BY id"
        )]
        analysis = {
            "suggestions": [
                {"tx_id": ids[0], "target_path": "餐饮 / 其他", "confidence": 0.91,
                 "reason": "AI 建议", "needs_review": True, "excluded": False},
                {"tx_id": ids[1], "target_path": "其他 / 未细分", "confidence": 0.99,
                 "reason": "测试数据", "needs_review": True, "excluded": False},
            ],
            "counts": {"total": 2, "suggested": 2, "excluded": 0, "needs_review": 2},
        }
        conn.execute(
            "INSERT INTO ai_history_learning_sessions"
            "(id,book_id,status,base_seq,total_count,processed_count,analysis_json,applied_tx_ids_json) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("pending-session", ledger_service.LIVE_BOOK_ID, "preview",
             0, 2, 2, json.dumps(analysis, ensure_ascii=False), "[]"),
        )
        conn.execute(
            "INSERT INTO tx_deletions(tx_id,deleted_by,reason) VALUES(?,?,?)",
            (ids[1], "test", "synthetic test row"),
        )
        conn.commit()
    finally:
        conn.close()

    result = chat_service._tool_pending_history_classifications({"limit": 14})
    assert result["pending_count"] == 1
    assert [item["tx_id"] for item in result["items"]] == [ids[0]]
    assert result["items"][0]["merchant"] == "真实商户A"


def test_generic_other_fallback_requires_user_clarification(monkeypatch, isolated_v21_db):
    async def fake_complete_json(config, *, system_prompt, user_payload):
        if "batch_summary" in user_payload:
            return {
                "categories": [{
                    "major": "其他", "minor": "未细分",
                    "description": "兜底", "reuse_existing": True,
                }],
                "summary": "无法确定用途。",
            }
        group = user_payload["groups"][0]
        return {
            "mappings": [{
                "group_key": group["group_key"],
                "major": "其他", "minor": "未细分",
                "confidence": 0.99, "reason": "信息不足。",
                "needs_review": False, "question": None,
            }]
        }

    monkeypatch.setattr(import_classifier, "complete_json", fake_complete_json)
    config = AIConfig(
        "openai_compatible", "https://example.test/v1", "mock-model", "secret",
        False, 0.90, True,
    )
    row = Row("alipay", "支付宝", "2026-09-01 10:00:00", "支出", 12.0,
              counterparty="未知商户", item="未说明", tx_type="其他", tx_no="generic-fallback")
    result = __import__("asyncio").run(import_classifier.classify_import_batch(
        [row],
        config=config,
        existing_categories=[{"node_id": "node-other", "path": "其他 / 未细分"}],
        rules=[],
    ))
    suggestion = result["suggestions"][0]
    assert suggestion["target_path"] == "其他 / 未细分"
    assert suggestion["needs_review"] is True
    assert suggestion["auto_apply_eligible"] is False
    assert suggestion["question"] == "这笔交易的具体用途是什么？"


def test_date_tool_does_not_count_internal_transfer_as_spending(isolated_v21_db):
    rows = [
        Row("alipay", "支付宝", "2026-09-09 09:00:00", "支出", 500.0,
            counterparty="本人银行卡", item="本人账户互转", tx_type="转账",
            tx_no="transfer-out", merchant_no="same-own-transfer"),
        Row("alipay", "支付宝", "2026-09-09 09:00:00", "收入", 500.0,
            counterparty="本人支付宝", item="本人账户互转", tx_type="转账",
            tx_no="transfer-in", merchant_no="same-own-transfer"),
    ]
    import_rows(rows, "alipay", "transfer-date.csv", batch_id="transfer-date")
    ledger_service.sync_live_book()

    result = chat_service._tool_transactions_by_date(
        {}, {"page": "transactions", "date": "2026-09-09", "type": "expense"}
    )
    assert result["count"] == 1
    assert result["raw_total_amount"] == 500.0
    assert result["effective_expense_total"] == 0.0
    assert result["transactions"][0]["internal_transfer_zeroed"] is True


def test_safe_page_context_keeps_selected_date_but_rejects_transaction_payload(isolated_v21_db):
    safe = chat_service._safe_context({
        "page": "transactions",
        "path": "/transactions",
        "date": "2026-09-03",
        "type": "expense",
        "category": "餐饮",
        "amount": 999999,
        "counterparty": "should-not-be-trusted",
    })
    assert safe == {
        "page": "transactions",
        "path": "/transactions",
        "type": "expense",
        "date": "2026-09-03",
        "category": "餐饮",
    }


def test_selected_date_prefetch_only_triggers_for_date_finance_questions(isolated_v21_db):
    context = {"page": "transactions", "date": "2026-09-03", "type": "expense"}
    assert chat_service._should_prefetch_selected_date("帮我看看这一天的收支", context)
    assert chat_service._should_prefetch_selected_date("9月3号的交易明细", context)
    assert not chat_service._should_prefetch_selected_date("介绍一下你能做什么", context)
    assert not chat_service._should_prefetch_selected_date(
        "帮我分析本月整体情况", {"page": "bill", "date": "2026-09-03"}
    )
