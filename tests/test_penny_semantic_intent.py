from __future__ import annotations

from datetime import date

from backend import db
from backend.ai import chat_service as service


PAGE_CONTEXT = {
    "page": "transactions",
    "path": "/transactions",
    "date": "2026-10-23",
    "type": "expense",
}


def test_trend_query_is_not_misread_as_a_category_keyword():
    message = "比较10月和9月的支出趋势"

    assert service._is_trend_question(message)
    assert service._extract_category_lookup_keyword(message) is None
    assert service._route_category_transaction_question(message, PAGE_CONTEXT) is None
    assert service._expected_evidence_tool_names(message) == {"analyze_spending_trends"}


def test_two_explicit_months_are_preserved_in_user_order():
    assert service._resolve_comparison_months(
        "比较10月和9月的支出趋势", PAGE_CONTEXT
    ) == ("2026-10", "2026-09")


def test_relative_month_comparison_uses_the_finance_page_month():
    assert service._resolve_comparison_months(
        "比较本月与上月的收支变化", PAGE_CONTEXT
    ) == ("2026-10", "2026-09")


def test_trend_tool_receives_both_server_resolved_months(monkeypatch):
    monkeypatch.setattr(service, "_tool_spending_trends", lambda args: args)

    result = service._execute_tool(
        "analyze_spending_trends",
        {"month": "2026-11", "compare_month": "2026-10", "direction": "全部"},
        "chat_test",
        {
            "_resolved_month": "2026-10",
            "_resolved_compare_month": "2026-09",
        },
    )

    assert result["month"] == "2026-10"
    assert result["compare_month"] == "2026-09"
    assert result["direction"] == "全部"


def test_category_list_question_uses_monthly_category_breakdown(monkeypatch):
    calls = []

    def fake_breakdown(args):
        calls.append(args)
        return {
            "month": args["month"],
            "direction": args["direction"],
            "categories": [
                {"category": "餐饮", "subcat": "早餐", "amount": 25.0, "count": 2},
                {"category": "交通", "subcat": "地铁", "amount": 10.0, "count": 1},
            ],
        }

    monkeypatch.setattr(service, "_tool_category_breakdown", fake_breakdown)
    answer = service._route_category_breakdown_question(
        "现在都有哪些类别的支出", PAGE_CONTEXT
    )

    assert calls == [{"month": "2026-10", "direction": "支出"}]
    assert "2026-10" in answer
    assert "餐饮" in answer
    assert "交通" in answer
    assert "¥35.00" in answer


def test_category_synonyms_resolve_to_existing_ledger_categories(monkeypatch):
    class FakeCursor:
        def fetchall(self):
            return [
                {"category": "餐饮", "subcat": "早餐"},
                {"category": "运动健身", "subcat": "健身房"},
            ]

    class FakeConnection:
        def execute(self, *_args, **_kwargs):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(db, "get_conn", lambda: FakeConnection())

    assert service._match_category_keyword("10月饮食的支出") == "餐饮"
    assert service._match_category_keyword("健身是几号的") == "运动健身"


def test_category_detail_question_is_routed_as_real_category_lookup(monkeypatch):
    class FakeCursor:
        def fetchall(self):
            return [
                {"category": "运动健身", "subcat": "健身房"},
            ]

    class FakeConnection:
        def execute(self, *_args, **_kwargs):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(db, "get_conn", lambda: FakeConnection())
    monkeypatch.setattr(
        service,
        "_query_category_transactions",
        lambda message, context, keyword: {
            "month": "2026-10",
            "period_note": "当前页面所在月份",
            "direction": "支出",
            "keyword": keyword,
            "match_mode": "category",
            "matched_categories": [{"category": "运动健身", "subcat": "健身房"}],
            "count": 1,
            "total_amount": 20.0,
            "transactions": [{
                "id": 10, "date": "2026-10-23", "tx_time": "2026-10-23 19:21:00",
                "direction": "支出", "amount": 20.0, "counterparty": "乐刻运动健身",
                "item": "乐刻运动", "category": "运动健身", "subcat": "健身房",
                "pay_method": "微信支付",
            }],
            "source": "test database",
        },
    )

    answer = service._route_category_transaction_question(
        "健身是几号的", PAGE_CONTEXT
    )

    assert "2026-10-23" in answer
    assert "乐刻运动健身" in answer
    assert "¥20.00" in answer


def test_category_amount_question_is_not_sent_to_keyword_search(monkeypatch):
    class FakeCursor:
        def fetchall(self):
            return [{"category": "餐饮", "subcat": "早餐"}]

    class FakeConnection:
        def execute(self, *_args, **_kwargs):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(db, "get_conn", lambda: FakeConnection())
    monkeypatch.setattr(
        service,
        "_query_category_transactions",
        lambda message, context, keyword: {
            "month": "2026-10",
            "keyword": keyword,
            "count": 2,
            "total_amount": 35.0,
            "matched_categories": [{"category": "餐饮", "subcat": "早餐"}],
            "transactions": [
                {"amount": 15.0, "subcat": "早餐"},
                {"amount": 20.0, "subcat": "早餐"},
            ],
            "truncated": False,
        },
    )

    answer = service._route_category_summary_question(
        "10月饮食的支出", PAGE_CONTEXT
    )

    assert "2026-10" in answer
    assert "餐饮" in answer
    assert "¥35.00" in answer
    assert "2 笔" in answer


def test_category_detail_can_inherit_month_from_recent_conversation():
    history = [
        {"role": "user", "content": "比较10月和9月的支出趋势"},
        {"role": "assistant", "content": "已经比较了10月和9月。"},
    ]

    assert service._resolve_message_month(
        "那健身是几号的？", {}, history
    ) == "2026-10"


def test_category_tool_month_is_optional_when_server_context_supplies_it():
    definition = next(
        item for item in service._TOOL_DEFINITIONS
        if item["name"] == "get_category_transactions"
    )
    assert definition["parameters"]["required"] == ["keyword"]


import pytest


@pytest.fixture
def semantic_db(tmp_path, monkeypatch):
    from backend.importer import import_rows
    from backend.importer.base import Row
    from backend.ledger import service as ledger_service

    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "semantic-test.db")
    monkeypatch.setenv("AI_FINANCE_MASTER_KEY_FILE", str(tmp_path / "outside" / "master.key"))
    db.init_db(db.DB_PATH)
    rows = [
        Row("alipay", "支付宝", "2026-09-01 10:00:00", "支出", 100.0,
            counterparty="商户A", item="餐饮", tx_type="餐饮", tx_no="sem-sep-exp"),
        Row("alipay", "支付宝", "2026-09-02 10:00:00", "收入", 500.0,
            counterparty="工资", item="工资", tx_type="工资", tx_no="sem-sep-inc"),
        Row("alipay", "支付宝", "2026-10-01 10:00:00", "支出", 180.0,
            counterparty="商户A", item="餐饮", tx_type="餐饮", tx_no="sem-oct-exp"),
        Row("alipay", "支付宝", "2026-10-02 10:00:00", "收入", 600.0,
            counterparty="工资", item="工资", tx_type="工资", tx_no="sem-oct-inc"),
    ]
    import_rows(rows, "alipay", "semantic.csv", batch_id="semantic-seed")
    ledger_service.sync_live_book()
    yield tmp_path


def test_trend_tool_compares_income_expense_and_net_for_two_months(semantic_db):
    result = service._tool_spending_trends({
        "month": "2026-10",
        "compare_month": "2026-09",
        "direction": "全部",
    })

    assert result["month"] == "2026-10"
    assert result["previous_month"] == "2026-09"
    assert result["current"]["expense"]["amount"] == 180.0
    assert result["previous"]["expense"]["amount"] == 100.0
    assert result["current"]["income"]["amount"] == 600.0
    assert result["previous"]["income"]["amount"] == 500.0
    assert result["current"]["net"] == 420.0
    assert result["previous"]["net"] == 400.0
    assert result["expense_change_amount"] == 80.0
    assert result["income_change_amount"] == 100.0
    assert result["net_change_amount"] == 20.0


def test_model_without_tool_call_gets_server_planned_trend_evidence(
    semantic_db, monkeypatch
):
    import asyncio
    from backend.ai.config import AIConfig

    config = AIConfig(
        "openai_compatible",
        "https://maas.qianwenaiapi.com/compatible-mode/v1",
        "qwen3.8-flash",
        "sk-test",
        False,
        0.9,
        True,
    )
    monkeypatch.setattr(service, "get_secret_config", lambda: config)
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
            calls.append({"url": url, "body": json, "timeout": self.timeout})
            if len(calls) == 1:
                return FakeResponse({"choices": [{
                    "message": {"role": "assistant", "content": "我先检查数据。"}
                }]})
            return FakeResponse({"choices": [{
                "message": {
                    "role": "assistant",
                    "content": "2026年10月支出为¥180.00，9月为¥100.00，增加¥80.00，增长80%。",
                }
            }]})

    monkeypatch.setattr(service.httpx, "AsyncClient", FakeClient)
    conversation = service.create_conversation()
    result = asyncio.run(service.send_message(
        conversation["id"],
        "比较10月和9月的支出趋势",
        {"page": "bill", "period": "2026-10"},
    ))

    assert "增加¥80.00" in result["assistant_message"]
    # Explicit two-month comparisons are rendered from the local aggregate
    # directly, so the language model cannot alter or block verified totals.
    assert calls == []


def test_category_count_wording_is_routed_to_category_breakdown():
    assert service._is_category_breakdown_question("10月份都有几种类别支出")
    assert service._is_category_breakdown_question("现在都有哪些类别的支出")


def test_category_breakdown_answer_reports_distinct_top_level_category_count():
    answer = service._format_category_breakdown_answer({
        "month": "2026-10",
        "direction": "支出",
        "categories": [
            {"category": "餐饮", "subcat": "早餐", "amount": 25.0, "count": 2},
            {"category": "餐饮", "subcat": "午餐", "amount": 35.0, "count": 3},
            {"category": "交通", "subcat": "地铁", "amount": 10.0, "count": 1},
        ],
    })
    assert "共 2 个支出大类" in answer
    assert "餐饮、交通" in answer


def test_trend_question_is_rendered_from_deterministic_local_snapshot(monkeypatch):
    captured = {}

    def fake_trends(args):
        captured.update(args)
        return {
            "month": "2026-10",
            "previous_month": "2026-09",
            "direction": "支出",
            "current": {"amount": 2628.06, "count": 89},
            "previous": {"amount": 2100.00, "count": 70},
            "change": 528.06,
            "change_percent": 25.1,
            "category_changes": [
                {
                    "category": "餐饮",
                    "current_amount": 900.0,
                    "previous_amount": 600.0,
                    "change_amount": 300.0,
                }
            ],
            "note": "来自本地测试数据库。",
        }

    monkeypatch.setattr(service, "_tool_spending_trends", fake_trends)
    answer = service._route_spending_trend_question(
        "比较10月和9月的支出趋势",
        {
            "_resolved_month": "2026-10",
            "_resolved_compare_month": "2026-09",
        },
    )

    assert captured == {
        "month": "2026-10",
        "compare_month": "2026-09",
        "direction": "支出",
    }
    assert "¥2,628.06" in answer
    assert "¥2,100.00" in answer
    assert "增加¥528.06" in answer
    assert "餐饮" in answer


def test_recent_spending_analysis_uses_latest_ledger_date_not_current_month(monkeypatch):
    calls = []

    class Cursor:
        def __init__(self, one=None, rows=None):
            self.one = one
            self.rows = rows or []

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.rows

    class Connection:
        def execute(self, sql, params=()):
            calls.append((sql, params))
            if "MAX(date)" in sql:
                return Cursor(one={"latest_date": "2026-10-30"})
            if "GROUP BY COALESCE(category" in sql:
                return Cursor(rows=[
                    {"category": "餐饮", "amount": 120.0, "count": 4},
                    {"category": "交通", "amount": 30.0, "count": 2},
                ])
            if "GROUP BY COALESCE(NULLIF(trim(counterparty)" in sql:
                return Cursor(rows=[
                    {"merchant": "测试餐厅", "amount": 80.0, "count": 2},
                ])
            if "SELECT id,date" in sql:
                return Cursor(rows=[
                    {
                        "id": 9, "date": "2026-10-28", "merchant": "测试餐厅",
                        "description": "晚餐", "amount": 80.0,
                        "category": "餐饮", "subcat": "",
                    },
                ])
            if "SUM(amount)" in sql:
                if params == ("2026-10-01", "2026-10-30"):
                    return Cursor(one={"amount": 150.0, "count": 6})
                if params == ("2026-09-01", "2026-09-30"):
                    return Cursor(one={"amount": 100.0, "count": 4})
            raise AssertionError(f"Unexpected SQL: {sql} {params}")

        def close(self):
            pass

    monkeypatch.setattr(db, "get_conn", lambda: Connection())
    snapshot = service._tool_recent_spending_analysis({"days": 30})

    assert snapshot["start_date"] == "2026-10-01"
    assert snapshot["end_date"] == "2026-10-30"
    assert snapshot["latest_ledger_date"] == "2026-10-30"
    assert snapshot["current"] == {"amount": 150.0, "count": 6}
    assert snapshot["previous_period"]["start_date"] == "2026-09-01"
    assert any(params == ("2026-10-01", "2026-10-30") for _, params in calls)


def test_recent_habit_question_routes_to_rolling_analysis(monkeypatch):
    captured = {}

    def fake_tool(args):
        captured.update(args)
        return {
            "has_data": True,
            "days": 30,
            "start_date": "2026-10-01",
            "end_date": "2026-10-30",
            "latest_ledger_date": "2026-10-30",
            "current": {"amount": 150.0, "count": 6},
            "previous_period": {
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
                "amount": 100.0,
                "count": 4,
            },
            "categories": [
                {"category": "餐饮", "amount": 120.0, "count": 4, "share_percent": 80.0},
                {"category": "交通", "amount": 30.0, "count": 2, "share_percent": 20.0},
            ],
            "top_merchants": [],
            "largest_transactions": [],
            "note": "来自本地测试数据库。",
        }

    monkeypatch.setattr(service, "_tool_recent_spending_analysis", fake_tool)
    answer = service._route_recent_spending_analysis_question(
        "分析一下我最近的消费习惯，有什么值得注意的地方？",
        {"date": "2026-11-03"},
    )

    assert captured == {"days": 30}
    assert "2026-10-01 至 2026-10-30" in answer
    assert "餐饮" in answer
    assert "占本期支出的 80.0%" in answer
    assert "与前 30 天相比" in answer
