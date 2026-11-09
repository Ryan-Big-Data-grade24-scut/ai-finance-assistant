"""Deterministic policy and DAG validation for LLM-generated plans."""
from __future__ import annotations

import json
from typing import Any

from .models import AgentPlan
from .tool_registry import ToolRegistry, ToolRegistryError, reference_paths


class PlanValidationError(ValueError):
    pass


class PlanValidator:
    def __init__(self, registry: ToolRegistry, max_steps: int = 12) -> None:
        self.registry = registry
        self.max_steps = max_steps

    def validate(
        self,
        plan: AgentPlan,
        *,
        requires_evidence: bool,
        allow_proposals: bool,
    ) -> AgentPlan:
        if len(plan.steps) > self.max_steps:
            raise PlanValidationError(f"计划步骤超过上限 {self.max_steps}。")
        if plan.needs_clarification:
            if plan.steps:
                raise PlanValidationError("需要澄清的计划不能包含工具步骤。")
            return plan
        if requires_evidence and not plan.steps:
            raise PlanValidationError("财务事实问题没有规划任何本地证据查询。")

        by_id = {step.id: step for step in plan.steps}
        if len(by_id) != len(plan.steps):
            raise PlanValidationError("计划中存在重复的 step id。")

        signatures: set[str] = set()
        for step in plan.steps:
            spec = self.registry.get(step.tool)
            if spec is None:
                raise PlanValidationError(f"计划使用了不存在的工具：{step.tool}")
            if spec.proposal_only and not allow_proposals:
                raise PlanValidationError(f"工具 {step.tool} 只能用于用户明确要求的写入提案。")
            if requires_evidence and not spec.read_only and not (spec.proposal_only and allow_proposals):
                raise PlanValidationError("财务查询任务只能执行只读工具及明确授权的待确认提案工具。")
            try:
                self.registry.validate_arguments(step.tool, step.arguments, allow_refs=True)
                self._validate_period_arguments(step.arguments)
            except (ToolRegistryError, ValueError) as exc:
                raise PlanValidationError(str(exc)) from exc
            signature = f"{step.tool}:{json.dumps(step.arguments, ensure_ascii=False, sort_keys=True)}"
            if signature in signatures:
                raise PlanValidationError(f"计划包含重复查询步骤：{step.tool}")
            signatures.add(signature)

            if len(set(step.depends_on)) != len(step.depends_on):
                raise PlanValidationError(f"步骤 {step.id} 包含重复的依赖项。")
            for dependency in step.depends_on:
                if dependency not in by_id:
                    raise PlanValidationError(f"步骤 {step.id} 依赖不存在的步骤 {dependency}。")
                if dependency == step.id:
                    raise PlanValidationError(f"步骤 {step.id} 不能依赖自身。")
            if spec.proposal_only and requires_evidence:
                dependency_specs = [self.registry.get(by_id[item].tool) for item in step.depends_on]
                if not any(item and item.read_only for item in dependency_specs):
                    raise PlanValidationError("查询与写入混合任务中，待确认提案必须依赖至少一个成功的只读查询步骤。")
            if step.tool == "propose_transaction_reclassification" and not any(
                by_id[item].tool == "search_transactions" for item in step.depends_on
            ):
                raise PlanValidationError("单笔重分类提案必须直接依赖 search_transactions 的真实交易结果。")
            for ref in reference_paths(step.arguments):
                source_step = ref.split(".", 1)[0]
                if source_step not in step.depends_on:
                    raise PlanValidationError(
                        f"步骤 {step.id} 引用了 {source_step}，但没有声明对应 depends_on。"
                    )
                if source_step not in by_id:
                    raise PlanValidationError(f"步骤 {step.id} 引用了不存在的步骤 {source_step}。")

        self._validate_acyclic(plan)
        if requires_evidence and not any(self.registry.get(step.tool).read_only for step in plan.steps):
            raise PlanValidationError("财务事实问题必须包含至少一个只读证据查询。")
        return plan

    @staticmethod
    def _validate_period_arguments(arguments: dict[str, Any]) -> None:
        import re
        from datetime import date

        for key in ("month", "compare_month"):
            value = arguments.get(key)
            if value not in (None, "") and not (
                isinstance(value, str) and re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", value)
            ):
                raise ValueError(f"{key} 必须是有效的 YYYY-MM 月份。")
        for key in ("date", "date_from", "date_to"):
            value = arguments.get(key)
            if value not in (None, ""):
                if not isinstance(value, str) or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
                    raise ValueError(f"{key} 必须是 YYYY-MM-DD 日期。")
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError(f"{key} 不是有效的日历日期。") from exc

    @staticmethod
    def _validate_acyclic(plan: AgentPlan) -> None:
        graph = {step.id: list(step.depends_on) for step in plan.steps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise PlanValidationError("计划依赖关系存在循环。")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph.get(node, []):
                visit(dependency)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)
