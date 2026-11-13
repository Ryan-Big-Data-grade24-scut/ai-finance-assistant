"""SQLite persistence for Agent tasks, checkpoints, and public progress state."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ... import db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None, fallback: Any):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class AgentTaskStore:
    def previous_user_message_id(self, conversation_id: str) -> int | None:
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM ai_chat_messages WHERE conversation_id=? "
                "AND role='user' ORDER BY id DESC LIMIT 1 OFFSET 1", (conversation_id,),
            ).fetchone()
            return int(row["id"]) if row else None
        finally:
            conn.close()

    def last_user_message_id(self, conversation_id: str) -> int | None:
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM ai_chat_messages WHERE conversation_id=? "
                "AND role='user' ORDER BY id DESC LIMIT 1", (conversation_id,),
            ).fetchone()
            return int(row["id"]) if row else None
        finally:
            conn.close()

    def ledger_snapshot_fingerprint(self) -> str | None:
        """Return a content fingerprint for data that can affect Agent finance tools.

        Checkpoints are reusable only when this fingerprint is unchanged. The
        immutable raw transaction layer is represented through v_tx, while the
        supporting category/history tables are hashed directly. No financial
        values are persisted in the task metadata; only the SHA-256 digest is.
        """
        conn = db.get_conn()
        try:
            digest = hashlib.sha256()
            digest.update(b"penny-ledger-snapshot-v1\0")

            # v_tx is the canonical projection used by the finance query tools.
            cursor = conn.execute("SELECT * FROM v_tx ORDER BY id")
            for row in cursor:
                digest.update(_dump(list(row)).encode("utf-8"))
                digest.update(b"\n")

            # These tables can affect category trees and history-classification tools
            # even when they do not change a transaction row in v_tx.
            tables = (
                "ledger_nodes", "ledger_books", "ledger_legs", "ledger_placements",
                "ai_classification_proposals", "ai_classification_suggestions",
                "ai_classification_reviews", "ai_classification_rules",
                "ai_history_learning_sessions", "ai_history_learning_messages",
                "ai_import_sessions", "ai_import_messages",
            )
            for table in tables:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    continue
                digest.update(table.encode("utf-8"))
                digest.update(b"\0")
                cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
                while True:
                    rows = cursor.fetchmany(500)
                    if not rows:
                        break
                    for row in rows:
                        digest.update(_dump(list(row)).encode("utf-8"))
                        digest.update(b"\n")
            return digest.hexdigest()
        except Exception:
            # A missing/migrating schema must disable checkpoint reuse, never
            # silently fall back to trusting potentially stale financial results.
            return None
        finally:
            conn.close()

    def get_preferences(self, scope_key: str = "local") -> dict[str, Any]:
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT preferences_json FROM ai_agent_preferences WHERE scope_key=?",
                (scope_key,),
            ).fetchone()
            return _load(row["preferences_json"], {}) if row else {}
        finally:
            conn.close()

    def save_preferences(self, updates: dict[str, Any], scope_key: str = "local") -> dict[str, Any]:
        allowed = {
            "default_direction": {"支出", "收入", "全部"},
            "default_period": {"current_month", "last_month", "last_30_days", "last_90_days"},
            "analysis_detail": {"concise", "detailed"},
            "language": {"zh-CN", "en"},
            "preferred_currency": {"CAD", "CNY", "USD"},
        }
        clean: dict[str, Any] = {}
        for key, value in (updates or {}).items():
            if key in allowed and isinstance(value, str) and value in allowed[key]:
                clean[key] = value
        if not clean:
            return self.get_preferences(scope_key)
        current = self.get_preferences(scope_key)
        merged = {**current, **clean}
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO ai_agent_preferences(scope_key,preferences_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(scope_key) DO UPDATE SET preferences_json=excluded.preferences_json,updated_at=excluded.updated_at",
                (scope_key, _dump(merged), _now()),
            )
            conn.commit()
        finally:
            conn.close()
        return merged

    def latest(self, conversation_id: str) -> dict[str, Any] | None:
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM ai_agent_tasks WHERE conversation_id=? "
                "ORDER BY updated_at DESC,created_at DESC,id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if row and row["status"] in {"planning", "running"}:
                try:
                    updated = datetime.fromisoformat(str(row["updated_at"]))
                    if (datetime.now(timezone.utc) - updated).total_seconds() > 120:
                        conn.execute(
                            "UPDATE ai_agent_tasks SET status='interrupted',phase='failed',"
                            "progress_message='上次执行已超时或进程中断，可以在继续对话时恢复。',updated_at=? WHERE id=?",
                            (_now(), row["id"]),
                        )
                        conn.commit()
                        row = conn.execute("SELECT * FROM ai_agent_tasks WHERE id=?", (row["id"],)).fetchone()
                except (TypeError, ValueError):
                    pass
            return dict(row) if row else None
        finally:
            conn.close()

    def get(self, task_id: str) -> dict[str, Any] | None:
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT * FROM ai_agent_tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def create_or_resume(
        self,
        conversation_id: str,
        goal: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            latest = conn.execute(
                "SELECT * FROM ai_agent_tasks WHERE conversation_id=? "
                "ORDER BY updated_at DESC,created_at DESC,id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            now = _now()
            if latest and latest["status"] in {"waiting_for_user", "interrupted"}:
                task_id = str(latest["id"])
                generation = int(latest["generation"] or 1) + 1
                prior_context = _load(latest["context_json"], {})
                previous_fingerprint = prior_context.get("ledger_snapshot_fingerprint")
                current_fingerprint = context.get("ledger_snapshot_fingerprint")
                has_snapshot_metadata = (
                    "ledger_snapshot_fingerprint" in prior_context
                    or "ledger_snapshot_fingerprint" in context
                )
                # Older direct callers that do not participate in the Agent
                # Runtime keep the historical exact-argument checkpoint behavior.
                # Runtime calls always pass the key, including None, and therefore
                # fail closed when a snapshot cannot be calculated.
                checkpoint_reuse_allowed = (
                    bool(previous_fingerprint and current_fingerprint and previous_fingerprint == current_fingerprint)
                    if has_snapshot_metadata else True
                )
                merged_context = {
                    **prior_context,
                    **context,
                    "resume_from_status": latest["status"],
                    "checkpoint_reuse_allowed": checkpoint_reuse_allowed,
                }
                conn.execute(
                    "UPDATE ai_agent_tasks SET status='planning',phase='planning',generation=?,"
                    "context_json=?,clarification_question=NULL,progress_message=?,updated_at=? WHERE id=?",
                    (generation, _dump(merged_context), "正在恢复任务并检查已完成的执行步骤…", now, task_id),
                )
                conn.commit()
                resumed_row = conn.execute("SELECT * FROM ai_agent_tasks WHERE id=?", (task_id,)).fetchone()
                return dict(resumed_row) if resumed_row else {}

            task_id = f"agt_{uuid4().hex[:24]}"
            parent_id = str(latest["id"]) if latest else None
            conn.execute(
                "INSERT INTO ai_agent_tasks"
                "(id,conversation_id,parent_task_id,status,phase,goal,generation,context_json,progress_message,created_at,updated_at) "
                "VALUES(?,?,?,'planning','understanding',?,1,?,?,?,?)",
                (task_id, conversation_id, parent_id, goal[:500], _dump(context), "正在理解你的问题…", now, now),
            )
            conn.commit()
            created_row = conn.execute("SELECT * FROM ai_agent_tasks WHERE id=?", (task_id,)).fetchone()
            return dict(created_row) if created_row else {}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {
            "status", "phase", "goal", "intent", "context_json", "plan_json",
            "result_json", "clarification_question", "progress_message", "completed_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = _now()
        assignments = ",".join(f"{key}=?" for key in updates)
        conn = db.get_conn()
        try:
            conn.execute(
                f"UPDATE ai_agent_tasks SET {assignments} WHERE id=?",
                (*updates.values(), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def add_steps(self, task_id: str, generation: int, steps: list[dict[str, Any]]) -> None:
        if not steps:
            return
        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for step in steps:
                conn.execute(
                    "INSERT OR IGNORE INTO ai_agent_task_steps"
                    "(task_id,generation,step_key,tool_name,purpose,arguments_json,depends_on_json,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'pending',?)",
                    (
                        task_id, generation, str(step["id"]), str(step["tool"]),
                        str(step.get("purpose") or ""), _dump(step.get("arguments") or {}),
                        _dump(step.get("depends_on") or []), _now(),
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def update_step(
        self,
        task_id: str,
        generation: int,
        step_key: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        conn = db.get_conn()
        try:
            conn.execute(
                "UPDATE ai_agent_task_steps SET status=?,result_json=?,error_text=?,evidence_json=?,"
                "started_at=CASE WHEN ?='running' AND started_at IS NULL THEN ? ELSE started_at END,"
                "completed_at=CASE WHEN ? IN ('succeeded','failed','skipped') THEN ? ELSE completed_at END "
                "WHERE task_id=? AND generation=? AND step_key=?",
                (
                    status, _dump(result) if result is not None else None, error,
                    _dump(evidence) if evidence is not None else None,
                    status, _now(), status, _now(), task_id, generation, step_key,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def attach_evidence(
        self, task_id: str, generation: int, step_key: str, evidence: dict[str, Any]
    ) -> None:
        conn = db.get_conn()
        try:
            conn.execute(
                "UPDATE ai_agent_task_steps SET evidence_json=? "
                "WHERE task_id=? AND generation=? AND step_key=?",
                (_dump(evidence), task_id, generation, step_key),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_interrupted_tasks(self) -> int:
        """Mark tasks left in planning/running state by a previous process as resumable."""
        conn = db.get_conn()
        try:
            cursor = conn.execute(
                "UPDATE ai_agent_tasks SET status='interrupted',phase='failed',"
                "progress_message='上次执行被中断，可以在继续对话时恢复。',updated_at=? "
                "WHERE status IN ('planning','running')",
                (_now(),),
            )
            conn.commit()
            return int(cursor.rowcount or 0)
        finally:
            conn.close()

    def reusable_checkpoint_results(self, task_id: str, generation: int, steps) -> dict[str, dict[str, Any]]:
        """Reuse exact completed steps only when the ledger snapshot is unchanged."""
        task = self.get(task_id)
        task_context = _load(task.get("context_json"), {}) if task else {}
        if task_context.get("checkpoint_reuse_allowed") is False:
            return {}
        rows = self.get_steps(task_id)
        candidates = [row for row in rows if int(row.get("generation") or 0) < generation and row.get("status") == "succeeded"]
        reused: dict[str, dict[str, Any]] = {}
        for step in steps:
            for row in reversed(candidates):
                if (
                    row.get("step_key") == step.id
                    and row.get("tool_name") == step.tool
                    and row.get("arguments") == step.arguments
                    and isinstance(row.get("result"), dict)
                    and not row["result"].get("error")
                ):
                    reused[step.id] = row["result"]
                    break
        return reused

    def get_steps(self, task_id: str) -> list[dict[str, Any]]:
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM ai_agent_task_steps WHERE task_id=? ORDER BY generation,id",
                (task_id,),
            ).fetchall()
            return [
                {
                    **dict(row),
                    "arguments": _load(row["arguments_json"], {}),
                    "depends_on": _load(row["depends_on_json"], []),
                    "result": _load(row["result_json"], None),
                    "evidence": _load(row["evidence_json"], None),
                }
                for row in rows
            ]
        finally:
            conn.close()

    @staticmethod
    def _public_evidence(result_payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Expose provenance metadata only; never expose raw tool results or amounts."""
        raw = result_payload.get("evidence") if isinstance(result_payload, dict) else None
        if not isinstance(raw, list):
            return []
        safe: list[dict[str, Any]] = []
        for item in raw[:20]:
            if not isinstance(item, dict):
                continue
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            safe_scope = {
                str(key): value for key, value in scope.items()
                if key in {
                    "month", "compare_month", "date", "date_from", "date_to",
                    "direction", "keyword", "category",
                }
                and isinstance(value, (str, int, float, bool))
            }
            summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
            safe_summary = {
                str(key): value for key, value in summary.items()
                if key in {"count", "transactions_count", "categories_count", "merchants_count", "ledger_complete"}
                and isinstance(value, (str, int, float, bool))
            }
            safe.append({
                "evidence_id": str(item.get("evidence_id") or ""),
                "tool": str(item.get("tool") or ""),
                "scope": safe_scope,
                "state": str(item.get("state") or "unknown"),
                "reason": str(item.get("reason") or "")[:240] or None,
                "summary": safe_summary,
            })
        return safe

    def reusable_previous_steps(
        self, task_id: str, fingerprint: str | None, planned_steps, registry
    ) -> dict[str, dict[str, Any]]:
        """Reuse only verified, read-only, exact-scope evidence on the same ledger snapshot.

        Never reuse failed writes, model text, unverified results, or stale data.
        """
        previous = self.get(task_id)
        if not previous or previous.get("status") != "completed" or not fingerprint:
            return {}
        previous_context = _load(previous.get("context_json"), {})
        if previous_context.get("ledger_snapshot_fingerprint") != fingerprint:
            return {}
        candidates = self.get_steps(task_id)
        reuse = {}
        for step in planned_steps:
            spec = registry.get(step.tool)
            if not spec or not spec.read_only or step.depends_on:
                continue
            for row in reversed(candidates):
                evidence = row.get("evidence") or {}
                if (row.get("status") == "succeeded"
                    and row.get("tool_name") == step.tool
                    and row.get("arguments") == step.arguments
                    and evidence.get("state") in {"verified", "no_data", "partial"}
                    and isinstance(row.get("result"), dict)
                    and not row["result"].get("error")):
                    reuse[step.id] = row["result"]
                    break
        return reuse

    def public_view(self, conversation_id: str, conn=None) -> dict[str, Any] | None:
        owns_connection = conn is None
        if conn is None:
            conn = db.get_conn()
        try:
            task_row = conn.execute(
                "SELECT * FROM ai_agent_tasks WHERE conversation_id=? "
                "ORDER BY updated_at DESC,created_at DESC,id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if not task_row:
                return None
            task = dict(task_row)
            task_context = _load(task.get("context_json"), {})
            bound_message_id = task_context.get("user_message_id")
            if isinstance(bound_message_id, int):
                last_user = conn.execute(
                    "SELECT id FROM ai_chat_messages WHERE conversation_id=? "
                    "AND role='user' ORDER BY id DESC LIMIT 1", (conversation_id,),
                ).fetchone()
                if not last_user or int(last_user["id"]) != bound_message_id:
                    # The latest turn was a fast-path query, not this old Agent task.
                    return None
            rows = conn.execute(
                "SELECT * FROM ai_agent_task_steps WHERE task_id=? ORDER BY generation,id",
                (task["id"],),
            ).fetchall()
            current_generation = int(task.get("generation") or 1)
            steps = [
                dict(row) for row in rows
                if int(row["generation"] or 0) == current_generation
            ]
            return {
                "id": task["id"],
                "status": task["status"],
                "phase": task["phase"],
                "goal": task["goal"],
                "generation": int(task.get("generation") or 1),
                "progress_message": task["progress_message"],
                "clarification_question": task["clarification_question"],
                "created_at": task["created_at"],
                "updated_at": task["updated_at"],
                "completed_at": task["completed_at"],
                "evidence": self._public_evidence(_load(task.get("result_json"), {})),
                "steps": [
                    {
                        "id": item["step_key"],
                        "tool": item["tool_name"],
                        "purpose": item["purpose"],
                        "status": item["status"],
                        "error": item["error_text"],
                        "generation": item["generation"],
                    }
                    for item in steps
                ],
                "completed_steps": sum(1 for item in steps if item["status"] == "succeeded"),
                "total_steps": len(steps),
            }
        finally:
            if owns_connection:
                conn.close()



TASK_STORE = AgentTaskStore()
