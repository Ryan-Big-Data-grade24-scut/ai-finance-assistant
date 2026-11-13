from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path

import pytest

from backend import db
from backend.ai import chat_service, history_workflow
from backend.importer import import_rows
from backend.importer.base import Row
from backend.ledger import service as ledger_service


@pytest.fixture
def v22_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "trustworthy-v22.db")
    monkeypatch.setenv("AI_FINANCE_MASTER_KEY_FILE", str(tmp_path / "outside" / "master.key"))
    db.init_db(db.DB_PATH)
    yield tmp_path


def _seed_day():
    rows = [
        Row("wechat", "微信支付", "2026-10-29 18:50:48", "支出", 8.0,
            counterparty="华南理工大学", item="校园消费", tx_type="餐饮", tx_no="v22-day-1"),
        Row("wechat", "微信支付", "2026-10-29 18:16:40", "支出", 15.0,
            counterparty="华南理工大学", item="校园消费", tx_type="餐饮", tx_no="v22-day-2"),
        Row("wechat", "微信支付", "2026-10-29 13:18:15", "支出", 200.0,
            counterparty="遇见小面", item="午餐", tx_type="餐饮", tx_no="v22-day-3"),
        Row("wechat", "微信支付", "2026-10-29 09:00:00", "收入", 50.0,
            counterparty="兼职平台", item="兼职收入", tx_type="收入", tx_no="v22-day-4"),
        Row("wechat", "微信支付", "2026-10-28 12:00:00", "支出", 999.0,
            counterparty="其他日期商户", item="不可混入", tx_type="其他", tx_no="v22-other-day"),
    ]
    import_rows(rows, "wechat", "v22-day.csv", batch_id="v22-day")
    ledger_service.sync_live_book()


def test_date_fact_question_is_answered_from_server_without_calling_model(monkeypatch, v22_db):
    _seed_day()
    monkeypatch.setattr(chat_service, "get_secret_config", lambda: None)

    async def forbidden_model_call(*args, **kwargs):
        raise AssertionError("deterministic date facts must not be sent to the model")

    monkeypatch.setattr(chat_service, "_run_openai_chat", forbidden_model_call)
    conversation = chat_service.create_conversation()

    result = asyncio.run(chat_service.send_message(
        conversation["id"],
        "能给我10月29日的支出流水和分类吗？",
        {"page": "transactions", "path": "/transactions", "date": "2026-10-29", "type": "expense"},
    ))

    answer = result["assistant_message"]
    assert "2026-10-29 流水核验" in answer
    assert "3 笔" in answer
    assert "¥223.00" in answer
    assert "华南理工大学" in answer
    assert "遇见小面" in answer
    assert "兼职平台" not in answer
    assert "其他日期商户" not in answer
    assert "¥999.00" not in answer
    assert "¥50.00" not in answer
    assert "本地后端查询及计算生成" in answer
    assert [item["role"] for item in result["messages"]] == ["user", "assistant"]


def test_explicit_all_directions_overrides_page_expense_filter(v22_db):
    _seed_day()
    snapshot = chat_service._tool_transactions_by_date(
        {"date": "2026-10-29"},
        {"page": "transactions", "date": "2026-10-29", "type": ""},
    )
    assert snapshot["count"] == 4
    assert snapshot["raw_total_amount"] == pytest.approx(273.0)


def test_resolved_server_date_cannot_be_overridden_by_model_tool_arguments(v22_db):
    _seed_day()
    snapshot = chat_service._tool_transactions_by_date(
        {"date": "2026-10-28", "direction": "收入"},
        {"page": "transactions", "date": "2026-10-28", "type": "income",
         "_resolved_date": "2026-10-29", "_resolved_direction": "支出"},
    )
    assert snapshot["date"] == "2026-10-29"
    assert snapshot["count"] == 3
    assert snapshot["raw_total_amount"] == pytest.approx(223.0)


def test_empty_selected_date_is_reported_as_zero_not_recent_transactions(monkeypatch, v22_db):
    _seed_day()
    snapshot = chat_service._tool_transactions_by_date(
        {"date": "2026-10-27"},
        {"page": "transactions", "date": "2026-10-27", "type": "expense"},
    )
    assert snapshot["count"] == 0
    answer = chat_service._format_verified_date_answer(snapshot)
    assert "0 笔" in answer
    assert "其他日期或历史对话中的交易替代" in answer
    assert "遇见小面" not in answer


def test_missing_ledger_projection_is_not_reported_as_zero(v22_db, monkeypatch):
    _seed_day()
    monkeypatch.setattr(
        chat_service.ledger_service,
        "get_live_tree",
        lambda **kwargs: {"transactions": [], "nodes": []},
    )
    snapshot = chat_service._tool_transactions_by_date(
        {"date": "2026-10-29"},
        {"page": "transactions", "date": "2026-10-29", "type": "expense"},
    )
    assert snapshot["count"] == 3
    assert snapshot["ledger_complete"] is False
    assert snapshot["effective_expense_total"] is None
    assert len(snapshot["ledger_missing_tx_ids"]) == 3
    answer = chat_service._format_verified_date_answer(snapshot)
    assert "账本核验未完成" in answer
    assert "Ledger有效支出：**¥0.00**" not in answer


def test_date_parser_prefers_explicit_user_date_over_selected_page_date():
    resolved = chat_service._resolve_date_for_message(
        "帮我看看9月3号的收支明细",
        {"page": "transactions", "date": "2026-10-29"},
    )
    assert resolved == "2026-09-03"


def test_date_parser_does_not_turn_month_or_relative_queries_into_selected_day():
    context = {"page": "transactions", "date": "2026-10-29"}
    assert chat_service._resolve_date_for_message("这个月支出多少", context) is None
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert chat_service._resolve_date_for_message("昨天花了多少", context) == yesterday
    assert yesterday != context["date"]


def test_history_suggestions_attach_only_real_user_confirmed_history(v22_db):
    _seed_day()
    conn = db.get_conn()
    try:
        # Turn one real transaction into an explicitly user-confirmed classification.
        tx = conn.execute(
            "SELECT id FROM transactions_raw WHERE tx_no='v22-day-1'"
        ).fetchone()
        assert tx is not None
        tx_id = int(tx["id"])
        conn.execute(
            "INSERT INTO tx_overrides(tx_id,manual_category,manual_subcat,updated_by) "
            "VALUES(?,?,?,'ui')",
            (tx_id, "餐饮", "食堂"),
        )
        conn.commit()
    finally:
        conn.close()

    inputs = history_workflow._load_history_inputs()
    from backend.importer.base import Row as ImportRow

    exact = ImportRow(
        "wechat", "微信支付", "2026-10-30 10:00:00", "支出", 9.0,
        counterparty="华南理工大学", item="校园消费", tx_type="其他",
    )
    evidence = history_workflow._history_evidence_for_row(exact, inputs["history_evidence"])
    assert evidence
    assert evidence[0]["tx_id"] == tx_id
    assert evidence[0]["match_basis"] == "相同商户、商品描述及方向"

    unknown = ImportRow(
        "alipay", "支付宝", "2026-10-30 10:00:00", "支出", 9.0,
        counterparty="完全陌生商户", item="未知商品", tx_type="其他",
    )
    assert history_workflow._history_evidence_for_row(unknown, inputs["history_evidence"]) == []


def test_history_status_reports_total_manual_overrides_and_pending_subset(v22_db):
    _seed_day()
    conn = db.get_conn()
    try:
        tx_id = int(conn.execute(
            "SELECT id FROM transactions_raw WHERE tx_no='v22-day-1'"
        ).fetchone()["id"])
        conn.execute(
            "INSERT INTO tx_overrides(tx_id,manual_category,manual_subcat,updated_by) "
            "VALUES(?,?,?,'ui')",
            (tx_id, "餐饮", "食堂"),
        )
        conn.commit()
    finally:
        conn.close()

    status = history_workflow.get_status()
    assert status["protected_manual"] == 1
    assert status["protected_manual_pending"] == 1
    assert status["pending_transactions"] == 4
