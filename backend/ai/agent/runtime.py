"""Single Main Agent orchestration: context -> plan -> validate -> execute -> verify -> synthesize."""
from __future__ import annotations

import json
from typing import Any

from ..providers import AIProviderError
from .context_manager import CONTEXT_MANAGER
from .executor import Executor, ExecutionReport
from .models import AgentPlan
from .plan_validator import PlanValidationError, PlanValidator
from .planner import Planner
from .synthesizer import ResponseSynthesizer
from .task_state import TASK_STORE
from .verifier import Verifier


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _step_signature(step) -> str:
    return f"{step.tool}:{_json(step.arguments)}"


class AgentRuntime:
    def __init__(
        self,
        *,
        registry,
        task_store=TASK_STORE,
        context_manager=CONTEXT_MANAGER,
        planner: Planner | None = None,
        executor: Executor | None = None,
        verifier: Verifier | None = None,
        synthesizer: ResponseSynthesizer | None = None,
        amount_validator=None,
    ) -> None:
        self.registry = registry
        self.task_store = task_store
        self.context_manager = context_manager
        self.planner = planner or Planner(registry)
        self.executor = executor or Executor(registry, task_store)
        self.verifier = verifier or Verifier(registry, amount_validator=amount_validator)
        self.synthesizer = synthesizer or ResponseSynthesizer()
        self.validator = PlanValidator(registry)

    async def run(
        self,
        *,
        config,
        conversation_id: str,
        user_message: str,
        history: list[dict[str, Any]],
        context: dict[str, str],
        requires_evidence: bool,
        allow_proposals: bool,
    ) -> tuple[str, list[dict[str, Any]]]:
        assembled = self.context_manager.build(
            conversation_id=conversation_id,
            user_message=user_message,
            history=history,
            page_context=context,
        )
        snapshot_before_planning = None
        if requires_evidence and hasattr(self.task_store, "ledger_snapshot_fingerprint"):
            snapshot_before_planning = self.task_store.ledger_snapshot_fingerprint()

        task_context = {
            "page_context": context,
            "user_message_id": self.task_store.last_user_message_id(conversation_id)
            if hasattr(self.task_store, "last_user_message_id") else None,
            "inherited_constraints": assembled.get("inherited_constraints") or {},
            "inherited_step_scopes": assembled.get("inherited_step_scopes") or [],
            "is_follow_up": bool(assembled.get("is_follow_up")),
            "ledger_snapshot_fingerprint": snapshot_before_planning,
        }
        task = self.task_store.create_or_resume(
            conversation_id,
            str((assembled.get("active_task") or {}).get("goal") or user_message),
            task_context,
        )
        task_id = str(task["id"])
        generation = int(task.get("generation") or 1)
        self.task_store.update_task(
            task_id,
            status="planning",
            phase="planning",
            progress_message="正在理解目标并制定查询计划…",
        )

        try:
            plan = await self.planner.plan(
                config,
                context=assembled,
                requires_evidence=requires_evidence,
                allow_proposals=allow_proposals,
            )
            try:
                self.validator.validate(
                    plan,
                    requires_evidence=requires_evidence,
                    allow_proposals=allow_proposals,
                )
            except PlanValidationError as exc:
                # One constrained repair attempt is cheaper and safer than silently
                # executing an invalid model plan or falling back to keyword routing.
                self.task_store.update_task(
                    task_id,
                    phase="validating",
                    progress_message="正在检查计划参数并请求模型修正…",
                )
                plan = await self.planner.plan(
                    config,
                    context=assembled,
                    requires_evidence=requires_evidence,
                    allow_proposals=allow_proposals,
                    validation_feedback=str(exc),
                )
                self.validator.validate(
                    plan,
                    requires_evidence=requires_evidence,
                    allow_proposals=allow_proposals,
                )

            if plan.preference_updates and any(
                marker in user_message for marker in ("以后", "今后", "下次", "记住", "默认")
            ):
                self.task_store.save_preferences(plan.preference_updates)
                assembled["long_term_preferences"] = self.task_store.get_preferences()

            if plan.needs_clarification:
                question = str(plan.clarification_question or "请补充一下查询范围。").strip()
                self.task_store.update_task(
                    task_id,
                    status="waiting_for_user",
                    phase="waiting_for_user",
                    goal=plan.goal or user_message,
                    intent=plan.intent,
                    plan_json=_json(plan.model_dump()),
                    clarification_question=question,
                    progress_message="需要你补充一项信息后才能继续。",
                )
                return question, []

            self.task_store.update_task(
                task_id,
                status="running",
                phase="executing",
                goal=plan.goal or user_message,
                intent=plan.intent,
                plan_json=_json(plan.model_dump()),
                progress_message=f"计划已确认，准备执行 {len(plan.steps)} 个查询步骤…",
                clarification_question=None,
            )
            snapshot_before_execution = snapshot_before_planning
            if requires_evidence and hasattr(self.task_store, "ledger_snapshot_fingerprint"):
                snapshot_before_execution = self.task_store.ledger_snapshot_fingerprint()
                if snapshot_before_planning != snapshot_before_execution:
                    # The ledger changed while the model was planning. Do not reuse
                    # old checkpoints; execute a fresh, internally consistent pass.
                    task_context["ledger_snapshot_fingerprint"] = snapshot_before_execution
                    task_context["checkpoint_reuse_allowed"] = False
                    saved_context = {}
                    try:
                        saved_context = json.loads(task.get("context_json") or "{}")
                    except (TypeError, ValueError):
                        saved_context = {}
                    if not isinstance(saved_context, dict):
                        saved_context = {}
                    saved_context.update(task_context)
                    self.task_store.update_task(task_id, context_json=_json(saved_context))

            checkpoint_results = self.task_store.reusable_checkpoint_results(
                task_id, generation, plan.steps
            )
            if snapshot_before_planning != snapshot_before_execution:
                checkpoint_results = {}
            # Previous completed task: exact tool+arguments, verified read-only and
            # same ledger fingerprint only. This is never cross-conversation reuse.
            previous_task_id = str((assembled.get("previous_task") or {}).get("id") or "")
            if (previous_task_id and previous_task_id != task_id
                and snapshot_before_execution
                and snapshot_before_planning == snapshot_before_execution
                and hasattr(self.task_store, "reusable_previous_steps")):
                previous_results = self.task_store.reusable_previous_steps(
                    previous_task_id, snapshot_before_execution, plan.steps, self.registry
                )
                checkpoint_results = {**previous_results, **checkpoint_results}
            self.task_store.add_steps(
                task_id, generation, [step.model_dump() for step in plan.steps]
            )
            checkpoint_executions = []
            for step in plan.steps:
                if step.id in checkpoint_results:
                    checkpoint_result = checkpoint_results[step.id]
                    self.task_store.update_step(
                        task_id, generation, step.id, status="succeeded", result=checkpoint_result
                    )
                    checkpoint_executions.append({
                        "step_id": step.id, "tool": step.tool,
                        "arguments": step.arguments, "status": "succeeded",
                        "result": checkpoint_result, "duration_ms": 0, "checkpoint_reused": True,
                    })

            report = await self.executor.execute(
                task_id=task_id,
                generation=generation,
                steps=[step for step in plan.steps if step.id not in checkpoint_results],
                conversation_id=conversation_id,
                context=self._agent_tool_context(context),
                allow_proposals=allow_proposals,
                initial_results=checkpoint_results,
            )
            report.executions.extend(checkpoint_executions)

            # Adaptive Plan-and-Execute pass: after seeing real tool results, let
            # the planner decide whether any additional query is actually needed.
            if plan.replan_after_execution and report.executions:
                self.task_store.update_task(
                    task_id,
                    phase="planning",
                    progress_message="首轮查询完成，正在判断是否还需要补充数据…",
                )
                execution_summary = self._execution_summary(report)
                followup = await self.planner.plan(
                    config,
                    context=assembled,
                    requires_evidence=requires_evidence,
                    allow_proposals=allow_proposals,
                    execution_summary=execution_summary,
                    existing_plan=plan.model_dump(),
                )
                if followup.needs_clarification:
                    question = str(followup.clarification_question or "还需要你补充一项信息。").strip()
                    self.task_store.update_task(
                        task_id,
                        status="waiting_for_user",
                        phase="waiting_for_user",
                        clarification_question=question,
                        progress_message="查询后发现还需要你确认一个条件。",
                        result_json=_json({"executions": execution_summary}),
                    )
                    return question, report.created_actions

                if followup.steps:
                    combined_steps = self._merge_steps(plan.steps, followup.steps)
                    combined_plan = AgentPlan(
                        goal=followup.goal or plan.goal,
                        intent=followup.intent or plan.intent,
                        needs_clarification=False,
                        steps=combined_steps,
                        replan_after_execution=False,
                    )
                    self.validator.validate(
                        combined_plan,
                        requires_evidence=requires_evidence,
                        allow_proposals=allow_proposals,
                    )
                    new_steps = [
                        step for step in followup.steps
                        if step.id not in report.results
                        and _step_signature(step) not in {_step_signature(old) for old in plan.steps}
                    ]
                    if new_steps:
                        self.task_store.update_task(
                            task_id,
                            phase="executing",
                            plan_json=_json(combined_plan.model_dump()),
                            progress_message=f"根据首轮结果补充 {len(new_steps)} 个查询步骤…",
                        )
                        self.task_store.add_steps(
                            task_id, generation, [step.model_dump() for step in new_steps]
                        )
                        second_report = await self.executor.execute(
                            task_id=task_id,
                            generation=generation,
                            steps=new_steps,
                            conversation_id=conversation_id,
                            context=self._agent_tool_context(context),
                            allow_proposals=allow_proposals,
                            initial_results={
                                key: value for key, value in report.results.items()
                                if key not in report.failed_steps
                            },
                        )
                        report.results.update(second_report.results)
                        report.executions.extend(second_report.executions)
                        report.created_actions.extend(second_report.created_actions)
                        report.failed_steps.extend(second_report.failed_steps)
                        plan = combined_plan

            if requires_evidence and snapshot_before_execution and hasattr(
                self.task_store, "ledger_snapshot_fingerprint"
            ):
                snapshot_after_execution = self.task_store.ledger_snapshot_fingerprint()
                if snapshot_after_execution != snapshot_before_execution:
                    answer = (
                        "分析过程中账本数据发生了变化。为避免混合使用不同时间点的数据，"
                        "本次结论已停止生成，请重新发起查询。"
                    )
                    self.task_store.update_task(
                        task_id,
                        status="failed",
                        phase="failed",
                        result_json=_json({"snapshot_changed": True}),
                        progress_message="检测到账本在执行期间发生变化，已停止生成结论。",
                        completed_at=self._now(),
                    )
                    return answer, report.created_actions

            proposal_steps = [
                step for step in plan.steps
                if (self.registry.get(step.tool) and self.registry.get(step.tool).proposal_only)
            ]
            if allow_proposals and not report.created_actions:
                answer = "我没有成功创建待确认提案，因此没有修改账本。请检查必要信息后重试。"
                self.task_store.update_task(
                    task_id,
                    status="failed",
                    phase="failed",
                    result_json=_json({
                        "proposal_failed": True,
                        "proposal_steps_planned": [step.id for step in proposal_steps],
                        "failed_steps": report.failed_steps,
                    }),
                    progress_message="待确认提案未能创建，Ledger 未被修改。",
                    completed_at=self._now(),
                )
                return answer, []

            self.task_store.update_task(
                task_id,
                phase="verifying",
                progress_message="查询已完成，正在核验数据库证据和统计范围…",
                plan_json=_json(plan.model_dump()),
            )
            verification = self.verifier.verify(
                plan=plan,
                executions=report.executions,
                requires_evidence=requires_evidence,
            )
            self._attach_step_evidence(task_id, generation, verification.evidence)

            has_read_only_evidence = any(
                item.get("read_only")
                and item.get("state") in {"verified", "no_data", "partial"}
                for item in verification.evidence
            )
            if requires_evidence and not has_read_only_evidence:
                answer = (
                    "我没有取得足够的本地账本证据，因此不能给出金额或交易结论。"
                    "请检查账单数据是否已导入，或稍后重试。"
                )
                self.task_store.update_task(
                    task_id,
                    status="failed",
                    phase="failed",
                    result_json=_json({"evidence": verification.evidence, "issues": verification.issues}),
                    progress_message="未取得可用的财务证据，已停止生成结论。",
                    completed_at=self._now(),
                )
                return answer, report.created_actions

            self.task_store.update_task(
                task_id,
                phase="synthesizing",
                progress_message="证据核验完成，正在整理分析结论…",
            )
            answer, used_evidence_ids = await self.synthesizer.synthesize(
                config,
                user_message=user_message,
                goal=plan.goal or user_message,
                plan=plan.model_dump(),
                verification=verification.model_dump(),
            )

            def validate_synthesized_answer(candidate: str, evidence_ids: list[str]):
                valid = {
                    str(item.get("evidence_id")) for item in verification.usable_results
                    if item.get("evidence_id")
                }
                invalid = [item for item in evidence_ids if item not in valid]
                selected = [
                    item for item in verification.usable_results
                    if str(item.get("evidence_id")) in set(evidence_ids)
                ]
                if requires_evidence and not selected:
                    return False, "回答生成器没有引用任何本轮已核验的财务证据。", selected
                if invalid:
                    return False, "回答生成器引用了无效证据编号：" + ", ".join(invalid[:5]), selected
                ok, issue = self.verifier.verify_answer(
                    candidate, selected, used_evidence_ids=evidence_ids,
                )
                return ok, issue, selected

            answer_ok, answer_issue, selected_evidence = validate_synthesized_answer(
                answer, used_evidence_ids
            )
            if not answer_ok:
                # One bounded repair gives the model the exact verifier failure,
                # while still restricting it to the same verified evidence set.
                answer, used_evidence_ids = await self.synthesizer.synthesize(
                    config,
                    user_message=user_message,
                    goal=plan.goal or user_message,
                    plan=plan.model_dump(),
                    verification=verification.model_dump(),
                    validation_feedback=answer_issue,
                )
                answer_ok, answer_issue, selected_evidence = validate_synthesized_answer(
                    answer, used_evidence_ids
                )
            if not answer_ok:
                # Never drop valid query evidence solely because prose introduced
                # an unsupported number. Return a deterministic, strictly bounded
                # partial answer rather than hallucinating or hiding all data.
                answer = self.verifier.factual_fallback(verification, plan)
                fallback_sources = self._source_line(verification.usable_results)
                if fallback_sources:
                    answer += "\n\n" + fallback_sources
                self.task_store.update_task(
                    task_id,
                    status="completed" if verification.usable_results else "failed",
                    phase="completed" if verification.usable_results else "failed",
                    result_json=_json({
                        "evidence": verification.evidence,
                        "issues": [*verification.issues, answer_issue],
                        "partial": True,
                        "synthesis_fallback": True,
                    }),
                    progress_message="已保留可核实的部分数据，未采用缺少证据的模型结论。",
                    completed_at=self._now(),
                )
                return answer, report.created_actions

            valid_ids = {str(item.get("evidence_id")) for item in verification.usable_results}
            used_ids = [item for item in used_evidence_ids if item in valid_ids]
            source_line = self._source_line(selected_evidence)
            if source_line:
                answer = answer.rstrip() + "\n\n" + source_line
            self.task_store.update_task(
                task_id,
                status="completed",
                phase="completed",
                result_json=_json({
                    "evidence": verification.evidence,
                    "issues": verification.issues,
                    "partial": verification.partial,
                    "used_evidence_ids": used_ids,
                }),
                progress_message=(
                    "分析已完成，部分数据未能取得。" if verification.partial
                    else "分析已完成，所有引用数据均已核验。"
                ),
                completed_at=self._now(),
            )
            return answer, report.created_actions
        except AIProviderError:
            self.task_store.update_task(
                task_id,
                status="failed",
                phase="failed",
                progress_message="模型服务暂时无法完成任务，请检查配置后重试。",
                completed_at=self._now(),
            )
            raise
        except Exception as exc:
            self.task_store.update_task(
                task_id,
                status="failed",
                phase="failed",
                result_json=_json({"error": str(exc)[:400]}),
                progress_message="任务执行遇到错误，未生成未经核验的财务结论。",
                completed_at=self._now(),
            )
            raise

    @staticmethod
    def _agent_tool_context(context: dict[str, str]) -> dict[str, str]:
        # The plan, not page metadata or single-month local routing, owns each
        # tool's query scope. Keep explicit user multi-month guard intact.
        return {
            **{key: value for key, value in context.items()
               if not key.startswith("_resolved_")},
            "_agent_managed": "true",
        }

    @staticmethod
    def _merge_steps(existing, followup):
        merged = list(existing)
        ids = {step.id for step in existing}
        signatures = {_step_signature(step) for step in existing}
        existing_by_id = {step.id: step for step in existing}
        for step in followup:
            signature = _step_signature(step)
            if step.id in ids:
                if _step_signature(existing_by_id[step.id]) == signature:
                    continue
                raise PlanValidationError(f"补充计划重复使用 step id：{step.id}")
            if signature in signatures:
                continue
            merged.append(step)
            ids.add(step.id)
            signatures.add(signature)
        return merged

    @staticmethod
    def _execution_summary(report: ExecutionReport) -> dict[str, Any]:
        return {
            "steps": [
                {
                    "step_id": item.get("step_id"),
                    "tool": item.get("tool"),
                    "arguments": item.get("arguments"),
                    "status": item.get("status"),
                    "result": AgentRuntime._compact_result(item.get("result") or {}),
                }
                for item in report.executions
            ],
            "completed_step_ids": [
                key for key in report.results if key not in report.failed_steps
            ],
            "failed_step_ids": list(report.failed_steps),
            "failed_count": len(report.failed_steps),
        }

    def _attach_step_evidence(self, task_id: str, generation: int, evidence: list[dict[str, Any]]) -> None:
        evidence_by_step = {str(item.get("step_id")): item for item in evidence}
        for step in self.task_store.get_steps(task_id):
            if int(step.get("generation") or 0) != generation:
                continue
            item = evidence_by_step.get(str(step.get("step_key")))
            if item:
                self.task_store.attach_evidence(task_id, generation, str(step["step_key"]), item)

    @staticmethod
    def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
        compact = dict(result)
        for key in ("transactions", "categories", "merchants", "large_spending_candidates",
                    "possible_duplicate_candidates", "category_spikes"):
            value = compact.get(key)
            if isinstance(value, list) and len(value) > 40:
                compact[key] = value[:40]
                compact[f"_{key}_truncated"] = len(value) - 40
        for key, value in list(compact.items()):
            if isinstance(value, str) and len(value) > 3000:
                compact[key] = value[:3000] + "…[truncated]"
        return compact

    @staticmethod
    def _source_line(evidence: list[dict[str, Any]]) -> str:
        labels = {
            "get_financial_overview": "月度收支总览",
            "get_category_breakdown": "分类分布",
            "get_category_transactions": "分类交易明细",
            "get_top_merchants": "商户排名",
            "search_transactions": "交易检索",
            "get_recent_transactions": "最近交易",
            "get_transactions_by_date": "指定日期流水",
            "analyze_spending_trends": "月度趋势对比",
            "detect_spending_anomalies": "异常候选检查",
            "get_category_tree": "账本分类树",
        }
        scopes = {
            "month": "月份", "compare_month": "对比月份", "direction": "方向",
            "keyword": "筛选词", "category": "分类", "date": "日期",
        }
        sources = []
        for item in evidence:
            if item.get("read_only") is False:
                continue
            scope = item.get("scope") or {}
            scope_text = "、".join(
                f"{scopes.get(key, key)}{value}" for key, value in scope.items()
                if key in scopes and value not in (None, "")
            )
            tool = str(item.get("tool") or "")
            sources.append(labels.get(tool, tool) + (f"（{scope_text}）" if scope_text else ""))
        return "数据依据：本地账本查询；" + "；".join(sources) if sources else ""

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


__all__ = ["AgentRuntime"]
