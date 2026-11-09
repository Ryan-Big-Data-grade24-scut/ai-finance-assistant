"""Bounded Plan executor with dependency ordering and parallel read-only batches."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from .models import AgentStep
from .task_state import TASK_STORE
from .tool_registry import ToolRegistry, is_reference


@dataclass
class ExecutionReport:
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    executions: list[dict[str, Any]] = field(default_factory=list)
    created_actions: list[dict[str, Any]] = field(default_factory=list)
    failed_steps: list[str] = field(default_factory=list)


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        task_store=TASK_STORE,
        *,
        max_tool_calls: int = 12,
        max_concurrency: int = 4,
        tool_timeout_seconds: float = 30.0,
        total_timeout_seconds: float = 100.0,
    ) -> None:
        self.registry = registry
        self.task_store = task_store
        self.max_tool_calls = max_tool_calls
        self.max_concurrency = max_concurrency
        self.tool_timeout_seconds = tool_timeout_seconds
        self.total_timeout_seconds = total_timeout_seconds

    async def execute(
        self,
        *,
        task_id: str,
        generation: int,
        steps: list[AgentStep],
        conversation_id: str,
        context: dict[str, str],
        allow_proposals: bool,
        initial_results: dict[str, dict[str, Any]] | None = None,
    ) -> ExecutionReport:
        report = ExecutionReport(results=dict(initial_results or {}))
        pending = {step.id: step for step in steps if step.id not in report.results}
        started = time.monotonic()
        calls = 0
        semaphore = asyncio.Semaphore(self.max_concurrency)

        while pending:
            if time.monotonic() - started > self.total_timeout_seconds:
                for step in pending.values():
                    self.task_store.update_step(
                        task_id, generation, step.id, status="skipped", error="任务总执行时间超过限制。"
                    )
                    report.failed_steps.append(step.id)
                break
            if calls >= self.max_tool_calls:
                for step in pending.values():
                    self.task_store.update_step(
                        task_id, generation, step.id, status="skipped", error="工具调用总数超过限制。"
                    )
                    report.failed_steps.append(step.id)
                break

            blocked = [
                step for step in pending.values()
                if any(dependency in report.failed_steps for dependency in step.depends_on)
            ]
            for step in blocked:
                pending.pop(step.id, None)
                self.task_store.update_step(
                    task_id, generation, step.id, status="skipped", error="依赖步骤执行失败，已跳过。"
                )
                report.failed_steps.append(step.id)

            # Compute ready steps after removing blocked dependants. A failed
            # step still has an error result in report.results, so checking only
            # dependency presence would incorrectly execute its dependants.
            ready = [
                step for step in pending.values()
                if all(dependency in report.results for dependency in step.depends_on)
            ]
            if not ready:
                if pending:
                    for step in pending.values():
                        self.task_store.update_step(
                            task_id, generation, step.id, status="skipped", error="依赖关系无法满足。"
                        )
                        report.failed_steps.append(step.id)
                    break
                continue

            ready = ready[: self.max_tool_calls - calls]
            ready_specs = [self.registry.get(step.tool) for step in ready]
            can_parallelize = len(ready) > 1 and all(spec and spec.read_only for spec in ready_specs)

            # Mark the whole parallel batch before dispatching the tool calls.
            # Otherwise SQLite status writes inside each coroutine can delay the
            # second dispatch until the first (fast) query is already finished.
            if can_parallelize:
                self.task_store.update_task(
                    task_id, phase="executing",
                    progress_message=f"正在并行查询 {len(ready)} 个只读数据源…",
                )
                for step in ready:
                    self.task_store.update_step(
                        task_id, generation, step.id, status="running"
                    )

            async def run_one(step: AgentStep) -> tuple[str, dict[str, Any]]:
                async with semaphore:
                    if not can_parallelize:
                        self.task_store.update_task(
                            task_id, phase="executing",
                            progress_message=f"正在执行：{step.purpose or step.tool}…",
                        )
                        self.task_store.update_step(
                            task_id, generation, step.id, status="running"
                        )
                    began = time.monotonic()
                    resolved_args = step.arguments
                    try:
                        resolved_args = self._resolve_references(step.arguments, report.results)
                        result = await asyncio.wait_for(
                            asyncio.to_thread(
                                self.registry.execute,
                                step.tool,
                                resolved_args,
                                conversation_id,
                                context,
                                allow_proposals=allow_proposals,
                            ),
                            timeout=self.tool_timeout_seconds,
                        )
                        duration_ms = int((time.monotonic() - began) * 1000)
                        status = "failed" if result.get("error") else "succeeded"
                        self.task_store.update_step(
                            task_id, generation, step.id, status=status,
                            result=result, error=result.get("error"),
                        )
                        return step.id, {
                            "step_id": step.id,
                            "tool": step.tool,
                            "arguments": resolved_args,
                            "status": status,
                            "result": result,
                            "duration_ms": duration_ms,
                        }
                    except asyncio.TimeoutError:
                        duration_ms = int((time.monotonic() - began) * 1000)
                        error = f"工具执行超过 {self.tool_timeout_seconds:g} 秒。"
                    except Exception as exc:
                        duration_ms = int((time.monotonic() - began) * 1000)
                        error = str(exc)[:400]
                    self.task_store.update_step(
                        task_id, generation, step.id, status="failed", error=error,
                    )
                    return step.id, {
                        "step_id": step.id,
                        "tool": step.tool,
                        "arguments": resolved_args,
                        "status": "failed",
                        "result": {"error": error},
                        "error": error,
                        "duration_ms": duration_ms,
                    }

            if can_parallelize:
                # Warm the event loop's thread pool before dispatching financial
                # reads. Without this, thread startup can serialize very short
                # queries even though they were submitted through gather().
                await asyncio.gather(*(asyncio.to_thread(lambda: None) for _ in ready))
                completed = await asyncio.gather(*(run_one(step) for step in ready))
            else:
                completed = []
                for step in ready:
                    completed.append(await run_one(step))

            for step_id, execution in completed:
                report.executions.append(execution)
                report.results[step_id] = execution.get("result") or {}
                if execution["status"] == "failed":
                    report.failed_steps.append(step_id)
                result = execution.get("result") or {}
                if result.get("status") == "pending_confirmation":
                    report.created_actions.append(result)
                pending.pop(step_id, None)
                calls += 1

        return report

    @classmethod
    def _resolve_references(cls, value: Any, results: dict[str, dict[str, Any]]) -> Any:
        if is_reference(value):
            path = str(value["$ref"]).split(".")
            step_id = path.pop(0)
            if step_id not in results:
                raise ValueError(f"引用的步骤 {step_id} 尚未产生结果。")
            current: Any = results[step_id]
            for part in path:
                if isinstance(current, list) and part.isdigit():
                    index = int(part)
                    if index >= len(current):
                        raise ValueError(f"结果引用 {value['$ref']} 的数组索引越界。")
                    current = current[index]
                elif isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    raise ValueError(f"结果引用 {value['$ref']} 不存在。")
            return current
        if isinstance(value, dict):
            return {key: cls._resolve_references(item, results) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._resolve_references(item, results) for item in value]
        return value
