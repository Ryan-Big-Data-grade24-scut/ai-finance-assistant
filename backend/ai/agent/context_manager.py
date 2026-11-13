"""Conversation and task context assembly for Penny.

The manager deliberately separates:
- current-turn page metadata;
- temporary constraints inherited from the active task;
- per-step scopes (which may contain different months/categories);
- long-term user preferences.

A multi-month plan must never be flattened into one global ``month`` value.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from .task_state import TASK_STORE


CONSTRAINT_KEYS = {
    "month", "compare_month", "date", "date_from", "date_to",
    "start_date", "end_date", "period", "year", "quarter",
    "direction", "keyword", "category", "subcat", "pay_method",
    "merchant", "merchant_name", "counterparty", "tx_id",
    "limit", "min_amount", "max_amount", "sort_by", "time_range",
}


class ContextManager:
    def __init__(self, task_store=TASK_STORE, max_history: int = 16) -> None:
        self.task_store = task_store
        self.max_history = max_history

    def build(
        self,
        *,
        conversation_id: str,
        user_message: str,
        history: list[dict[str, Any]],
        page_context: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        latest = self.task_store.latest(conversation_id)
        if latest and latest.get("status") in {"completed", "failed"}:
            prior_task_context = self._json(latest.get("context_json"))
            bound_id = prior_task_context.get("user_message_id")
            if isinstance(bound_id, int) and hasattr(self.task_store, "previous_user_message_id"):
                previous_user_id = self.task_store.previous_user_message_id(conversation_id)
                if previous_user_id is not None and previous_user_id != bound_id:
                    # A deterministic fast-path turn intervened; the older Agent
                    # task is no longer the active referent.
                    latest = None
        latest_plan = self._json(latest.get("plan_json")) if latest else {}
        prior_context = self._json(latest.get("context_json")) if latest else {}

        plan_constraints, conflicting_constraints, plan_scopes = self._extract_constraint_context(latest_plan)
        is_follow_up = self._is_follow_up(
            user_message=user_message,
            history=history,
            latest_status=str(latest.get("status") or "") if latest else "",
        )
        inherited_constraints = dict(prior_context.get("inherited_constraints") or {}) if is_follow_up else {}
        # A previous single-value constraint becomes unsafe if the latest plan
        # intentionally used different values in separate steps.
        for key in conflicting_constraints:
            inherited_constraints.pop(key, None)
        if is_follow_up:
            inherited_constraints.update(plan_constraints)

        stored_scopes = prior_context.get("inherited_step_scopes")
        inherited_step_scopes = (
            plan_scopes or (stored_scopes if isinstance(stored_scopes, list) else [])
        ) if is_follow_up else []

        active_task = None
        if latest and latest.get("status") in {"waiting_for_user", "interrupted"}:
            current_generation = int(latest.get("generation") or 1)
            checkpoint_rows = [
                row for row in self.task_store.get_steps(str(latest.get("id")))
                if int(row.get("generation") or 0) == current_generation
            ]
            active_task = {
                "id": latest.get("id"),
                "goal": latest.get("goal"),
                "intent": latest.get("intent"),
                "status": latest.get("status"),
                "clarification_question": latest.get("clarification_question"),
                "previous_plan": self._plan_summary(latest_plan),
                "generation": int(latest.get("generation") or 1),
                "checkpoint_steps": [
                    {
                        "id": row.get("step_key"),
                        "tool": row.get("tool_name"),
                        "purpose": row.get("purpose"),
                        "status": row.get("status"),
                        "result_summary": self._step_result_summary(row),
                    }
                    for row in checkpoint_rows
                    if row.get("status") in {"succeeded", "failed"}
                ][:12],
            }

        previous_task = None
        if latest and not active_task and is_follow_up:
            latest_steps = self.task_store.get_steps(str(latest.get("id")))
            previous_task = {
                "id": latest.get("id"),
                "goal": latest.get("goal"),
                "intent": latest.get("intent"),
                "status": latest.get("status"),
                "previous_plan": self._plan_summary(latest_plan),
                "result_summary": self._result_summary(self._json(latest.get("result_json"))),
                "verified_step_scopes": [
                    {
                        "tool": row.get("tool_name"),
                        "arguments": row.get("arguments"),
                        "state": (row.get("evidence") or {}).get("state"),
                        "summary": self._step_result_summary(row),
                    }
                    for row in latest_steps
                    if row.get("status") == "succeeded"
                    and (row.get("evidence") or {}).get("state") in {"verified", "no_data", "partial"}
                ][:12],
            }

        return {
            "conversation_id": conversation_id,
            "current_date": date.today().isoformat(),
            "is_follow_up": is_follow_up,
            "user_message": user_message,
            "history": [
                {"role": item.get("role"), "content": str(item.get("content") or "")[:4000]}
                for item in history[-self.max_history:]
                if item.get("role") in {"user", "assistant"}
            ],
            "page_context": dict(page_context or {}),
            "long_term_preferences": self.task_store.get_preferences(),
            "active_task": active_task,
            "previous_task": previous_task,
            "inherited_constraints": inherited_constraints,
            # Preserve distinct step scopes for multi-month / multi-category tasks.
            # The planner must not collapse these into a single global argument.
            "inherited_step_scopes": inherited_step_scopes[:12],
        }

    @staticmethod
    def _is_follow_up(
        *, user_message: str, history: list[dict[str, Any]], latest_status: str
    ) -> bool:
        if latest_status in {"waiting_for_user", "interrupted", "planning", "running"}:
            return True
        text = str(user_message or "").strip()
        if not text:
            return False
        markers = (
            "那", "呢", "这个", "那个", "其中", "继续", "再", "刚才", "上面", "前面",
            "接着", "同样", "还有", "除此之外", "按商户", "按分类", "只看", "如果",
            "换成", "改为", "这笔", "这些", "这个结果", "刚刚的", "上一个",
        )
        if any(marker in text for marker in markers):
            return True
        # A short request without its own object is a reference to the preceding
        # financial answer, not an independent fresh query. The LLM still decides
        # the intent and plan; this only supplies the missing referent.
        if history and latest_status == "completed" and re.fullmatch(
            r"(?:帮我|给我|请|再)?(?:分析|看看|总结|解释)(?:一下|下|这个|这些|上面的结果|具体一点)?[。！？?]*",
            text,
        ):
            return True
        # Very short elliptical turns are usually a response to the assistant's
        # immediately preceding clarification or a continuation of its result.
        if len(text) <= 12 and history:
            last_assistant = next(
                (str(item.get("content") or "") for item in reversed(history)
                 if item.get("role") == "assistant"),
                "",
            )
            if last_assistant and any(token in last_assistant for token in (
                "哪个", "哪一个", "补充", "确认", "范围", "月份", "方向", "支出还是收入",
            )):
                return True
        return False

    @staticmethod
    def _json(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}

    @classmethod
    def _extract_constraint_context(
        cls, plan: dict[str, Any]
    ) -> tuple[dict[str, Any], set[str], list[dict[str, Any]]]:
        values: dict[str, list[Any]] = {}
        scopes: list[dict[str, Any]] = []
        steps = plan.get("steps") if isinstance(plan, dict) else []
        if not isinstance(steps, list):
            return {}, set(), []

        for step in steps:
            if not isinstance(step, dict):
                continue
            args = step.get("arguments")
            if not isinstance(args, dict):
                continue
            scope = {
                key: value for key, value in args.items()
                if key in CONSTRAINT_KEYS and value not in (None, "")
                and not (isinstance(value, dict) and "$ref" in value)
            }
            if not scope:
                continue
            scopes.append({
                "step_id": str(step.get("id") or ""),
                "tool": str(step.get("tool") or ""),
                "arguments": scope,
            })
            for key, value in scope.items():
                bucket = values.setdefault(key, [])
                if value not in bucket:
                    bucket.append(value)

        unique = {key: bucket[0] for key, bucket in values.items() if len(bucket) == 1}
        conflicts = {key for key, bucket in values.items() if len(bucket) > 1}
        return unique, conflicts, scopes

    @staticmethod
    def _plan_summary(plan: dict[str, Any]) -> list[dict[str, Any]]:
        steps = plan.get("steps") if isinstance(plan, dict) else []
        if not isinstance(steps, list):
            return []
        return [
            {
                "tool": str(step.get("tool") or ""),
                "arguments": {
                    key: value for key, value in (step.get("arguments") or {}).items()
                    if key in CONSTRAINT_KEYS and isinstance(value, (str, int, float, bool))
                },
                "purpose": str(step.get("purpose") or "")[:180],
            }
            for step in steps if isinstance(step, dict)
        ][:12]

    @staticmethod
    def _step_result_summary(row: dict[str, Any]) -> dict[str, Any]:
        result = row.get("result")
        if not isinstance(result, dict):
            return {"status": row.get("status")}
        summary: dict[str, Any] = {"status": row.get("status")}
        for key in ("month", "compare_month", "date", "count", "total_amount", "has_data", "ledger_complete"):
            value = result.get(key)
            if isinstance(value, (str, int, float, bool, type(None))):
                summary[key] = value
        for key in ("transactions", "categories", "merchants"):
            if isinstance(result.get(key), list):
                summary[f"{key}_count"] = len(result[key])
        return summary

    @staticmethod
    def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
        evidence = result.get("evidence")
        if not isinstance(evidence, list):
            return {}
        return {
            "evidence_count": len(evidence),
            "sources": [
                {
                    "tool": item.get("tool"),
                    "scope": item.get("scope"),
                    "state": item.get("state"),
                }
                for item in evidence if isinstance(item, dict)
            ][:12],
        }


CONTEXT_MANAGER = ContextManager()
