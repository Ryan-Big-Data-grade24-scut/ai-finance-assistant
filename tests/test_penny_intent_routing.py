from datetime import date

import pytest

from backend.ai import chat_service as service


PAGE_CONTEXT = {
    "page": "transactions",
    "path": "/transactions",
    "date": "2026-10-29",
    "type": "expense",
}


def test_month_question_never_inherits_selected_day():
    message = "今年十月的输出是多少"

    assert service._resolve_month_for_message(message, PAGE_CONTEXT) == (
        f"{date.today().year:04d}-10"
    )
    assert service._resolve_date_for_message(message, PAGE_CONTEXT) is None
    assert service._has_period_wide_scope(message, PAGE_CONTEXT)
    assert service._infer_month_direction(message) == "支出"


def test_month_without_year_uses_page_year_not_page_day():
    message = "10月支出多少"

    assert service._resolve_month_for_message(message, PAGE_CONTEXT) == "2026-10"
    assert service._resolve_date_for_message(message, PAGE_CONTEXT) is None


def test_explicit_day_takes_precedence_over_month_scope():
    message = "2026年10月29日支出多少"

    assert service._resolve_date_for_message(message, PAGE_CONTEXT) == "2026-10-29"
    assert not service._has_period_wide_scope(message, PAGE_CONTEXT)


def test_month_total_is_answered_from_local_overview(monkeypatch):
    def fake_overview(args):
        assert args == {"month": "2026-10"}
        return {
            "month": "2026-10",
            "expense": {"amount": 1234.5, "count": 12},
            "income": {"amount": 5000.0, "count": 3},
            "net": 3765.5,
            "note": "test accounting scope",
        }

    monkeypatch.setattr(service, "_tool_financial_overview", fake_overview)
    answer = service._route_month_total_question(
        "2026年10月支出多少", PAGE_CONTEXT
    )

    assert "¥1234.50" in answer
    assert "12 笔" in answer
    assert "¥5000.00" not in answer


def test_monthly_tool_uses_server_resolved_month(monkeypatch):
    monkeypatch.setattr(service, "_tool_financial_overview", lambda args: args)

    result = service._execute_tool(
        "get_financial_overview",
        {"month": "2026-11"},
        "chat_test",
        {"_resolved_month": "2026-10"},
    )

    assert result["month"] == "2026-10"


def test_month_scope_cannot_call_selected_date_tool():
    with pytest.raises(service.AIChatError):
        service._execute_tool(
            "get_transactions_by_date",
            {"date": "2026-10-29"},
            "chat_test",
            {"_ignore_selected_date": "true"},
        )


def test_unresolved_year_scope_cannot_default_to_latest_month():
    with pytest.raises(service.AIChatError):
        service._execute_tool(
            "get_financial_overview",
            {},
            "chat_test",
            {"_unresolved_period_scope": "true"},
        )


def test_trend_and_category_questions_are_not_misclassified_as_simple_total():
    assert not service._is_month_total_question("今年十月支出趋势如何")
    assert not service._is_month_total_question("今年十月餐饮花了多少")
