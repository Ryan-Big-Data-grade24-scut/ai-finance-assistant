from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend import db
from backend.ai import chat_service
from backend.importer import import_rows
from backend.importer.base import Row
from backend.ledger import service as ledger_service


@pytest.fixture
def penny_grounding_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny-grounding.db")
    monkeypatch.setenv("AI_FINANCE_MASTER_KEY_FILE", str(tmp_path / "outside" / "master.key"))
    db.init_db(db.DB_PATH)
    yield tmp_path


def _seed_grounded_rows():
    rows = [
        Row(
            "wechat", "微信支付", "2026-10-23 19:21:00", "支出", 20.0,
            counterparty="乐刻运动健身", item="乐刻运动", tx_type="运动健身",
            tx_no="penny-real-gym-1",
        ),
        Row(
            "wechat", "微信支付", "2026-10-30 19:57:00", "支出", 30.0,
            counterparty="随遇而安（用户84）", item="微信转账", tx_type="转账",
            tx_no="penny-real-transfer-1",
        ),
        Row(
            "wechat", "微信支付", "2026-10-24 12:00:00", "支出", 5.0,
            counterparty="便利店", item="矿泉水", tx_type="日用",
            tx_no="penny-real-daily-1",
        ),
    ]
    import_rows(rows, "wechat", "penny-grounding.csv", batch_id="penny-grounding")
    ledger_service.sync_live_book()


def _october_context():
    return {
        "page": "transactions",
        "path": "/transactions",
        "period": "2026-10",
        "date": "2026-10-23",
        "type": "expense",
    }


def test_category_date_question_returns_only_matching_database_rows_and_skips_model(
    penny_grounding_db, monkeypatch
):
    _seed_grounded_rows()
    monkeypatch.setattr(
        chat_service,
        "_run_openai_chat",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM must not create transaction facts")),
    )
    monkeypatch.setattr(
        chat_service,
        "_run_anthropic_chat",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM must not create transaction facts")),
    )

    conversation = chat_service.create_conversation()
    result = asyncio.run(chat_service.send_message(
        conversation["id"], "健身是几号的", _october_context()
    ))
    answer = result["assistant_message"]

    assert "2026-10-23" in answer
    assert "乐刻运动健身" in answer
    assert "运动健身" in answer
    assert "¥20.00" in answer
    assert "共 **1 笔**" in answer
    assert "随遇而安（用户84）" not in answer
    assert "¥30.00" not in answer
    assert "¥50.00" not in answer


def test_category_date_question_with_no_matching_rows_returns_no_records(
    penny_grounding_db, monkeypatch
):
    conversation = chat_service.create_conversation()
    result = asyncio.run(chat_service.send_message(
        conversation["id"], "健身是几号的", _october_context()
    ))
    answer = result["assistant_message"]

    assert "没有找到符合条件的交易" in answer
    assert "没有使用其他月份、其他分类或模型记忆中的交易补齐结果" in answer
    assert "乐刻运动健身" not in answer
    assert "随遇而安" not in answer
    assert "¥50.00" not in answer


def test_category_date_question_without_month_uses_current_month_not_latest_data(
    penny_grounding_db
):
    from datetime import date

    snapshot = chat_service._query_category_transactions(
        "健身是几号的", {"page": "transactions"}, "健身"
    )
    answer = chat_service._format_category_transactions_answer(snapshot)

    assert snapshot["month"] == date.today().strftime("%Y-%m")
    assert snapshot["count"] == 0
    assert "没有找到符合条件的交易" in answer
    assert "账本最近有数据月份" not in answer


def test_category_query_uses_whole_page_month_not_selected_calendar_day(
    penny_grounding_db
):
    _seed_grounded_rows()
    snapshot = chat_service._query_category_transactions(
        "健身是几号的",
        _october_context(),
        "健身",
    )

    assert snapshot["month"] == "2026-10"
    assert snapshot["count"] == 1
    assert [row["date"] for row in snapshot["transactions"]] == ["2026-10-23"]


def test_transaction_detail_answer_is_rendered_from_tool_rows_not_model_text(
    monkeypatch, penny_grounding_db
):
    real_row = {
        "id": 101,
        "date": "2026-10-23",
        "tx_time": "2026-10-23 19:21:00",
        "direction": "支出",
        "amount": 20.0,
        "counterparty": "乐刻运动健身",
        "item": "乐刻运动",
        "category": "运动健身",
        "subcat": "",
    }
    monkeypatch.setattr(
        chat_service,
        "_execute_tool",
        lambda name, args, conversation_id, context: {
            "keyword": "健身",
            "count": 1,
            "transactions": [real_row],
            "source": "test database",
        },
    )
    responses = iter([
        {
            "tool_calls": [{
                "id": "call_search",
                "function": {
                    "name": "search_transactions",
                    "arguments": json.dumps({"keyword": "健身"}, ensure_ascii=False),
                },
            }]
        },
        {
            "content": (
                "查到两笔：2026-10-30 随遇而安（用户84）30元，"
                "2026-10-23 乐刻运动健身20元，合计50元。"
            )
        },
    ])

    async def fake_post(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(chat_service, "_post_openai", fake_post)
    answer, _actions = asyncio.run(chat_service._run_openai_chat(
        config=None,
        messages=[{"role": "user", "content": "请查一下健身交易明细"}],
        conversation_id="chat_grounding_test",
        system="system",
        context={},
    ))

    assert "乐刻运动健身" in answer
    assert "¥20.00" in answer
    assert "随遇而安（用户84）" not in answer
    assert "¥30.00" not in answer
    assert "¥50.00" not in answer


def test_financial_answer_without_successful_read_tool_is_blocked(monkeypatch):
    async def fake_post(*args, **kwargs):
        return {
            "content": (
                "2026年10月支出50元，其中健身消费30元，餐饮消费20元。"
            )
        }

    monkeypatch.setattr(chat_service, "_post_openai", fake_post)
    answer, _actions = asyncio.run(chat_service._run_openai_chat(
        config=None,
        messages=[{"role": "user", "content": "2026年10月支出多少"}],
        conversation_id="chat_no_tool_test",
        system="system",
        context={},
    ))

    assert "为了避免编造账务信息" in answer
    assert "50元" not in answer


def test_unverified_money_amount_is_blocked_after_a_tool_result(monkeypatch):
    monkeypatch.setattr(
        chat_service,
        "_execute_tool",
        lambda name, args, conversation_id, context: {
            "month": "2026-10",
            "expense": {"amount": 12.0, "count": 1},
            "income": {"amount": 0.0, "count": 0},
            "net": -12.0,
        },
    )
    responses = iter([
        {
            "tool_calls": [{
                "id": "call_overview",
                "function": {
                    "name": "get_financial_overview",
                    "arguments": json.dumps({"month": "2026-10"}),
                },
            }]
        },
        {"content": "2026年10月支出总额为¥999.00。"},
    ])

    async def fake_post(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(chat_service, "_post_openai", fake_post)
    answer, _actions = asyncio.run(chat_service._run_openai_chat(
        config=None,
        messages=[{"role": "user", "content": "2026年10月支出多少"}],
        conversation_id="chat_amount_guard_test",
        system="system",
        context={},
    ))

    assert "金额未能在本轮本地查询结果中核验" in answer
    assert "999.00" not in answer


def test_verified_money_amount_is_allowed():
    evidence = [{
        "month": "2026-10",
        "expense": {"amount": 12.0, "count": 1},
        "income": {"amount": 0.0, "count": 0},
        "net": -12.0,
    }]
    assert not chat_service._has_unverified_money_amount(
        "2026年10月支出为¥12.00。", evidence
    )
    assert chat_service._has_unverified_money_amount(
        "2026年10月支出为¥999.00。", evidence
    )


def test_qwen_openai_compatible_keeps_tool_choice_auto_for_financial_questions(monkeypatch):
    from backend.ai.config import AIConfig

    calls = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeClient:
        def __init__(self, timeout=90):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers, json):
            calls["body"] = json
            return FakeResponse()

    monkeypatch.setattr(chat_service.httpx, "AsyncClient", FakeClient)
    config = AIConfig(
        "openai_compatible",
        "https://maas.qianwenaiapi.com/compatible-mode/v1",
        "qwen3.8-flash",
        "sk-test",
        False,
        0.9,
        True,
    )
    asyncio.run(chat_service._post_openai(
        config, [{"role": "user", "content": "健身是几号的"}]
    ))

    assert calls["body"]["tool_choice"] == "auto"


def test_category_keyword_extraction_removes_chinese_month_scope(penny_grounding_db):
    assert chat_service._extract_category_lookup_keyword("今年十月支出多少") is None
    assert chat_service._extract_category_lookup_keyword("十月健身是几号的") == "健身"


def test_explicit_chinese_month_overrides_page_month(penny_grounding_db):
    _seed_grounded_rows()
    snapshot = chat_service._query_category_transactions(
        "九月健身是几号的",
        _october_context(),
        "健身",
    )
    assert snapshot["month"] == "2026-09"
    assert snapshot["count"] == 0


def test_pending_history_questions_are_answered_from_real_pending_tool_items(monkeypatch):
    snapshot = {
        "has_session": True,
        "pending_count": 2,
        "items": [{
            "date": "2026-10-25",
            "merchant": "未知商户A",
            "item": "消费",
            "amount": 18.5,
            "current_path": "其他",
            "suggested_path": "餐饮",
            "question": "这笔交易具体是什么用途？",
        }],
    }
    monkeypatch.setattr(
        chat_service,
        "_execute_tool",
        lambda name, args, conversation_id, context: snapshot,
    )
    responses = iter([
        {
            "tool_calls": [{
                "id": "call_pending",
                "function": {
                    "name": "get_pending_history_classifications",
                    "arguments": "{}",
                },
            }]
        },
        {"content": "我猜这是一笔健身消费，金额为¥18.50。"},
    ])

    async def fake_post(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(chat_service, "_post_openai", fake_post)
    answer, _actions = asyncio.run(chat_service._run_openai_chat(
        config=None,
        messages=[{"role": "user", "content": "还有哪些待分类交易？"}],
        conversation_id="chat_pending_test",
        system="system",
        context={},
    ))

    assert "未知商户A" in answer
    assert "¥18.50" in answer
    assert "餐饮" in answer
    assert "健身消费" not in answer
    assert "这笔交易具体是什么用途？" in answer


def test_category_change_phrase_is_treated_as_mutation_not_read_only_lookup():
    assert chat_service._is_mutating_financial_request("把星巴克分类为餐饮")
    assert not chat_service._is_financial_fact_question("把星巴克分类为餐饮")


def test_wrong_read_tool_cannot_authorize_an_unrelated_financial_answer(monkeypatch):
    monkeypatch.setattr(
        chat_service,
        "_execute_tool",
        lambda name, args, conversation_id, context: {
            "month": "2026-10",
            "expense": {"amount": 20.0, "count": 1},
            "income": {"amount": 0.0, "count": 0},
            "net": -20.0,
        },
    )
    responses = iter([
        {
            "tool_calls": [{
                "id": "call_wrong_tool",
                "function": {
                    "name": "get_financial_overview",
                    "arguments": json.dumps({"month": "2026-10"}),
                },
            }]
        },
        {"content": "10月支出趋势明显上升，增加了¥20.00。"},
    ])

    async def fake_post(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(chat_service, "_post_openai", fake_post)
    answer, _actions = asyncio.run(chat_service._run_openai_chat(
        config=None,
        messages=[{"role": "user", "content": "2026年10月支出趋势如何"}],
        conversation_id="chat_wrong_tool_test",
        system="system",
        context={},
    ))

    assert "为了避免编造账务信息" in answer
    assert "趋势明显上升" not in answer
