"""历史账单 AI 学习工作流。

只读取既有交易并生成建议；分类变更必须经过分类服务和 Ledger Engine。
历史学习默认不自动落账，用户需要明确选择要应用的交易。
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from .. import db
from ..classification import service as classification_service
from ..classification import store as classification_store
from ..classification.models import ClassificationSuggestion
from ..importer.base import Row
from ..ledger import loader as ledger_loader
from ..ledger import service as ledger_service
from .config import get_secret_config
from .import_classifier import classify_import_batch
from .providers import AIProviderError

logger = logging.getLogger(__name__)

LIVE_BOOK_ID = ledger_service.LIVE_BOOK_ID


class AIHistoryError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _node_paths(nodes: list[dict]) -> dict[str, str]:
    by_id = {str(node["id"]): node for node in nodes}
    cache: dict[str, str] = {}

    def path(node_id: str) -> str:
        if node_id in cache:
            return cache[node_id]
        node = by_id[node_id]
        parent_id = node.get("parent_id")
        parent = by_id.get(str(parent_id)) if parent_id else None
        if not parent_id or (parent and parent.get("kind") == "root"):
            value = str(node["name"])
        else:
            prefix = path(str(parent_id)) if parent else ""
            value = f"{prefix} / {node['name']}" if prefix else str(node["name"])
        cache[node_id] = value
        return value

    for node in nodes:
        if node.get("kind") == "group":
            path(str(node["id"]))
    return cache


def _load_history_inputs() -> dict:
    """读取 live book 中未被 AI 确认或人工分类覆盖的交易。"""
    ledger_service.sync_live_book()
    conn = db.get_conn()
    try:
        book = ledger_loader.read_book(conn, LIVE_BOOK_ID)
        state = ledger_loader.read_state(conn, LIVE_BOOK_ID)
        facts = ledger_loader.raw_transactions(conn, state["members"])
        paths = _node_paths(state["nodes"])
        reviewed_ids = classification_store.reviewed_tx_ids(conn, LIVE_BOOK_ID)
        override_rows = conn.execute(
            "SELECT tx_id,manual_category,manual_subcat FROM tx_overrides "
            "WHERE (manual_category IS NOT NULL AND trim(manual_category) <> '') "
            "OR (manual_subcat IS NOT NULL AND trim(manual_subcat) <> '')"
        ).fetchall()
        overrides_by_id = {int(row["tx_id"]): dict(row) for row in override_rows}
        protected_ids = set(overrides_by_id)
        platform_rows = conn.execute(
            "SELECT id,platform_id FROM transactions_raw"
        ).fetchall()
        platform_by_id = {int(row["id"]): str(row["platform_id"]) for row in platform_rows}

        # 人工覆盖和已确认的分类作为学习样本，但不允许本轮 AI 覆盖它们。
        # `history_evidence` is kept server-side and never asks the model to invent evidence IDs.
        manual_examples = []
        history_evidence_by_tx: dict[int, dict[str, Any]] = {}
        facts_by_id = {int(tx["id"]): tx for tx in facts}
        for tx_id in protected_ids:
            tx = facts_by_id.get(tx_id)
            override = overrides_by_id.get(tx_id)
            if not tx or not override:
                continue
            major = str(override["manual_category"] or "").strip()
            minor = str(override["manual_subcat"] or "").strip()
            if major:
                confirmed_category = f"{major} / {minor}" if minor else major
                manual_examples.append({
                    "counterparty": tx.get("counterparty") or "",
                    "item": tx.get("item") or "",
                    "source": tx.get("source") or "",
                    "direction": tx.get("direction") or "",
                    "confirmed_category": confirmed_category,
                    "count": 1,
                })
                history_evidence_by_tx[tx_id] = {
                    "tx_id": tx_id,
                    "date": tx.get("date") or "",
                    "counterparty": tx.get("counterparty") or "",
                    "item": tx.get("item") or "",
                    "source": tx.get("source") or "",
                    "direction": tx.get("direction") or "",
                    "confirmed_category": confirmed_category,
                    "evidence_type": "人工覆盖",
                }

        history_examples: dict[tuple[str, str, str, str, str], int] = {}
        reviewed = conn.execute(
            "SELECT t.id,t.date,t.counterparty,t.item,t.source,t.direction,r.target_node_id "
            "FROM ai_classification_reviews r "
            "JOIN transactions_raw t ON t.id=r.tx_id "
            "JOIN v_tx visible ON visible.id=t.id "
            "WHERE r.book_id=? ORDER BY r.reviewed_at DESC LIMIT 1000",
            (LIVE_BOOK_ID,),
        ).fetchall()
        for item in reviewed:
            category = paths.get(str(item["target_node_id"]))
            if not category:
                continue
            key = (str(item["counterparty"] or ""), str(item["item"] or ""),
                   str(item["source"] or ""), str(item["direction"] or ""), category)
            history_examples[key] = history_examples.get(key, 0) + 1
            tx_id = int(item["id"])
            # A manual override is the stronger and more recent explicit source.
            if tx_id not in history_evidence_by_tx:
                history_evidence_by_tx[tx_id] = {
                    "tx_id": tx_id,
                    "date": item["date"] or "",
                    "counterparty": item["counterparty"] or "",
                    "item": item["item"] or "",
                    "source": item["source"] or "",
                    "direction": item["direction"] or "",
                    "confirmed_category": category,
                    "evidence_type": "用户确认的 AI 分类",
                }
        reviewed_examples = [
            {"counterparty": key[0], "item": key[1], "source": key[2],
             "direction": key[3], "confirmed_category": key[4], "count": count}
            for key, count in sorted(history_examples.items(), key=lambda pair: (-pair[1], pair[0]))[:200]
        ]
        history_examples_list = reviewed_examples + manual_examples[:300]
        history_evidence = list(history_evidence_by_tx.values())

        rows: list[Row] = []
        tx_ids: list[int] = []
        current_paths: dict[int, str] = {}
        for tx in facts:
            tx_id = int(tx["id"])
            if tx_id in reviewed_ids or tx_id in protected_ids:
                continue
            raw = tx.get("raw_json")
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (TypeError, ValueError):
                    raw = {}
            if not isinstance(raw, dict):
                raw = {}
            rows.append(Row(
                platform_id=platform_by_id.get(tx_id, "unknown"),
                source=str(tx.get("source") or ""),
                tx_time=str(tx.get("tx_time") or ""),
                direction=str(tx.get("direction") or "中性"),
                amount=float(tx.get("amount") or 0),
                pay_method=str(tx.get("pay_method") or ""),
                counterparty=str(tx.get("counterparty") or ""),
                item=str(tx.get("item") or ""),
                tx_type=str(tx.get("tx_type") or ""),
                status=str(tx.get("status") or ""),
                tx_no=str(tx.get("tx_no") or ""),
                merchant_no=str(tx.get("merchant_no") or ""),
                remark=str(tx.get("remark") or ""),
                raw=raw,
            ))
            tx_ids.append(tx_id)
            current_node_id = state["placements"].get(str(tx_id))
            current_paths[tx_id] = paths.get(str(current_node_id), "")

        categories = [
            {"node_id": node_id, "path": path}
            for node_id, path in paths.items()
        ]
        rules = classification_store.list_rules(conn, LIVE_BOOK_ID)
        total = len(state["members"])
        reviewed_count = len(reviewed_ids.intersection(set(state["members"])))
        protected_count = len(protected_ids.intersection(set(state["members"])))
        return {
            "book_seq": int(book["current_seq"]),
            "total": total,
            "reviewed_count": reviewed_count,
            "protected_count": protected_count,
            "categories": categories,
            "rules": rules,
            "history_examples": history_examples_list,
            "history_evidence": history_evidence,
            "rows": rows,
            "tx_ids": tx_ids,
            "current_paths": current_paths,
        }
    finally:
        conn.close()



def _history_evidence_for_row(row: Row, evidence_rows: list[dict] | None) -> list[dict]:
    """Attach only real, user-confirmed same-merchant history as traceable evidence."""
    merchant = " ".join(str(row.counterparty or "").casefold().split())
    item = " ".join(str(row.item or "").casefold().split())
    direction = str(row.direction or "")
    if not merchant:
        return []

    exact_item: list[dict] = []
    same_merchant: list[dict] = []
    for evidence in evidence_rows or []:
        if not isinstance(evidence, dict):
            continue
        evidence_merchant = " ".join(str(evidence.get("counterparty") or "").casefold().split())
        if evidence_merchant != merchant or str(evidence.get("direction") or "") != direction:
            continue
        evidence_item = " ".join(str(evidence.get("item") or "").casefold().split())
        match_basis = "相同商户及方向"
        if item and evidence_item == item:
            match_basis = "相同商户、商品描述及方向"
            exact_item.append({**evidence, "match_basis": match_basis})
        else:
            same_merchant.append({**evidence, "match_basis": match_basis})

    matches = exact_item + same_merchant
    # Prefer recent evidence within each match strength; Python's stable sort
    # keeps the exact-item group ahead of same-merchant-only evidence.
    matches.sort(key=lambda value: str(value.get("date") or ""), reverse=True)
    matches.sort(key=lambda value: 0 if value.get("match_basis") == "相同商户、商品描述及方向" else 1)
    return [
        {
            "tx_id": int(value["tx_id"]),
            "date": str(value.get("date") or ""),
            "counterparty": str(value.get("counterparty") or ""),
            "item": str(value.get("item") or ""),
            "source": str(value.get("source") or ""),
            "confirmed_category": str(value.get("confirmed_category") or ""),
            "evidence_type": str(value.get("evidence_type") or ""),
            "match_basis": str(value.get("match_basis") or ""),
        }
        for value in matches[:3]
        if value.get("tx_id") is not None and value.get("confirmed_category")
    ]


def _filter_analysis_to_visible(conn, analysis: dict | None) -> dict | None:
    """Hide tombstoned transactions from persisted AI previews without deleting audit history."""
    if not isinstance(analysis, dict):
        return analysis
    suggestions = analysis.get("suggestions")
    if not isinstance(suggestions, list):
        return analysis
    suggestion_ids: set[int] = set()
    evidence_ids: set[int] = set()
    for item in suggestions:
        if not isinstance(item, dict):
            continue
        if item.get("tx_id") is not None:
            parsed = _safe_int(item.get("tx_id"))
            if parsed >= 0:
                suggestion_ids.add(parsed)
        for evidence in item.get("history_evidence") or []:
            if isinstance(evidence, dict) and evidence.get("tx_id") is not None:
                parsed = _safe_int(evidence.get("tx_id"))
                if parsed >= 0:
                    evidence_ids.add(parsed)
    all_ids = suggestion_ids | evidence_ids
    visible_ids: set[int] = set()
    if all_ids:
        placeholders = ",".join("?" for _ in all_ids)
        visible_ids = {
            int(row["id"]) for row in conn.execute(
                f"SELECT id FROM v_tx WHERE id IN ({placeholders})",
                tuple(sorted(all_ids)),
            )
        }
    visible_suggestion_ids = suggestion_ids & visible_ids
    visible_evidence_ids = evidence_ids & visible_ids
    filtered = []
    for item in suggestions:
        if not isinstance(item, dict) or _safe_int(item.get("tx_id")) not in visible_suggestion_ids:
            continue
        copied = dict(item)
        copied["history_evidence"] = [
            evidence for evidence in (item.get("history_evidence") or [])
            if isinstance(evidence, dict) and _safe_int(evidence.get("tx_id")) in visible_evidence_ids
        ]
        copied["history_evidence_count"] = len(copied["history_evidence"])
        filtered.append(copied)
    removed = len(suggestions) - len(filtered)
    result = dict(analysis)
    result["suggestions"] = filtered
    counts = dict(result.get("counts") or {})
    if removed:
        counts["total"] = len(filtered)
        counts["suggested"] = sum(
            1 for item in filtered
            if not item.get("excluded") and item.get("target_path")
        )
        counts["excluded"] = sum(1 for item in filtered if item.get("excluded"))
        counts["needs_review"] = sum(
            1 for item in filtered
            if not item.get("excluded") and (
                item.get("needs_review") or not item.get("target_path")
            )
        )
        config = get_secret_config()
        threshold = float(config.confidence_threshold) if config else 0.90
        counts["high_confidence"] = sum(
            1 for item in filtered
            if not item.get("excluded") and item.get("target_path")
            and float(item.get("confidence") or 0) >= threshold
        )
        result["counts"] = counts
        result["hidden_deleted_transactions"] = removed
    return result


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1

def get_status() -> dict:
    ledger_service.sync_live_book()
    status = classification_service.get_status(LIVE_BOOK_ID)
    conn = db.get_conn()
    try:
        state = ledger_loader.read_state(conn, LIVE_BOOK_ID)
        facts = ledger_loader.raw_transactions(conn, state["members"])
        eligible_rows, _excluded_counts, _special = classification_service._eligible_classification_rows(facts)
        eligible_ids = {int(item["id"]) for item in eligible_rows}
        reviewed_ids = classification_store.reviewed_tx_ids(conn, LIVE_BOOK_ID)
        override_rows = conn.execute(
            "SELECT tx_id FROM tx_overrides WHERE "
            "(manual_category IS NOT NULL AND trim(manual_category) <> '') "
            "OR (manual_subcat IS NOT NULL AND trim(manual_subcat) <> '')"
        ).fetchall()
        protected_ids = {int(row["tx_id"]) for row in override_rows}
        pending_ids = eligible_ids - reviewed_ids
        protected_pending = len(pending_ids & protected_ids)
        manual_override_total = len(set(state["members"]) & protected_ids)
        latest = conn.execute(
            "SELECT id,status,total_count,processed_count,error_message,created_at,updated_at,analysis_json "
            "FROM ai_history_learning_sessions ORDER BY created_at DESC,id DESC LIMIT 1"
        ).fetchone()
        latest_session = dict(latest) if latest else None
        if latest_session:
            saved_analysis = json.loads(latest_session.pop("analysis_json") or "null")
            visible_analysis = _filter_analysis_to_visible(conn, saved_analysis)
            if isinstance(visible_analysis, dict):
                visible_total = len(visible_analysis.get("suggestions") or [])
                latest_session["total_count"] = visible_total
                latest_session["processed_count"] = min(
                    int(latest_session.get("processed_count") or 0), visible_total
                )
                latest_session["visible_counts"] = visible_analysis.get("counts") or {}
                latest_session["hidden_deleted_transactions"] = int(
                    visible_analysis.get("hidden_deleted_transactions") or 0
                )
    finally:
        conn.close()
    return {
        "book_id": LIVE_BOOK_ID,
        "total_transactions": status["total"],
        "eligible_transactions": status["eligible_total"],
        "already_reviewed": status["reviewed"],
        "pending_transactions": max(0, len(pending_ids) - protected_pending),
        # `protected_manual` is the total number of visible manual overrides.
        # Keep the pending subset separately so the UI never conflates the two.
        "protected_manual": manual_override_total,
        "protected_manual_pending": protected_pending,
        "excluded_special": status["excluded_special"],
        "active_rules": status["active_rules"],
        "latest_session": latest_session,
    }


def start_analysis() -> dict:
    config = get_secret_config()
    if config is None:
        raise AIHistoryError("尚未配置 AI 模型，请先在 AI 设置页面保存 API Key。", 409)
    if not config.allow_external_processing:
        raise AIHistoryError("请先在 AI 设置中授权将必要交易字段发送到模型接口。", 409)
    inputs = _load_history_inputs()
    if not inputs["rows"]:
        raise AIHistoryError("没有需要 AI 重新分析的历史交易。已确认或人工覆盖的交易会受到保护。", 409)

    session_id = f"aihist_{uuid4().hex[:24]}"
    now = datetime.now(timezone.utc).isoformat()
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        running = conn.execute(
            "SELECT id,status FROM ai_history_learning_sessions "
            "WHERE status IN ('queued','running') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if running:
            conn.rollback()
            return {"session_id": running["id"], "status": running["status"], "reused": True}
        conn.execute(
            "INSERT INTO ai_history_learning_sessions"
            "(id,book_id,status,base_seq,total_count,processed_count,created_at,updated_at) "
            "VALUES(?,?,'queued',?,?,0,?,?)",
            (session_id, LIVE_BOOK_ID, inputs["book_seq"], len(inputs["rows"]), now, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"session_id": session_id, "status": "queued", "total_count": len(inputs["rows"]), "reused": False}


async def run_analysis(session_id: str) -> None:
    """由 FastAPI BackgroundTasks 调用；失败状态持久化，页面可轮询。"""
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        claimed = conn.execute(
            "UPDATE ai_history_learning_sessions SET status='running',updated_at=datetime('now') "
            "WHERE id=? AND status='queued'",
            (session_id,),
        )
        if claimed.rowcount != 1:
            conn.rollback()
            return
        conn.commit()
    finally:
        conn.close()

    try:
        config = get_secret_config()
        if config is None or not config.allow_external_processing:
            raise AIHistoryError("AI 模型配置或数据处理授权已失效，请重新配置后重试。", 409)
        inputs = _load_history_inputs()
        conn = db.get_conn()
        try:
            started = conn.execute("SELECT base_seq FROM ai_history_learning_sessions WHERE id=?", (session_id,)).fetchone()
        finally:
            conn.close()
        if not started:
            return
        if int(started["base_seq"]) != int(inputs["book_seq"]):
            raise AIHistoryError("历史分析排队期间账本发生了变化，请重新启动历史学习，避免使用过期的分类上下文。", 409)
        if not inputs["rows"]:
            analysis = {
                "summary": "没有未确认且未被人工覆盖的历史交易需要重新分类。",
                "taxonomy": [], "suggestions": [],
                "counts": {"total": 0, "suggested": 0, "excluded": 0, "needs_review": 0,
                           "high_confidence": 0, "new_categories": 0},
                "protected_manual": inputs["protected_count"],
                "already_reviewed": inputs["reviewed_count"],
            }
        else:
            # 历史批量重分类一律先预览，不能因已有自动规则设置而静默修改旧账。
            safe_config = replace(config, auto_apply_rules=False)
            raw_analysis = await classify_import_batch(
                inputs["rows"], config=safe_config,
                existing_categories=inputs["categories"],
                rules=inputs["rules"],
                history_examples=inputs["history_examples"],
            )
            suggestions = []
            for item in raw_analysis.get("suggestions", []):
                index = int(item.get("row_index", -1))
                if index < 0 or index >= len(inputs["tx_ids"]):
                    continue
                tx_id = inputs["tx_ids"][index]
                row = inputs["rows"][index]
                suggestion = {
                    **item,
                    "tx_id": tx_id,
                    "tx_time": row.tx_time,
                    "source": row.source,
                    "current_path": inputs["current_paths"].get(tx_id, ""),
                }
                suggestion["history_evidence"] = _history_evidence_for_row(
                    row, inputs.get("history_evidence", [])
                )
                suggestion["history_evidence_count"] = len(suggestion["history_evidence"])
                suggestions.append(suggestion)
            counts = {
                "total": len(inputs["rows"]),
                "suggested": sum(1 for item in suggestions if not item.get("excluded") and item.get("target_path")),
                "excluded": sum(1 for item in suggestions if item.get("excluded")),
                "needs_review": sum(1 for item in suggestions if not item.get("excluded") and (
                    item.get("needs_review") or not item.get("target_path")
                )),
                "high_confidence": sum(1 for item in suggestions if not item.get("excluded")
                                        and item.get("target_path") and float(item.get("confidence", 0)) >= config.confidence_threshold),
                "new_categories": sum(1 for item in raw_analysis.get("taxonomy", []) if item.get("is_new")),
            }
            dates = sorted(row.date for row in inputs["rows"])
            source_counts: dict[str, int] = {}
            for row in inputs["rows"]:
                source_counts[row.source or "未知平台"] = source_counts.get(row.source or "未知平台", 0) + 1
            analysis = {
                "summary": raw_analysis.get("summary") or "已完成历史账单整批分析。",
                "date_from": dates[0] if dates else None,
                "date_to": dates[-1] if dates else None,
                "source_counts": source_counts,
                "taxonomy": raw_analysis.get("taxonomy", []),
                "suggestions": suggestions,
                "counts": counts,
                "protected_manual": inputs["protected_count"],
                "already_reviewed": inputs["reviewed_count"],
            }

        conn = db.get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE ai_history_learning_sessions SET status='preview',processed_count=total_count,"
                "analysis_json=?,error_message=NULL,updated_at=datetime('now') WHERE id=?",
                (_json(analysis), session_id),
            )
            conn.execute(
                "INSERT INTO ai_history_learning_messages(session_id,role,content) VALUES(?,?,?)",
                (session_id, "assistant", analysis["summary"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as exc:
        # 保留安全的模型服务商错误，避免把 HTTP 400/401/429 等关键原因
        # 全部压成同一条提示。其他异常写入后端日志，前端只显示异常类型。
        logger.exception("AI history analysis failed (session_id=%s)", session_id)
        if isinstance(exc, (AIHistoryError, AIProviderError)):
            message = str(exc)
        else:
            message = f"历史账单分析失败（{type(exc).__name__}）。请查看启动后端的终端日志。"
        conn = db.get_conn()
        try:
            conn.execute(
                "UPDATE ai_history_learning_sessions SET status='failed',error_message=?,updated_at=datetime('now') WHERE id=?",
                (message[:1000], session_id),
            )
            conn.commit()
        finally:
            conn.close()


def get_session(session_id: str) -> dict:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM ai_history_learning_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise AIHistoryError("没有找到这次历史账单学习任务。", 404)
        result = dict(row)
        analysis = json.loads(result["analysis_json"]) if result.get("analysis_json") else None
        analysis = _filter_analysis_to_visible(conn, analysis)
        commit_result = json.loads(result["result_json"]) if result.get("result_json") else None
        messages = [dict(item) for item in conn.execute(
            "SELECT role,content,created_at FROM ai_history_learning_messages WHERE session_id=? ORDER BY id",
            (session_id,),
        )]
        applied = json.loads(result.get("applied_tx_ids_json") or "[]")
        if applied:
            placeholders = ",".join("?" for _ in set(int(x) for x in applied))
            visible_applied = {
                int(item["id"]) for item in conn.execute(
                    f"SELECT id FROM v_tx WHERE id IN ({placeholders})",
                    tuple(sorted(set(int(x) for x in applied))),
                )
            }
            applied = [int(x) for x in applied if int(x) in visible_applied]
        commit_request = json.loads(result["commit_request_json"]) if result.get("commit_request_json") else None
        visible_total = len(analysis.get("suggestions") or []) if isinstance(analysis, dict) else int(result["total_count"])
        return {
            "session_id": result["id"], "status": result["status"],
            "base_seq": result["base_seq"], "total_count": visible_total,
            "processed_count": min(int(result["processed_count"]), visible_total), "analysis": analysis,
            "result": commit_result, "applied_tx_ids": applied, "commit_request": commit_request,
            "error_message": result.get("error_message"), "messages": messages,
            "created_at": result["created_at"], "updated_at": result["updated_at"],
        }
    finally:
        conn.close()


def _category_operations(state: dict, taxonomy: list[dict], approved_paths: set[str]) -> tuple[list[dict], dict[str, str]]:
    paths = _node_paths(state["nodes"])
    path_to_id = {path: node_id for node_id, path in paths.items()}
    root_id = f"{LIVE_BOOK_ID}:root"
    by_parent_name = {
        (str(node.get("parent_id")), str(node["name"])): str(node["id"])
        for node in state["nodes"] if node.get("kind") == "group"
    }
    operations: list[dict] = []
    for category in taxonomy:
        path = str(category.get("path") or "")
        if path not in approved_paths or path in path_to_id:
            continue
        major = str(category.get("major") or "").strip()
        minor = str(category.get("minor") or "").strip()
        if not major:
            continue
        major_id = by_parent_name.get((root_id, major))
        if not major_id:
            major_id = f"{LIVE_BOOK_ID}:aihist:{uuid4().hex[:12]}"
            operations.append({"op": "create_node", "node_id": major_id,
                              "parent_id": root_id, "name": major, "op_value": "ADD"})
            by_parent_name[(root_id, major)] = major_id
            path_to_id[major] = major_id
        if not minor:
            path_to_id[path] = major_id
            continue
        minor_id = by_parent_name.get((major_id, minor))
        if not minor_id:
            minor_id = f"{LIVE_BOOK_ID}:aihist:{uuid4().hex[:12]}"
            operations.append({"op": "create_node", "node_id": minor_id,
                              "parent_id": major_id, "name": minor, "op_value": "ADD"})
            by_parent_name[(major_id, minor)] = minor_id
        path_to_id[path] = minor_id
    return operations, path_to_id


def commit_session(
    session_id: str,
    *,
    approved_category_paths: list[str],
    approved_tx_ids: list[int],
    remember_rule_tx_ids: list[int] | None = None,
) -> dict:
    session = get_session(session_id)
    if session["status"] == "applied":
        return session["result"] or {"status": "applied", "message": "历史分类已经应用。"}
    if session["status"] not in {"preview", "partially_applied"} or not session["analysis"]:
        raise AIHistoryError("这次历史分析尚未完成，或当前状态不允许提交。", 409)

    analysis = session["analysis"]
    taxonomy_by_path = {str(item["path"]): item for item in analysis.get("taxonomy", [])}
    approved_paths = set(approved_category_paths or [])
    if not approved_paths.issubset(set(taxonomy_by_path)):
        raise AIHistoryError("提交中包含不属于本次 AI 建议的分类路径。")

    suggestions_by_id = {
        int(item["tx_id"]): item for item in analysis.get("suggestions", [])
        if item.get("tx_id") is not None
    }
    selected_ids = [int(value) for value in approved_tx_ids]
    if not selected_ids or len(selected_ids) != len(set(selected_ids)):
        raise AIHistoryError("请至少选择一笔交易，且不能重复选择。")
    if not set(selected_ids).issubset(set(suggestions_by_id)):
        raise AIHistoryError("所选交易不属于本次历史分析结果。")
    for tx_id in selected_ids:
        suggestion = suggestions_by_id[tx_id]
        if suggestion.get("excluded") or not suggestion.get("target_path"):
            raise AIHistoryError(f"交易 {tx_id} 没有可应用的分类建议。")
        category = taxonomy_by_path.get(str(suggestion["target_path"]))
        if category and category.get("is_new") and suggestion["target_path"] not in approved_paths:
            raise AIHistoryError(f"请先批准新增分类：{suggestion['target_path']}。")

    remember_ids = {int(value) for value in (remember_rule_tx_ids or [])}
    if not remember_ids.issubset(set(selected_ids)):
        raise AIHistoryError("长期规则只能为本次明确选择应用的交易创建。")
    request = {
        "approved_category_paths": sorted(approved_paths),
        "approved_tx_ids": sorted(selected_ids),
        "remember_rule_tx_ids": sorted(remember_ids),
    }
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT commit_request_json,applied_tx_ids_json FROM ai_history_learning_sessions WHERE id=?",
                           (session_id,)).fetchone()
        if not row:
            raise AIHistoryError("没有找到这次历史学习任务。", 404)
        saved_request = row["commit_request_json"]
        if saved_request:
            saved = json.loads(saved_request)
            # 允许历史预览中的已删除/墓碑交易从重试请求中剔除，其余已确认选择仍必须一致。
            saved_ids = [int(value) for value in saved.get("approved_tx_ids", [])]
            if saved_ids:
                placeholders = ",".join("?" for _ in set(saved_ids))
                visible_saved_ids = {
                    int(item["id"]) for item in conn.execute(
                        f"SELECT id FROM v_tx WHERE id IN ({placeholders})",
                        tuple(sorted(set(saved_ids))),
                    )
                }
                saved["approved_tx_ids"] = sorted(
                    value for value in saved_ids if value in visible_saved_ids
                )
                saved["remember_rule_tx_ids"] = sorted(
                    int(value) for value in saved.get("remember_rule_tx_ids", [])
                    if int(value) in visible_saved_ids
                )
            if saved != request:
                raise AIHistoryError("这次提交已开始，重试时必须使用相同的有效交易和分类选择。", 409)
        applied_ids = set(json.loads(row["applied_tx_ids_json"] or "[]"))
        if not saved_request:
            conn.execute("UPDATE ai_history_learning_sessions SET commit_request_json=?,updated_at=datetime('now') WHERE id=?",
                         (_json(request), session_id))
            conn.commit()
    finally:
        conn.close()

    # 先检查用户分析后是否改动了目标交易的现有分类，避免覆盖其他操作。
    ledger_service.sync_live_book()
    conn = db.get_conn()
    try:
        current_state = ledger_loader.read_state(conn, LIVE_BOOK_ID)
        current_paths = _node_paths(current_state["nodes"])
        current_seq = int(ledger_loader.read_book(conn, LIVE_BOOK_ID)["current_seq"])
        for tx_id in selected_ids:
            suggestion = suggestions_by_id[tx_id]
            expected_current = str(suggestion.get("current_path") or "")
            current_node = current_state["placements"].get(str(tx_id))
            actual_current = current_paths.get(str(current_node), "")
            if expected_current and actual_current != expected_current:
                raise AIHistoryError(f"交易 {tx_id} 在分析后已被其他操作修改分类，请重新分析历史账单。", 409)
    finally:
        conn.close()

    operations, _ = _category_operations(current_state, analysis.get("taxonomy", []), approved_paths)
    if operations:
        ledger_service.apply_ops_batch(
            LIVE_BOOK_ID, operations, expected_seq=current_seq,
            label=f"批准 AI 历史分类体系 {session_id}",
        )
    conn = db.get_conn()
    try:
        current_state = ledger_loader.read_state(conn, LIVE_BOOK_ID)
        current_paths = _node_paths(current_state["nodes"])
        path_to_id = {path: node_id for node_id, path in current_paths.items()}
    finally:
        conn.close()

    remaining_ids = [tx_id for tx_id in selected_ids if tx_id not in applied_ids]
    try:
        for start in range(0, len(remaining_ids), 50):
            chunk_ids = remaining_ids[start:start + 50]
            chunk: list[ClassificationSuggestion] = []
            for tx_id in chunk_ids:
                item = suggestions_by_id[tx_id]
                target_path = str(item["target_path"])
                target_node_id = path_to_id.get(target_path)
                if not target_node_id:
                    raise AIHistoryError(f"目标分类不存在：{target_path}。请批准该分类后重试。", 409)
                suggestion_data: dict[str, Any] = {
                    "tx_id": tx_id,
                    "target_node_id": target_node_id,
                    "confidence": float(item.get("confidence", 0.0)),
                    "reason": str(item.get("reason") or "AI 历史账单分类建议")[:500],
                    "needs_clarification": False,
                    "question": None,
                    "remember_rule": False,
                }
                if tx_id in remember_ids:
                    pattern = str(item.get("counterparty") or item.get("item") or "").strip()
                    field = "counterparty" if item.get("counterparty") else "item"
                    if pattern:
                        suggestion_data.update({"remember_rule": True, "rule_field": field, "rule_pattern": pattern})
                chunk.append(ClassificationSuggestion.model_validate(suggestion_data))

            current = ledger_service.get_status(LIVE_BOOK_ID)
            draft = classification_service.preview(
                LIVE_BOOK_ID, chunk, expected_seq=int(current["current_seq"]),
                created_by="web_ai_history_learning",
            )
            classification_service.confirm(
                draft["proposal_id"], chunk_ids, draft["base_seq"],
                confirmed_by="web_history_user_confirmed",
                decision_note="用户在历史账单学习工作区确认 AI 分类",
            )
            applied_ids.update(chunk_ids)
            conn = db.get_conn()
            try:
                conn.execute(
                    "UPDATE ai_history_learning_sessions SET status='partially_applied',applied_tx_ids_json=?,updated_at=datetime('now') WHERE id=?",
                    (_json(sorted(applied_ids)), session_id),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        # 已成功的分块保留审计记录；相同请求重试时只处理剩余交易。
        raise

    result = {
        "status": "applied",
        "session_id": session_id,
        "applied_count": len(applied_ids),
        "applied_tx_ids": sorted(applied_ids),
        "created_categories": sorted(approved_paths),
        "remembered_rules": len(remember_ids),
        "message": f"已通过 Ledger Engine 应用 {len(applied_ids)} 笔历史分类；原始交易记录未被修改。",
    }
    conn = db.get_conn()
    try:
        conn.execute(
            "UPDATE ai_history_learning_sessions SET status='applied',result_json=?,applied_tx_ids_json=?,updated_at=datetime('now') WHERE id=?",
            (_json(result), _json(sorted(applied_ids)), session_id),
        )
        conn.commit()
    finally:
        conn.close()
    return result
