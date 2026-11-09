"""Regression tests for Penny contextual evidence, scope integrity and task display."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend import db
from backend.ai import chat_service
from backend.ai.agent.context_manager import ContextManager
from backend.ai.agent.models import AgentPlan, AgentStep
from backend.ai.agent.runtime import AgentRuntime
from backend.ai.agent.task_state import AgentTaskStore
from backend.ai.agent.tool_registry import ToolRegistry
from backend.ai.agent.verifier import Verifier
from backend.ai.config import AIConfig


@pytest.fixture
def local_db(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "penny-continuity.db")
    monkeypatch.setenv("AI_FINANCE_MASTER_KEY_FILE", str(tmp_path / "key" / "master.key"))
    db.init_db(db.DB_PATH)
    yield


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register({
        "name": "get_category_transactions", "description": "read",
        "parameters": {
            "type": "object", "required": ["month", "keyword"],
            "properties": {
                "month": {"type": "string"},
                "keyword": {"type": "string"},
                "direction": {"type": "string"},
            },
        },
    }, read_only=True)
    return registry


def _config():
    return AIConfig("openai_compatible", "https://test.invalid/v1", "mock", "mock-key", False, 0.8, True)


def _plan():
    return AgentPlan(
        goal="分析2026年10月餐饮",
        intent="category_analysis",
        replan_after_execution=False,
        steps=[AgentStep(
            id="dining", tool="get_category_transactions", purpose="获取10月餐饮明细",
            arguments={"month": "2026-10", "keyword": "餐饮", "direction": "支出"},
        )],
    )


def _data():
    return {
        "month": "2026-10", "keyword": "餐饮", "direction": "支出",
        "count": 41, "total_amount": 1085.8,
        "transactions": [{"id": 18, "date": "2026-10-04", "amount": 32.6, "category": "社会餐饮"}],
        "truncated": True,
    }


def test_latest_task_is_hidden_after_a_new_fast_path_message(local_db):
    store = AgentTaskStore()
    conv = chat_service.create_conversation()["id"]
    chat_service._store_user_message(conv, "分析上个月")
    first_user_id = store.last_user_message_id(conv)
    task = store.create_or_resume(conv, "分析上个月", {"user_message_id": first_user_id})
    store.update_task(task["id"], status="completed")
    assert store.public_view(conv)["id"] == task["id"]
    chat_service._store_assistant_message(conv, "完成分析")

    # A deterministic query doesn't create a task but it must hide the old panel.
    chat_service._store_user_message(conv, "给我十月份的支出")
    chat_service._store_assistant_message(conv, "10月支出来自本地数据库")
    assert store.public_view(conv) is None
    assert chat_service.get_conversation(conv)["agent_task"] is None


def test_generic_follow_up_exposes_precise_prior_scope_and_evidence(local_db):
    store = AgentTaskStore()
    conv = chat_service.create_conversation()["id"]
    chat_service._store_user_message(conv, "那10月餐饮呢？列出具体交易")
    task = store.create_or_resume(conv, "10月餐饮", {"user_message_id": store.last_user_message_id(conv)})
    plan = _plan()
    store.add_steps(task["id"], 1, [plan.steps[0].model_dump()])
    store.update_step(task["id"], 1, "dining", status="succeeded", result=_data())
    store.attach_evidence(task["id"], 1, "dining", {"state": "verified"})
    store.update_task(task["id"], status="completed", plan_json=plan.model_dump_json())
    chat_service._store_assistant_message(conv, "2026年10月餐饮41笔")
    history = chat_service._store_user_message(conv, "帮我分析一下")
    context = ContextManager(store).build(
        conversation_id=conv, user_message="帮我分析一下", history=history, page_context={},
    )
    assert context["is_follow_up"] is True
    assert context["previous_task"]["id"] == task["id"]
    assert context["previous_task"]["verified_step_scopes"][0]["arguments"]["month"] == "2026-10"
    assert context["previous_task"]["verified_step_scopes"][0]["summary"]["count"] == 41


def test_intervening_fast_path_does_not_inherit_stale_agent_scope(local_db):
    store = AgentTaskStore()
    conv = chat_service.create_conversation()["id"]
    chat_service._store_user_message(conv, "9月餐饮")
    task = store.create_or_resume(conv, "9月餐饮", {"user_message_id": store.last_user_message_id(conv)})
    store.update_task(task["id"], status="completed", plan_json=_plan().model_dump_json())
    chat_service._store_assistant_message(conv, "完成")
    chat_service._store_user_message(conv, "10月收入多少")
    chat_service._store_assistant_message(conv, "查到10月收入")
    history = chat_service._store_user_message(conv, "帮我分析一下")
    context = ContextManager(store).build(
        conversation_id=conv, user_message="帮我分析一下", history=history, page_context={},
    )
    assert context["previous_task"] is None
    assert context["inherited_constraints"] == {}


def test_previous_evidence_is_reused_only_if_verified_and_snapshot_identical(local_db):
    store = AgentTaskStore()
    conv = chat_service.create_conversation()["id"]
    fingerprint = store.ledger_snapshot_fingerprint()
    assert fingerprint
    task = store.create_or_resume(conv, "10月餐饮", {"ledger_snapshot_fingerprint": fingerprint})
    store.add_steps(task["id"], 1, [_plan().steps[0].model_dump()])
    store.update_step(task["id"], 1, "dining", status="succeeded", result=_data())
    store.attach_evidence(task["id"], 1, "dining", {"state": "verified"})
    store.update_task(task["id"], status="completed")
    registry = _registry()
    assert store.reusable_previous_steps(task["id"], fingerprint, _plan().steps, registry)["dining"]["count"] == 41
    assert store.reusable_previous_steps(task["id"], "stale-snapshot", _plan().steps, registry) == {}
    other = _plan().steps[0].model_copy(update={"arguments": {"month": "2026-09", "keyword": "餐饮"}})
    assert store.reusable_previous_steps(task["id"], fingerprint, [other], registry) == {}
    store.attach_evidence(task["id"], 1, "dining", {"state": "failed"})
    assert store.reusable_previous_steps(task["id"], fingerprint, _plan().steps, registry) == {}


def test_verifier_rejects_scope_mismatch_and_conflicting_queries():
    verifier = Verifier(_registry())
    plan = _plan()
    wrong = verifier.verify(plan=plan, requires_evidence=True, executions=[{
        "step_id": "dining", "tool": "get_category_transactions", "status": "succeeded",
        "arguments": plan.steps[0].arguments, "result": {**_data(), "month": "2026-09"},
    }])
    assert wrong.evidence[0]["state"] == "failed"
    assert not wrong.usable_results

    conflict = verifier.verify(plan=plan, requires_evidence=True, executions=[
        {"step_id": "dining", "tool": "get_category_transactions", "status": "succeeded",
         "arguments": plan.steps[0].arguments, "result": _data()},
        {"step_id": "again", "tool": "get_category_transactions", "status": "succeeded",
         "arguments": plan.steps[0].arguments, "result": {**_data(), "count": 0, "transactions": []}},
    ])
    assert not conflict.verified
    assert not conflict.usable_results
    assert all(item["state"] == "conflict" for item in conflict.evidence)


def test_invalid_model_claims_generate_grounded_partial_answer(local_db):
    registry = _registry()
    registry.set_handler(lambda *_: _data())
    conv = chat_service.create_conversation()["id"]
    history = chat_service._store_user_message(conv, "分析一下2026年10月餐饮")

    class FakePlanner:
        async def plan(self, *_args, **_kwargs):
            return _plan()

    class BadSynthesizer:
        async def synthesize(self, *_args, **_kwargs):
            return "2026-10餐饮支出999999元，41笔。", ["E001"]

    runtime = AgentRuntime(
        registry=registry, task_store=AgentTaskStore(), planner=FakePlanner(),
        synthesizer=BadSynthesizer(), amount_validator=lambda answer, evidence: "999999" not in answer,
    )
    answer, _ = asyncio.run(runtime.run(
        config=_config(), conversation_id=conv, user_message="分析一下2026年10月餐饮",
        history=history, context={}, requires_evidence=True, allow_proposals=False,
    ))
    assert "1085.80" in answer
    assert "41 笔" in answer
    assert "999999" not in answer
    assert "已拦截该结论" not in answer
    task = AgentTaskStore().public_view(conv)
    assert task["status"] == "completed"


def test_second_turn_reuses_exact_verified_read_without_second_tool_call(local_db):
    registry = _registry()
    calls = []

    def handler(name, args, *_):
        calls.append((name, args))
        return _data()

    registry.set_handler(handler)
    conv = chat_service.create_conversation()["id"]
    store = AgentTaskStore()

    class FakePlanner:
        async def plan(self, *_args, **_kwargs):
            return _plan()

    class Synthesizer:
        async def synthesize(self, *_args, **_kwargs):
            return "2026-10餐饮有41笔。", ["E001"]

    runtime = AgentRuntime(
        registry=registry, task_store=store, context_manager=ContextManager(store),
        planner=FakePlanner(), synthesizer=Synthesizer(),
    )
    for question in ("那10月餐饮呢？列出具体交易", "帮我分析一下"):
        history = chat_service._store_user_message(conv, question)
        answer, _ = asyncio.run(runtime.run(
            config=_config(), conversation_id=conv, user_message=question, history=history,
            context={}, requires_evidence=True, allow_proposals=False,
        ))
        chat_service._store_assistant_message(conv, answer)
        assert "41笔" in answer
    assert len(calls) == 1
    assert store.public_view(conv)["status"] == "completed"


def test_verifier_rejects_broad_empty_aggregate_against_positive_category_detail():
    registry = _registry()
    registry.register({
        "name": "get_category_breakdown", "description": "breakdown",
        "parameters": {
            "type": "object", "properties": {
                "month": {"type": "string"}, "direction": {"type": "string"},
            }, "required": ["month"],
        },
    }, read_only=True)
    plan = AgentPlan(steps=[
        AgentStep(id="b", tool="get_category_breakdown", arguments={"month": "2026-10", "direction": "支出"}),
        _plan().steps[0],
    ])
    result = Verifier(registry).verify(plan=plan, requires_evidence=True, executions=[
        {"step_id": "b", "tool": "get_category_breakdown", "status": "succeeded",
         "arguments": {"month": "2026-10", "direction": "支出"},
         "result": {"month": "2026-10", "direction": "支出", "has_data": False, "categories": []}},
        {"step_id": "dining", "tool": "get_category_transactions", "status": "succeeded",
         "arguments": _plan().steps[0].arguments, "result": _data()},
    ])
    assert not result.verified
    assert not result.usable_results
    assert all(item["state"] == "conflict" for item in result.evidence)


def test_verifier_rejects_empty_marker_with_positive_transaction_count():
    result = Verifier(_registry()).verify(
        plan=_plan(), requires_evidence=True, executions=[{
            "step_id": "dining", "tool": "get_category_transactions", "status": "succeeded",
            "arguments": _plan().steps[0].arguments,
            "result": {**_data(), "has_data": False},
        }],
    )
    assert result.evidence[0]["state"] == "conflict"
    assert not result.usable_results
