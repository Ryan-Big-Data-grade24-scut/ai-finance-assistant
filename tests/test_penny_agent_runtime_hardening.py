from __future__ import annotations

import asyncio
import json
from pathlib import Path

from backend import db
from backend.ai import chat_service
from backend.ai.agent.context_manager import ContextManager
from backend.ai.agent.models import AgentPlan, AgentStep
from backend.ai.agent.runtime import AgentRuntime
from backend.ai.agent.task_state import AgentTaskStore
from backend.ai.agent.tool_registry import ToolRegistry
from backend.ai.agent.verifier import Verifier
from backend.ai.config import AIConfig
from backend.importer import import_rows
from backend.importer.base import Row
from backend.ledger import service as ledger_service


def _config() -> AIConfig:
    return AIConfig(
        "openai_compatible", "https://example.test/v1", "test-model", "test-key",
        False, 0.9, True,
    )


def _init_test_db(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "agent-hardening.db")
    monkeypatch.setenv("AI_FINANCE_MASTER_KEY_FILE", str(tmp_path / "outside" / "master.key"))
    db.init_db(db.DB_PATH)


def test_context_manager_keeps_conflicting_months_per_step_not_global(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "分析9月和10月", {})
    store.update_task(task["id"], plan_json=(
        '{"steps":['
        '{"id":"sep","tool":"get_financial_overview","purpose":"9月","arguments":{"month":"2026-09"}},'
        '{"id":"oct","tool":"get_financial_overview","purpose":"10月","arguments":{"month":"2026-10"}}'
        ']}'
    ))

    context = ContextManager(store).build(
        conversation_id=conversation["id"], user_message="那10月呢", history=[], page_context={},
    )

    assert "month" not in context["inherited_constraints"]
    assert [item["arguments"]["month"] for item in context["inherited_step_scopes"]] == [
        "2026-09", "2026-10",
    ]
    assert context["current_date"]


def test_checkpoint_reuse_is_allowed_only_for_matching_snapshot(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(
        conversation["id"], "分析10月", {"ledger_snapshot_fingerprint": "snapshot-a"}
    )
    step = AgentStep(
        id="overview", tool="get_financial_overview", arguments={"month": "2026-10"}
    )
    store.add_steps(task["id"], 1, [step.model_dump()])
    store.update_step(
        task["id"], 1, "overview", status="succeeded",
        result={"has_data": True, "month": "2026-10", "expense": {"amount": 88}},
    )
    store.update_task(task["id"], status="interrupted")

    resumed = store.create_or_resume(
        conversation["id"], "继续", {"ledger_snapshot_fingerprint": "snapshot-b"}
    )
    assert resumed["generation"] == 2
    assert store.reusable_checkpoint_results(task["id"], 2, [step]) == {}


def test_checkpoint_reuse_accepts_same_snapshot_and_exact_arguments(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(
        conversation["id"], "分析10月", {"ledger_snapshot_fingerprint": "snapshot-a"}
    )
    step = AgentStep(
        id="overview", tool="get_financial_overview", arguments={"month": "2026-10"}
    )
    store.add_steps(task["id"], 1, [step.model_dump()])
    store.update_step(
        task["id"], 1, "overview", status="succeeded",
        result={"has_data": True, "month": "2026-10", "expense": {"amount": 88}},
    )
    store.update_task(task["id"], status="interrupted")

    resumed = store.create_or_resume(
        conversation["id"], "继续", {"ledger_snapshot_fingerprint": "snapshot-a"}
    )
    assert resumed["generation"] == 2
    reused = store.reusable_checkpoint_results(task["id"], 2, [step])
    assert reused["overview"]["expense"]["amount"] == 88


def test_verifier_does_not_treat_incomplete_empty_ledger_as_no_data():
    registry = ToolRegistry()
    registry.register({
        "name": "read", "description": "read",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }, read_only=True)
    result = Verifier(registry).verify(
        plan=AgentPlan(steps=[AgentStep(id="s1", tool="read")]),
        executions=[{
            "step_id": "s1", "tool": "read", "arguments": {}, "status": "succeeded",
            "result": {"has_data": False, "ledger_complete": False, "accounting_note": "账本投影不完整"},
        }],
        requires_evidence=True,
    )
    assert result.evidence[0]["state"] == "incomplete"
    assert not result.usable_results
    assert not result.verified


def test_verifier_requires_valid_evidence_ids_for_answer():
    registry = ToolRegistry()
    registry.register({
        "name": "read", "description": "read",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }, read_only=True)
    verifier = Verifier(registry)
    evidence = [{
        "evidence_id": "E001", "tool": "read", "result": {"month": "2026-10"},
    }]
    assert verifier.verify_answer(
        "2026-10 的结果见本地账本。", evidence, used_evidence_ids=["E001"]
    ) == (True, None)
    ok, issue = verifier.verify_answer(
        "2026-10 的结果见本地账本。", evidence, used_evidence_ids=["E999"]
    )
    assert not ok and "不存在或未核验" in issue
    ok, issue = verifier.verify_answer(
        "2026-10 的结果见本地账本。", evidence, used_evidence_ids=[]
    )
    assert not ok and "没有声明" in issue


def test_public_agent_evidence_exposes_provenance_but_not_raw_financial_results(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "分析10月", {})
    store.update_task(task["id"], result_json=(
        '{"evidence":[{"evidence_id":"E001","tool":"get_financial_overview",'
        '"scope":{"month":"2026-10"},"state":"verified",'
        '"summary":{"count":4},"result":{"expense":{"amount":9999}}}]}'
    ))
    public = store.public_view(conversation["id"])
    assert public["evidence"][0]["tool"] == "get_financial_overview"
    assert public["evidence"][0]["scope"] == {"month": "2026-10"}
    assert "result" not in public["evidence"][0]
    assert "amount" not in str(public["evidence"])


def test_agent_runtime_replans_after_a_tool_failure_and_returns_partial_result(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    registry = ToolRegistry()
    empty_schema = {"type": "object", "properties": {}, "required": []}
    for name in ("read_failed", "read_ok"):
        registry.register({"name": name, "description": name, "parameters": empty_schema}, read_only=True)

    def handler(name, _args, _conversation_id, _context):
        if name == "read_failed":
            return {"error": "模拟工具暂时失败"}
        return {
            "has_data": True, "month": "2026-10",
            "expense": {"amount": 12, "count": 1},
        }

    registry.set_handler(handler)

    class Planner:
        calls = 0

        async def plan(self, _config, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return AgentPlan(steps=[AgentStep(id="first", tool="read_failed")])
            return AgentPlan(
                steps=[AgentStep(id="supplement", tool="read_ok")],
                replan_after_execution=False,
            )

    class Synthesizer:
        async def synthesize(self, _config, **_kwargs):
            return "2026-10 支出为12元，共1笔。由于另一步查询失败，本次分析不完整。", ["E002"]

    planner = Planner()
    runtime = AgentRuntime(
        registry=registry, task_store=store, planner=planner, synthesizer=Synthesizer(),
    )
    answer, _actions = asyncio.run(runtime.run(
        config=_config(), conversation_id=conversation["id"],
        user_message="分析2026年10月支出情况", history=[], context={},
        requires_evidence=True, allow_proposals=False,
    ))

    task = store.public_view(conversation["id"])
    assert planner.calls == 2
    assert "12元" in answer
    assert "数据依据：本地账本查询" in answer
    assert task["status"] == "completed"
    assert task["evidence"][0]["state"] == "failed"
    assert task["evidence"][1]["state"] == "verified"


def test_agent_router_sends_open_ended_financial_judgement_to_planner(monkeypatch):
    monkeypatch.setattr(chat_service.TASK_STORE, "latest", lambda _conversation_id: None)
    assert chat_service._should_use_agent_runtime("这份账单整体是否合理，有什么问题？", [], "chat_test")
    assert chat_service._should_use_agent_runtime("我最近花钱是不是正常？", [], "chat_test")


def test_independent_new_request_does_not_inherit_previous_temporary_month(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "查询10月", {})
    store.update_task(task["id"], status="completed", plan_json=(
        '{"steps":[{"id":"oct","tool":"get_financial_overview",'
        '"arguments":{"month":"2026-10"}}]}'
    ))

    context = ContextManager(store).build(
        conversation_id=conversation["id"], user_message="查看11月收入", history=[], page_context={},
    )

    assert context["is_follow_up"] is False
    assert context["inherited_constraints"] == {}
    assert context["inherited_step_scopes"] == []
    assert context["previous_task"] is None


def test_ledger_snapshot_fingerprint_changes_after_override_update(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    rows = [Row(
        "alipay", "支付宝", "2026-10-02 12:00:00", "支出", 28.0,
        counterparty="咖啡店", item="拿铁", tx_type="餐饮", tx_no="fingerprint-1",
    )]
    import_rows(rows, "alipay", "fingerprint.csv", batch_id="fingerprint")
    ledger_service.sync_live_book()
    store = AgentTaskStore()
    before = store.ledger_snapshot_fingerprint()
    assert before and before == store.ledger_snapshot_fingerprint()

    conn = db.get_conn()
    try:
        tx_id = conn.execute(
            "SELECT id FROM transactions_raw WHERE tx_no='fingerprint-1'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO tx_overrides(tx_id,manual_category,updated_by) VALUES(?,?,?)",
            (tx_id, "运动", "ui"),
        )
        conn.commit()
    finally:
        conn.close()

    after = store.ledger_snapshot_fingerprint()
    assert after and after != before


def test_write_proposal_tools_are_hidden_unless_user_explicitly_requests_mutation(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    visible_by_default = {item["function"]["name"] for item in chat_service._openai_tools()}
    visible_for_mutation = {
        item["function"]["name"]
        for item in chat_service._openai_tools(allow_proposals=True)
    }
    assert "propose_manual_transaction" not in visible_by_default
    assert "propose_manual_transaction" in visible_for_mutation
    assert not chat_service._is_mutating_financial_request("如何修改这笔交易的分类？")
    assert not chat_service._is_mutating_financial_request("分类规则怎么设置？")
    assert chat_service._is_mutating_financial_request("帮我把星巴克分类为餐饮")
    try:
        chat_service._execute_tool(
            "propose_category_rule",
            {"match_field": "counterparty", "pattern": "星巴克", "target_path": "餐饮", "reason": "用户要求"},
            "chat_test", {},
        )
    except chat_service.AIChatError as exc:
        assert "不能创建写入提案" in str(exc)
    else:
        raise AssertionError("默认只读工具执行器不应创建写入提案")


def test_mixed_read_and_write_plan_requires_real_read_evidence(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    from backend.ai.agent.plan_validator import PlanValidator

    lookup = AgentStep(
        id="lookup", tool="search_transactions",
        arguments={"keyword": "星巴克"}, purpose="先查询真实交易",
    )
    proposal = AgentStep(
        id="proposal", tool="propose_transaction_reclassification",
        arguments={
            "tx_id": {"$ref": "lookup.transactions.0.id"},
            "target_path": "餐饮", "reason": "用户明确要求调整分类",
        },
        depends_on=["lookup"], purpose="创建待确认分类提案",
    )
    plan = AgentPlan(steps=[lookup, proposal], replan_after_execution=False)
    validated = PlanValidator(chat_service._TOOL_REGISTRY).validate(
        plan, requires_evidence=True, allow_proposals=True,
    )
    assert len(validated.steps) == 2


def test_verifier_separates_read_evidence_from_pending_proposal():
    registry = ToolRegistry()
    empty_schema = {"type": "object", "properties": {}, "required": []}
    registry.register({"name": "read", "description": "read", "parameters": empty_schema}, read_only=True)
    registry.register(
        {"name": "proposal", "description": "proposal", "parameters": empty_schema},
        read_only=False, proposal_only=True,
    )
    plan = AgentPlan(steps=[
        AgentStep(id="read_step", tool="read"),
        AgentStep(id="proposal_step", tool="proposal", depends_on=["read_step"]),
    ])
    result = Verifier(registry).verify(
        plan=plan,
        executions=[
            {"step_id": "read_step", "tool": "read", "arguments": {}, "status": "succeeded",
             "result": {"has_data": True, "month": "2026-10", "count": 1}},
            {"step_id": "proposal_step", "tool": "proposal", "arguments": {}, "status": "succeeded",
             "result": {"status": "pending_confirmation", "action_id": "action-1"}},
        ],
        requires_evidence=True,
    )
    assert result.verified
    assert [item["state"] for item in result.evidence] == ["verified", "proposal"]
    assert result.usable_results[1]["proposal_only"] is True


def test_pure_mutation_and_mixed_query_are_distinguished():
    assert chat_service._is_mutating_financial_request("把这笔交易改成餐饮")
    assert not chat_service._is_financial_fact_question("把这笔交易改成餐饮")
    assert chat_service._is_mutating_financial_request("查一下星巴克交易并帮我改成餐饮")
    assert chat_service._is_financial_fact_question("查一下星巴克交易并帮我改成餐饮")


def test_mixed_query_proposal_cannot_run_before_a_read_only_dependency(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    from backend.ai.agent.plan_validator import PlanValidationError, PlanValidator

    plan = AgentPlan(steps=[
        AgentStep(id="lookup", tool="search_transactions", arguments={"keyword": "星巴克"}),
        AgentStep(
            id="proposal", tool="propose_transaction_reclassification",
            arguments={"tx_id": 12, "target_path": "餐饮", "reason": "用户要求"},
        ),
    ])
    try:
        PlanValidator(chat_service._TOOL_REGISTRY).validate(
            plan, requires_evidence=True, allow_proposals=True,
        )
    except PlanValidationError as exc:
        assert "依赖" in str(exc)
    else:
        raise AssertionError("写入提案必须依赖成功的只读查询")


def test_explicit_mutations_enter_agent_but_simple_one_step_reads_can_remain_fast():
    assert not chat_service._should_use_agent_after_fast_paths("查一下星巴克交易")
    assert chat_service._should_use_agent_after_fast_paths("把这笔交易改成餐饮")
    assert not chat_service._should_use_agent_after_fast_paths("如何修改分类规则？")
    assert not chat_service._should_use_agent_after_fast_paths("你好")


def test_single_transaction_reclassification_requires_search_even_for_pure_write(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    from backend.ai.agent.plan_validator import PlanValidationError, PlanValidator

    plan = AgentPlan(steps=[AgentStep(
        id="proposal", tool="propose_transaction_reclassification",
        arguments={"tx_id": 12, "target_path": "餐饮", "reason": "用户要求"},
    )])
    try:
        PlanValidator(chat_service._TOOL_REGISTRY).validate(
            plan, requires_evidence=False, allow_proposals=True,
        )
    except PlanValidationError as exc:
        assert "search_transactions" in str(exc)
    else:
        raise AssertionError("单笔重分类必须先查询真实交易")


def test_executor_skips_dependants_when_a_dependency_fails(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    from backend.ai.agent.executor import Executor

    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "执行依赖测试", {})
    registry = ToolRegistry()
    schema = {"type": "object", "properties": {}, "required": []}
    registry.register({"name": "first", "description": "first", "parameters": schema}, read_only=True)
    registry.register({"name": "second", "description": "second", "parameters": schema}, read_only=True)
    called = []

    def handler(name, _args, _conversation_id, _context):
        called.append(name)
        if name == "first":
            return {"error": "first failed"}
        return {"has_data": True}

    registry.set_handler(handler)
    steps = [
        AgentStep(id="first", tool="first"),
        AgentStep(id="second", tool="second", depends_on=["first"]),
    ]
    store.add_steps(task["id"], 1, [step.model_dump() for step in steps])
    report = asyncio.run(Executor(registry, store).execute(
        task_id=task["id"], generation=1, steps=steps,
        conversation_id=conversation["id"], context={}, allow_proposals=False,
    ))

    assert called == ["first"]
    assert report.failed_steps == ["first", "second"]
    states = {item["step_key"]: item["status"] for item in store.get_steps(task["id"])}
    assert states == {"first": "failed", "second": "skipped"}


def test_verifier_checks_explicitly_quoted_merchant_and_category_names():
    verifier = Verifier(ToolRegistry())
    evidence = [{
        "evidence_id": "E001",
        "tool": "search_transactions",
        "result": {"transactions": [{
            "counterparty": "星巴克", "category": "餐饮", "subcat": "咖啡",
        }]},
    }]
    ok, issue = verifier.verify_answer(
        "商户是「星巴克」，分类为「餐饮」。", evidence, used_evidence_ids=["E001"]
    )
    assert ok and issue is None
    ok, issue = verifier.verify_answer(
        "商户是「虚构商户」，分类为「餐饮」。", evidence, used_evidence_ids=["E001"]
    )
    assert not ok and "不在本轮核验结果中" in issue


def test_interrupted_agent_tasks_are_marked_resumable_on_restart(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "恢复测试", {})
    store.update_task(task["id"], status="running", phase="executing")

    changed = store.mark_interrupted_tasks()

    assert changed == 1
    assert store.get(task["id"])["status"] == "interrupted"


def test_deleting_conversation_cascades_agent_task_and_steps(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    task = store.create_or_resume(conversation["id"], "删除级联测试", {})
    step = AgentStep(id="step", tool="get_category_tree")
    store.add_steps(task["id"], 1, [step.model_dump()])

    assert chat_service.delete_conversation(conversation["id"])
    assert store.get(task["id"]) is None
    assert store.get_steps(task["id"]) == []


def test_runtime_retries_synthesis_once_when_evidence_ids_fail_validation(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    conversation = chat_service.create_conversation()
    store = AgentTaskStore()
    registry = ToolRegistry()
    registry.register({
        "name": "read", "description": "read",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }, read_only=True)
    registry.set_handler(lambda *_args: {"has_data": True, "month": "2026-10", "count": 1})

    class Planner:
        async def plan(self, _config, **_kwargs):
            return AgentPlan(
                steps=[AgentStep(id="read_step", tool="read")],
                replan_after_execution=False,
            )

    class Synthesizer:
        calls = 0

        async def synthesize(self, _config, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return "结果已查询。", ["E999"]
            assert kwargs.get("validation_feedback")
            return "结果已查询并核验。", ["E001"]

    synthesizer = Synthesizer()
    runtime = AgentRuntime(
        registry=registry, task_store=store, planner=Planner(), synthesizer=synthesizer,
    )
    answer, _actions = asyncio.run(runtime.run(
        config=_config(), conversation_id=conversation["id"],
        user_message="查询2026年10月的记录", history=[], context={},
        requires_evidence=True, allow_proposals=False,
    ))
    assert synthesizer.calls == 2
    assert "结果已查询并核验" in answer
    assert store.public_view(conversation["id"])["status"] == "completed"


def test_runtime_stops_if_ledger_changes_during_financial_execution(tmp_path, monkeypatch):
    _init_test_db(tmp_path, monkeypatch)
    conversation = chat_service.create_conversation()

    class ChangingSnapshotStore(AgentTaskStore):
        calls = 0

        def ledger_snapshot_fingerprint(self):
            self.calls += 1
            return "snapshot-before" if self.calls <= 2 else "snapshot-after"

    store = ChangingSnapshotStore()
    registry = ToolRegistry()
    registry.register({
        "name": "read", "description": "read",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }, read_only=True)
    registry.set_handler(lambda *_args: {"has_data": True, "month": "2026-10", "count": 1})

    class Planner:
        async def plan(self, _config, **_kwargs):
            return AgentPlan(
                steps=[AgentStep(id="read_step", tool="read")],
                replan_after_execution=False,
            )

    class NeverSynthesize:
        async def synthesize(self, *_args, **_kwargs):
            raise AssertionError("账本快照变化后不得生成财务结论")

    runtime = AgentRuntime(
        registry=registry, task_store=store, planner=Planner(), synthesizer=NeverSynthesize(),
    )
    answer, _actions = asyncio.run(runtime.run(
        config=_config(), conversation_id=conversation["id"],
        user_message="查询2026年10月的记录", history=[], context={},
        requires_evidence=True, allow_proposals=False,
    ))
    task = store.public_view(conversation["id"])
    assert "账本数据发生了变化" in answer
    assert task["status"] == "failed"
    stored = store.get(task["id"])
    assert json.loads(stored["result_json"])["snapshot_changed"] is True
