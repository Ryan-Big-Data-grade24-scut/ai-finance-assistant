from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from backend import db
from backend.ai import chat_service
from backend.ai.agent.context_manager import ContextManager
from backend.ai.agent.executor import Executor
from backend.ai.agent.models import AgentPlan, AgentStep
from backend.ai.agent.plan_validator import PlanValidationError, PlanValidator
from backend.ai.agent.planner import Planner
from backend.ai.agent.task_state import AgentTaskStore
from backend.ai.agent.tool_registry import ToolRegistry, ToolRegistryError
from backend.ai.agent.verifier import Verifier
from backend.ai.config import AIConfig
from backend.importer import import_rows
from backend.importer.base import Row
from backend.ledger import service as ledger_service


@pytest.fixture
def agent_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny-agent.db")
    monkeypatch.setenv("AI_FINANCE_MASTER_KEY_FILE", str(tmp_path / "outside" / "master.key"))
    db.init_db(db.DB_PATH)
    yield tmp_path


def _config() -> AIConfig:
    return AIConfig(
        "openai_compatible", "https://example.test/v1", "test-model", "test-key",
        False, 0.9, True,
    )


# 50 natural-language regression prompts. These assert that complex, incomplete,
# and context-dependent requests enter the planner instead of a one-shot route.
AGENT_ROUTING_CASES = [
    ("分析一下我9月和10月的消费习惯，有什么值得注意的地方？", True),
    ("比较9月和10月的支出，并看看分类和商户变化。", True),
    ("帮我对比这两个月的消费结构、商户分布和大额交易。", True),
    ("最近花钱是不是有点多？从类别和商户两个方面看看。", True),
    ("检查10月异常消费，再结合分类变化分析原因。", True),
    ("9月和10月餐饮支出对比一下，顺便找出具体交易。", True),
    ("帮我看看收入、支出、分类结构和商户排名的整体变化。", True),
    ("我想知道最近消费最多的商户，以及钱主要花在哪些类别。", True),
    ("看看最近有没有重复扣款，并分析大额消费。", True),
    ("把最近三个月的支出趋势、类别分布和异常项目一起分析。", True),
    ("分析一下2026-09和2026-10的消费模式。", True),
    ("2026年9月与10月的收入支出变化，哪些类别贡献最大？", True),
    ("帮我从月度总额、类别和商户三个维度分析10月。", True),
    ("比较上个月和这个月，指出消费变化最大的地方。", True),
    ("我想看消费习惯，有哪些问题值得注意？", True),
    ("从账单里找出异常支出，再看看是否集中在某些商户。", True),
    ("分析交通和餐饮两类消费的变化和具体流水。", True),
    ("对比9月和10月的餐饮、交通、购物支出。", True),
    ("看一下每个月总支出，再分析商户集中度。", True),
    ("我最近开销怎么样，和之前相比有什么变化？", True),
    ("帮我分析一下", True),
    ("看看账单", True),
    ("这个情况怎么回事？", True),
    ("那10月呢", True),
    ("其中餐饮呢？", True),
    ("继续，看看具体交易。", True),
    ("再按商户拆开分析。", True),
    ("如果只看支出呢？", True),
    ("上面说的异常交易有哪些具体记录？", True),
    ("刚才的结果和9月相比呢？", True),
    ("2026年10月支出多少？", False),
    ("10月有哪些支出类别？", False),
    ("健身是几号的？", False),
    ("比较10月和9月的支出趋势。", False),
    ("最近10笔交易是什么？", False),
    ("查看待分类交易。", False),
    ("今天花了多少钱？", False),
    ("查一下星巴克交易。", False),
    ("10月收入多少？", False),
    ("餐饮类别有哪些交易？", False),
    ("查询10月商户排名。", False),
    ("最近有没有异常消费？", False),
    ("查看AI分类学习进度。", False),
    ("把这笔交易改成餐饮。", False),
    ("新增一笔今天的支出。", False),
    ("帮我保存星巴克分类规则。", False),
    ("10月支出总额是多少？", False),
    ("查看2026-10-29的交易明细。", False),
    ("把最近交易列出来。", False),
    ("查询9月支出趋势。", False),
]


@pytest.mark.parametrize("message,expected", AGENT_ROUTING_CASES)
def test_natural_language_routing_matrix(message, expected, monkeypatch):
    monkeypatch.setattr(chat_service.TASK_STORE, "latest", lambda _conversation_id: None)
    prior = []
    if message in {"那10月呢", "其中餐饮呢？", "继续，看看具体交易。", "再按商户拆开分析。"}:
        prior = [
            {"role": "user", "content": "分析一下9月和10月的消费习惯"},
            {"role": "assistant", "content": "我已经完成初步分析。"},
        ]
    assert chat_service._should_use_agent_runtime(message, prior, "chat_test") is expected


def test_tool_registry_is_single_source_for_provider_schemas(agent_db):
    registry = chat_service._TOOL_REGISTRY
    names = registry.names()
    openai_names = {item["function"]["name"] for item in registry.openai_tools(allow_proposals=True)}
    anthropic_names = {item["name"] for item in registry.anthropic_tools(allow_proposals=True)}
    planner_names = {item["name"] for item in registry.planner_catalog(allow_proposals=True)}
    assert names == openai_names == anthropic_names == planner_names
    assert len(names) == 16


def test_tool_registry_hides_proposals_for_read_only_planner(agent_db):
    catalog = chat_service._TOOL_REGISTRY.planner_catalog(allow_proposals=False)
    names = {item["name"] for item in catalog}
    assert "get_financial_overview" in names
    assert "propose_manual_transaction" not in names
    assert all(not item["proposal_only"] for item in catalog)


def test_tool_registry_validates_schema_and_required_fields():
    registry = ToolRegistry()
    registry.register({
        "name": "lookup",
        "description": "test",
        "parameters": {
            "type": "object",
            "properties": {"month": {"type": "string", "pattern": "^20\\d{2}-(0[1-9]|1[0-2])$"}},
            "required": ["month"],
        },
    }, read_only=True)
    with pytest.raises(ToolRegistryError, match="缺少必填"):
        registry.validate_arguments("lookup", {})
    with pytest.raises(ToolRegistryError, match="格式不合法"):
        registry.validate_arguments("lookup", {"month": "2026-13"})
    registry.validate_arguments("lookup", {"month": "2026-10"})


def test_tool_registry_rejects_unknown_tool(agent_db):
    with pytest.raises(chat_service.AIChatError, match="未注册"):
        chat_service._execute_tool("unknown_tool", {}, "chat_test", {})


def test_month_arguments_are_not_globally_overwritten_in_multi_month_plan(monkeypatch, agent_db):
    monkeypatch.setattr(chat_service, "_tool_financial_overview", lambda args: args)
    result = chat_service._execute_tool(
        "get_financial_overview", {"month": "2026-09"}, "chat_test",
        {"_resolved_month": "2026-10", "_explicit_months": "2026-09|2026-10"},
    )
    assert result["month"] == "2026-09"


def test_single_month_server_resolution_remains_authoritative(monkeypatch, agent_db):
    monkeypatch.setattr(chat_service, "_tool_financial_overview", lambda args: args)
    result = chat_service._execute_tool(
        "get_financial_overview", {"month": "2026-11"}, "chat_test",
        {"_resolved_month": "2026-10"},
    )
    assert result["month"] == "2026-10"


def test_unresolved_period_cannot_be_overridden_by_model_guessed_month(monkeypatch, agent_db):
    monkeypatch.setattr(chat_service, "_tool_financial_overview", lambda args: args)
    with pytest.raises(chat_service.AIChatError, match="无法从问题中确定具体月份"):
        chat_service._execute_tool(
            "get_financial_overview", {"month": "2026-01"}, "chat_test",
            {"_unresolved_period_scope": "true"},
        )


def test_trend_tool_preserves_two_explicit_months(monkeypatch, agent_db):
    monkeypatch.setattr(chat_service, "_tool_spending_trends", lambda args: args)
    result = chat_service._execute_tool(
        "analyze_spending_trends",
        {"month": "2026-09", "compare_month": "2026-10", "direction": "全部"},
        "chat_test",
        {"_resolved_month": "2026-10", "_resolved_compare_month": "2026-09",
         "_explicit_months": "2026-09|2026-10"},
    )
    assert result["month"] == "2026-09"
    assert result["compare_month"] == "2026-10"


def test_plan_validator_accepts_parallel_read_only_plan(agent_db):
    plan = AgentPlan(steps=[
        AgentStep(id="overview", tool="get_financial_overview", purpose="查询月度收支", arguments={"month": "2026-10"}),
        AgentStep(id="categories", tool="get_category_breakdown", purpose="查询分类分布", arguments={"month": "2026-10", "direction": "支出"}),
    ])
    validated = PlanValidator(chat_service._TOOL_REGISTRY).validate(
        plan, requires_evidence=True, allow_proposals=False
    )
    assert len(validated.steps) == 2


def test_plan_validator_rejects_unknown_tool(agent_db):
    plan = AgentPlan(steps=[AgentStep(id="bad", tool="not_registered")])
    with pytest.raises(PlanValidationError, match="不存在的工具"):
        PlanValidator(chat_service._TOOL_REGISTRY).validate(
            plan, requires_evidence=True, allow_proposals=False
        )


def test_plan_validator_rejects_invalid_tool_arguments(agent_db):
    plan = AgentPlan(steps=[AgentStep(
        id="bad", tool="get_category_breakdown", arguments={"month": "2026-13", "direction": "支出"}
    )])
    with pytest.raises(PlanValidationError, match="有效的 YYYY-MM"):
        PlanValidator(chat_service._TOOL_REGISTRY).validate(
            plan, requires_evidence=True, allow_proposals=False
        )


def test_plan_validator_rejects_write_proposal_for_finance_question(agent_db):
    plan = AgentPlan(steps=[AgentStep(
        id="write", tool="propose_manual_transaction",
        arguments={"date": "2026-10-01", "direction": "支出", "amount": 10},
    )])
    with pytest.raises(PlanValidationError, match="只能用于"):
        PlanValidator(chat_service._TOOL_REGISTRY).validate(
            plan, requires_evidence=False, allow_proposals=False
        )


def test_plan_validator_rejects_cycles(agent_db):
    plan = AgentPlan(steps=[
        AgentStep(id="a", tool="get_financial_overview", arguments={"month": "2026-10"}, depends_on=["b"]),
        AgentStep(id="b", tool="get_category_breakdown", arguments={"month": "2026-10", "direction": "支出"}, depends_on=["a"]),
    ])
    with pytest.raises(PlanValidationError, match="循环"):
        PlanValidator(chat_service._TOOL_REGISTRY).validate(
            plan, requires_evidence=True, allow_proposals=False
        )


def test_plan_validator_requires_evidence_for_financial_question(agent_db):
    with pytest.raises(PlanValidationError, match="没有规划"):
        PlanValidator(chat_service._TOOL_REGISTRY).validate(
            AgentPlan(), requires_evidence=True, allow_proposals=False
        )


def test_plan_validator_accepts_clarification_without_tools(agent_db):
    plan = AgentPlan(needs_clarification=True, clarification_question="你想分析哪个月份？")
    assert PlanValidator(chat_service._TOOL_REGISTRY).validate(
        plan, requires_evidence=True, allow_proposals=False
    ).needs_clarification


def test_plan_validator_checks_reference_dependency(agent_db):
    plan = AgentPlan(steps=[
        AgentStep(id="lookup", tool="search_transactions", arguments={"keyword": "咖啡"}),
        AgentStep(id="proposal", tool="propose_transaction_reclassification",
                  arguments={"tx_id": {"$ref": "lookup.transactions.0.id"},
                             "target_path": "餐饮 / 咖啡", "reason": "用户要求调整"},
                  depends_on=["lookup"]),
    ])
    with pytest.raises(PlanValidationError, match="只能用于"):
        PlanValidator(chat_service._TOOL_REGISTRY).validate(
            plan, requires_evidence=False, allow_proposals=False
        )


def test_context_manager_loads_waiting_task_and_inherited_constraints(agent_db):
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "比较9月和10月消费", {"page_context": {}})
    store.update_task(task["id"], status="waiting_for_user", plan_json=(
        '{"steps":[{"tool":"analyze_spending_trends","arguments":'
        '{"month":"2026-10","compare_month":"2026-09"}}]}'
    ), clarification_question="你要看支出还是收支？")
    assembled = ContextManager(store).build(
        conversation_id=conversation["id"], user_message="支出",
        history=[{"role": "user", "content": "比较9月和10月消费"}], page_context={},
    )
    assert assembled["active_task"]["goal"] == "比较9月和10月消费"
    assert assembled["active_task"]["clarification_question"] == "你要看支出还是收支？"
    assert assembled["inherited_constraints"] == {"month": "2026-10", "compare_month": "2026-09"}


def test_long_term_preferences_are_separate_from_temporary_query_constraints(agent_db):
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    store.save_preferences({"default_direction": "支出", "analysis_detail": "detailed"})
    task = store.create_or_resume(conversation["id"], "比较9月和10月", {})
    store.update_task(task["id"], plan_json=(
        '{"steps":[{"tool":"analyze_spending_trends","arguments":'
        '{"month":"2026-10","compare_month":"2026-09"}}]}'
    ))
    context = ContextManager(store).build(
        conversation_id=conversation["id"], user_message="那10月呢", history=[], page_context={},
    )
    assert context["long_term_preferences"] == {"default_direction": "支出", "analysis_detail": "detailed"}
    assert context["inherited_constraints"] == {"month": "2026-10", "compare_month": "2026-09"}
    assert "default_direction" not in context["inherited_constraints"]


def test_long_term_preferences_reject_unknown_keys_and_values(agent_db):
    store = AgentTaskStore()
    saved = store.save_preferences({
        "default_direction": "全部", "admin_mode": "true",
        "preferred_currency": "BTC", "analysis_detail": {"secret": "no"},
    })
    assert saved == {"default_direction": "全部"}


def test_task_store_resumes_waiting_task_with_new_generation(agent_db):
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "分析账单", {"inherited_constraints": {}})
    store.update_task(task["id"], status="waiting_for_user")
    resumed = store.create_or_resume(conversation["id"], "支出", {"inherited_constraints": {"direction": "支出"}})
    assert resumed["id"] == task["id"]
    assert resumed["generation"] == 2
    assert resumed["status"] == "planning"


def test_interrupted_task_checkpoint_is_recoverable_and_reuses_only_exact_steps(agent_db):
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "分析10月收支", {})
    step = AgentStep(
        id="overview", tool="get_financial_overview",
        arguments={"month": "2026-10"}, purpose="查询10月总览",
    )
    store.add_steps(task["id"], 1, [step.model_dump()])
    store.update_step(
        task["id"], 1, "overview", status="succeeded",
        result={"has_data": True, "month": "2026-10", "expense": {"amount": 88}},
    )
    assert store.mark_interrupted_tasks() == 1
    interrupted = store.latest(conversation["id"])
    assert interrupted["status"] == "interrupted"
    resumed = store.create_or_resume(conversation["id"], "继续", {})
    assert resumed["id"] == task["id"] and resumed["generation"] == 2
    reused = store.reusable_checkpoint_results(task["id"], 2, [step])
    assert reused["overview"]["expense"]["amount"] == 88
    changed = AgentStep(
        id="overview", tool="get_financial_overview",
        arguments={"month": "2026-09"}, purpose="查询9月总览",
    )
    assert store.reusable_checkpoint_results(task["id"], 2, [changed]) == {}


def test_task_store_creates_child_after_completed_task(agent_db):
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    first = store.create_or_resume(conversation["id"], "查询10月", {})
    store.update_task(first["id"], status="completed")
    second = store.create_or_resume(conversation["id"], "再查9月", {})
    assert second["id"] != first["id"]
    assert second["parent_task_id"] == first["id"]


def test_deleting_conversation_cascades_agent_tasks(agent_db):
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "查询", {})
    chat_service.delete_conversation(conversation["id"])
    assert store.get(task["id"]) is None


def test_executor_runs_independent_queries_in_parallel_and_resolves_refs(agent_db):
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "并行执行测试", {})
    registry = ToolRegistry()
    for name, schema in [
        ("read_a", {"type": "object", "properties": {}, "required": []}),
        ("read_b", {"type": "object", "properties": {}, "required": []}),
        ("read_c", {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}),
    ]:
        registry.register({"name": name, "description": name, "parameters": schema}, read_only=True)
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def handler(name, args, conversation_id, context):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.08)
        with lock:
            state["active"] -= 1
        if name == "read_a":
            return {"value": 7}
        if name == "read_b":
            return {"value": 9}
        return {"received": args["value"]}

    registry.set_handler(handler)
    steps = [
        AgentStep(id="a", tool="read_a", arguments={}),
        AgentStep(id="b", tool="read_b", arguments={}),
        AgentStep(id="c", tool="read_c", arguments={"value": {"$ref": "a.value"}}, depends_on=["a"]),
    ]
    store.add_steps(task["id"], 1, [step.model_dump() for step in steps])
    report = asyncio.run(Executor(registry, store).execute(
        task_id=task["id"], generation=1, steps=steps,
        conversation_id=conversation["id"], context={}, allow_proposals=False,
    ))
    assert state["max_active"] >= 2
    assert report.results["c"] == {"received": 7}
    assert len(report.executions) == 3
    assert not report.failed_steps


def test_executor_marks_tool_timeout_as_failed(agent_db):
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "timeout", {})
    registry = ToolRegistry()
    registry.register({"name": "slow", "description": "slow", "parameters": {"type": "object", "properties": {}, "required": []}}, read_only=True)
    registry.set_handler(lambda *args: (time.sleep(0.08) or {"ok": True}))
    step = AgentStep(id="slow1", tool="slow")
    store.add_steps(task["id"], 1, [step.model_dump()])
    report = asyncio.run(Executor(registry, store, tool_timeout_seconds=0.01).execute(
        task_id=task["id"], generation=1, steps=[step],
        conversation_id=conversation["id"], context={}, allow_proposals=False,
    ))
    assert report.failed_steps == ["slow1"]
    assert report.executions[0]["status"] == "failed"


def test_verifier_accepts_successful_read_only_evidence(agent_db):
    plan = AgentPlan(steps=[AgentStep(id="s1", tool="get_financial_overview", arguments={"month": "2026-10"})])
    result = Verifier(chat_service._TOOL_REGISTRY).verify(
        plan=plan,
        executions=[{"step_id": "s1", "tool": "get_financial_overview", "arguments": {"month": "2026-10"},
                    "status": "succeeded", "result": {"has_data": True, "month": "2026-10", "expense": {"amount": 20, "count": 1}}}],
        requires_evidence=True,
    )
    assert result.verified
    assert result.evidence[0]["state"] == "verified"
    assert result.evidence[0]["scope"]["month"] == "2026-10"


def test_verifier_treats_empty_search_as_no_data_not_zero_spend(agent_db):
    plan = AgentPlan(steps=[AgentStep(id="s1", tool="search_transactions", arguments={"keyword": "不存在"})])
    result = Verifier(chat_service._TOOL_REGISTRY).verify(
        plan=plan,
        executions=[{"step_id": "s1", "tool": "search_transactions", "arguments": {"keyword": "不存在"},
                    "status": "succeeded", "result": {"keyword": "不存在", "count": 0, "transactions": []}}],
        requires_evidence=True,
    )
    assert result.verified
    assert result.evidence[0]["state"] == "no_data"
    assert "不能将其解释为零支出" in result.usable_results[0]["reason"]


def test_verifier_rejects_ledger_incomplete_result(agent_db):
    plan = AgentPlan(steps=[AgentStep(id="s1", tool="get_transactions_by_date", arguments={"date": "2026-10-01"})])
    result = Verifier(chat_service._TOOL_REGISTRY).verify(
        plan=plan,
        executions=[{"step_id": "s1", "tool": "get_transactions_by_date", "arguments": {"date": "2026-10-01"},
                    "status": "succeeded", "result": {"ledger_complete": False, "transactions": []}}],
        requires_evidence=True,
    )
    assert not result.verified
    assert "Ledger 证据不完整" in result.issues[0]


def test_verifier_marks_missing_month_as_partial_not_zero(agent_db):
    plan = AgentPlan(steps=[AgentStep(id="s1", tool="analyze_spending_trends", arguments={"month": "2026-10", "compare_month": "2026-09"})])
    result = Verifier(chat_service._TOOL_REGISTRY).verify(
        plan=plan,
        executions=[{"step_id": "s1", "tool": "analyze_spending_trends", "arguments": {"month": "2026-10", "compare_month": "2026-09"},
                    "status": "succeeded", "result": {"has_data": True, "month_coverage": {
                        "2026-10": {"has_data": True, "count": 12},
                        "2026-09": {"has_data": False, "count": 0},
                    }, "current": {"amount": 100}, "previous": {"amount": None}}}],
        requires_evidence=True,
    )
    assert result.partial
    assert result.evidence[0]["state"] == "partial"


def test_answer_verifier_rejects_unverified_month_and_date(agent_db):
    verifier = Verifier(chat_service._TOOL_REGISTRY)
    evidence = [{"evidence_id": "E001", "result": {"month": "2026-10", "date": "2026-10-02"}}]
    assert verifier.verify_answer("2026-10 有一笔交易。", evidence)[0]
    assert not verifier.verify_answer("2026-09 有一笔交易。", evidence)[0]
    assert not verifier.verify_answer("交易发生在2026-10-03。", evidence)[0]


def test_answer_verifier_rejects_unverified_count_percentage_and_tx_id(agent_db):
    verifier = Verifier(chat_service._TOOL_REGISTRY)
    evidence = [{"evidence_id": "E001", "result": {
        "count": 3, "change_percent": 25.0, "transactions": [{"id": 88}],
    }}]
    assert verifier.verify_answer("共3笔，增长25%，交易 #88。", evidence)[0]
    assert not verifier.verify_answer("共4笔。", evidence)[0]
    assert not verifier.verify_answer("增长30%。", evidence)[0]
    assert not verifier.verify_answer("交易 #99。", evidence)[0]


def test_planner_receives_history_context_and_registry_catalog(monkeypatch, agent_db):
    captured = {}

    async def fake_complete_json(config, *, system_prompt, user_payload, timeout):
        captured.update(user_payload)
        return {"goal": "分析两个月消费", "intent": "multi_month_analysis", "steps": [
            {"id": "s1", "tool": "get_financial_overview", "purpose": "查询10月总览",
             "arguments": {"month": "2026-10"}, "depends_on": []}
        ]}

    monkeypatch.setattr("backend.ai.agent.planner.complete_json", fake_complete_json)
    planner = Planner(chat_service._TOOL_REGISTRY)
    plan = asyncio.run(planner.plan(
        _config(),
        context={"user_message": "那10月呢", "history": [
            {"role": "user", "content": "比较9月和10月"},
            {"role": "assistant", "content": "先看整体情况"},
        ], "inherited_constraints": {"compare_month": "2026-09"}},
        requires_evidence=True, allow_proposals=False,
    ))
    assert plan.goal == "分析两个月消费"
    assert captured["user_message"] == "那10月呢"
    assert len(captured["history"]) == 2
    assert "get_financial_overview" in {item["name"] for item in captured["available_tools"]}
    assert "propose_manual_transaction" not in {item["name"] for item in captured["available_tools"]}


def test_chat_service_routes_complex_natural_language_through_agent_runtime(agent_db, monkeypatch):
    conversation = chat_service.create_conversation()
    called = {}

    class StubRuntime:
        async def run(self, **kwargs):
            called.update(kwargs)
            return "已根据本地账本完成多维分析。", []

    monkeypatch.setattr(chat_service, "get_secret_config", lambda: _config())
    monkeypatch.setattr(chat_service, "_get_agent_runtime", lambda: StubRuntime())
    result = asyncio.run(chat_service.send_message(
        conversation["id"], "分析一下我9月和10月的消费习惯，并看看商户变化。", {}
    ))
    assert called["requires_evidence"] is True
    assert called["allow_proposals"] is False
    assert called["user_message"].startswith("分析一下")
    assert result["assistant_message"] == "已根据本地账本完成多维分析。"
    assert [item["role"] for item in result["messages"]] == ["user", "assistant"]


def test_runtime_completes_plan_execute_verify_synthesize_and_persists(agent_db):
    rows = [
        Row("alipay", "支付宝", "2026-10-02 12:00:00", "支出", 28.0,
            counterparty="咖啡店", item="拿铁", tx_type="餐饮", tx_no="agent-e2e-1"),
        Row("alipay", "支付宝", "2026-10-03 12:00:00", "收入", 100.0,
            counterparty="兼职", item="兼职收入", tx_type="收入", tx_no="agent-e2e-2"),
    ]
    import_rows(rows, "alipay", "agent-e2e.csv", batch_id="agent-e2e")
    ledger_service.sync_live_book()
    conversation = chat_service.create_conversation()

    class FakePlanner:
        def __init__(self):
            self.calls = 0

        async def plan(self, config, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return AgentPlan(goal="分析2026年10月收支和分类", intent="monthly_analysis", steps=[
                    AgentStep(id="overview", tool="get_financial_overview", purpose="查询10月收支总览", arguments={"month": "2026-10"}),
                    AgentStep(id="categories", tool="get_category_breakdown", purpose="查询10月支出分类", arguments={"month": "2026-10", "direction": "支出"}),
                ])
            return AgentPlan(goal="分析2026年10月收支和分类", intent="monthly_analysis", steps=[], replan_after_execution=False)

    class FakeSynthesizer:
        async def synthesize(self, config, **kwargs):
            return "2026-10 支出总额为¥28.00，共1笔。", ["E001", "E002"]

    from backend.ai.agent.runtime import AgentRuntime
    runtime = AgentRuntime(
        registry=chat_service._TOOL_REGISTRY,
        task_store=AgentTaskStore(),
        planner=FakePlanner(),
        synthesizer=FakeSynthesizer(),
        amount_validator=lambda answer, evidence: not chat_service._has_unverified_money_amount(answer, evidence),
    )
    answer, actions = asyncio.run(runtime.run(
        config=_config(),
        conversation_id=conversation["id"],
        user_message="分析一下2026年10月的收支和分类情况",
        history=[{"role": "user", "content": "分析一下2026年10月的收支和分类情况"}],
        context={"_resolved_month": "2026-10"},
        requires_evidence=True,
        allow_proposals=False,
    ))
    task = AgentTaskStore().public_view(conversation["id"])
    assert "¥28.00" in answer
    assert "数据依据：本地账本查询" in answer
    assert not actions
    assert task["status"] == "completed"
    assert task["completed_steps"] == 2
    assert task["total_steps"] == 2
    assert all(step["status"] == "succeeded" for step in task["steps"])


def test_runtime_saves_only_explicit_long_term_preference_requests(agent_db):
    conversation = chat_service.create_conversation()

    class PreferencePlanner:
        async def plan(self, config, **kwargs):
            return AgentPlan(
                goal="以后默认按支出分析",
                needs_clarification=True,
                clarification_question="你想分析哪个月份？",
                preference_updates={"default_direction": "支出"},
            )

    from backend.ai.agent.runtime import AgentRuntime
    store = AgentTaskStore()
    runtime = AgentRuntime(
        registry=chat_service._TOOL_REGISTRY, task_store=store, planner=PreferencePlanner(),
    )
    asyncio.run(runtime.run(
        config=_config(), conversation_id=conversation["id"],
        user_message="以后默认按支出分析",
        history=[{"role": "user", "content": "以后默认按支出分析"}],
        context={}, requires_evidence=True, allow_proposals=False,
    ))
    assert store.get_preferences()["default_direction"] == "支出"


def test_runtime_pauses_for_clarification_and_saves_question(agent_db):
    conversation = chat_service.create_conversation()

    class ClarifyingPlanner:
        async def plan(self, config, **kwargs):
            return AgentPlan(
                goal="分析消费习惯", needs_clarification=True,
                clarification_question="你想分析哪个月份或时间范围？",
            )

    from backend.ai.agent.runtime import AgentRuntime
    runtime = AgentRuntime(
        registry=chat_service._TOOL_REGISTRY,
        task_store=AgentTaskStore(),
        planner=ClarifyingPlanner(),
    )
    answer, _ = asyncio.run(runtime.run(
        config=_config(), conversation_id=conversation["id"],
        user_message="帮我分析一下消费习惯", history=[{"role": "user", "content": "帮我分析一下消费习惯"}],
        context={}, requires_evidence=True, allow_proposals=False,
    ))
    task = AgentTaskStore().public_view(conversation["id"])
    assert answer == "你想分析哪个月份或时间范围？"
    assert task["status"] == "waiting_for_user"
    assert task["phase"] == "waiting_for_user"
    assert task["clarification_question"] == answer


def test_conversation_detail_exposes_public_agent_progress_only(agent_db):
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "分析账单", {})
    store.add_steps(task["id"], 1, [{
        "id": "s1", "tool": "get_financial_overview", "purpose": "查询月度总览",
        "arguments": {"month": "2026-10"}, "depends_on": [],
    }])
    detail = chat_service.get_conversation(conversation["id"])
    assert detail["agent_task"]["id"] == task["id"]
    assert detail["agent_task"]["steps"][0]["purpose"] == "查询月度总览"
    assert "arguments" not in detail["agent_task"]["steps"][0]


def test_agent_progress_http_endpoint_returns_public_checkpoint(agent_db):
    from fastapi.testclient import TestClient
    from backend.main import app

    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "分析10月", {})
    store.add_steps(task["id"], 1, [{
        "id": "overview", "tool": "get_financial_overview",
        "purpose": "查询10月总览", "arguments": {"month": "2026-10"}, "depends_on": [],
    }])
    with TestClient(app) as client:
        response = client.get(
            f"/api/ai/chat/conversations/{conversation['id']}/agent-tasks/latest"
        )
    assert response.status_code == 200
    body = response.json()["agent_task"]
    assert body["id"] == task["id"]
    assert body["steps"][0]["purpose"] == "查询10月总览"
    assert "arguments" not in body["steps"][0]


def test_latest_agent_task_endpoint_service_rejects_unknown_conversation(agent_db):
    with pytest.raises(chat_service.AIChatError, match="没有找到"):
        chat_service.get_latest_agent_task("chat_does_not_exist")
