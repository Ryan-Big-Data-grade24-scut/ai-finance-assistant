"""模型无关的 AI 分类业务层。

流程固定为：查询待审 → 保存草案（零账本写入）→ 用户确认 → Ledger 批量归位。
外部 MCP 与未来内置模型 API 复用本模块。
"""
from __future__ import annotations

from uuid import uuid4
import json
import sqlite3

from .. import db
from ..ledger import loader as ledger_loader
from ..ledger import service as ledger_service
from ..ledger.amount import cents
from ..ledger.atoms import analyze_special_transactions, is_refund_transaction
from . import store
from .errors import ClassificationError, Code
from .models import ClassificationSuggestion


def _sync_live_if_needed(book_id: str) -> None:
    if book_id == ledger_service.LIVE_BOOK_ID:
        ledger_service.sync_live_book()


def _node_paths(nodes: list[dict]) -> dict[str, str]:
    by_id = {node["id"]: node for node in nodes}
    cache: dict[str, str] = {}

    def path(node_id: str) -> str:
        if node_id in cache:
            return cache[node_id]
        node = by_id[node_id]
        parent_id = node.get("parent_id")
        parent = by_id.get(parent_id) if parent_id else None
        if parent_id is None or (parent and parent.get("kind") == "root"):
            value = node["name"]
        else:
            prefix = path(parent_id) if parent else ""
            value = f"{prefix} / {node['name']}" if prefix else node["name"]
        cache[node_id] = value
        return value

    for node_id in by_id:
        path(node_id)
    return cache


def _rule_hints(tx: dict, rules: list[dict], valid_nodes: set[str]) -> list[dict]:
    hints = []
    for rule in rules:
        if rule["target_node_id"] not in valid_nodes:
            continue
        value = str(tx.get(rule["match_field"]) or "").casefold()
        if rule["pattern_norm"] in value:
            hints.append(
                {
                    "rule_id": rule["id"],
                    "field": rule["match_field"],
                    "pattern": rule["pattern"],
                    "target_node_id": rule["target_node_id"],
                }
            )
    return hints


def _validate_remember_rule(item: ClassificationSuggestion, tx: dict) -> None:
    """只允许保存确实匹配当前交易文本的规则，避免模型生成无效/错配规则。"""
    if not item.remember_rule:
        return
    field = item.rule_field
    pattern = (item.rule_pattern or "").strip()
    value = str(tx.get(field or "") or "")
    if not pattern or pattern.casefold() not in value.casefold():
        raise ClassificationError(
            Code.INVALID_SUGGESTION,
            f"交易 {item.tx_id} 的规则关键词必须真实出现在对应的 {field} 字段中。",
        )


def _classification_exclusion_reason(tx: dict, special: dict) -> str | None:
    tx_id = int(tx["id"])
    if is_refund_transaction(tx):
        return "refund_record"
    if tx_id in special["refund_conflict_ids"]:
        return "refund_conflict"
    if tx_id in special["transfer_zero_ids"]:
        return "internal_transfer"
    if tx_id in special["refund_zero_ids"]:
        return "fully_refunded"
    return None


def _platform_details(tx: dict) -> dict:
    """只向 AI 暴露与分类有关的平台字段，不暴露工作簿账号/导出元信息。"""
    raw = tx.get("raw_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    if not isinstance(raw, dict):
        return {}
    aliases = {
        "transaction_origin": ("交易来源地", "交易来源"),
        "fund_status": ("资金状态",),
        "service_fee": ("服务费（元）", "服务费(元)", "服务费"),
        "platform_refund_amount": ("成功退款（元）", "成功退款(元)", "成功退款"),
    }
    result = {}
    for output_key, keys in aliases.items():
        for key in keys:
            value = raw.get(key)
            if value not in (None, ""):
                result[output_key] = value
                break
    return result


def _eligible_classification_rows(facts: list[dict]) -> tuple[list[dict], dict, dict]:
    special = analyze_special_transactions(facts)
    eligible = []
    excluded_counts: dict[str, int] = {}
    excluded_ids: dict[int, str] = {}
    for tx in facts:
        reason = _classification_exclusion_reason(tx, special)
        if reason:
            excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
            excluded_ids[int(tx["id"])] = reason
        else:
            eligible.append(tx)
    return eligible, excluded_counts, special


def get_status(book_id: str) -> dict:
    _sync_live_if_needed(book_id)
    conn = db.get_conn()
    try:
        book = ledger_loader.read_book(conn, book_id)
        state = ledger_loader.read_state(conn, book_id)
        facts = ledger_loader.raw_transactions(conn, state["members"])
        eligible, excluded_counts, _special = _eligible_classification_rows(facts)
        reviewed = store.reviewed_tx_ids(conn, book_id)
        eligible_ids = {int(tx["id"]) for tx in eligible}
        reviewed_eligible = len(eligible_ids & reviewed)
        counts = store.status_counts(conn, book_id)
        return {
            "book_id": book_id,
            "book_name": book["name"],
            "ledger_seq": book["current_seq"],
            "total": len(state["members"]),
            "eligible_total": len(eligible),
            "reviewed": reviewed_eligible,
            "pending": max(0, len(eligible) - reviewed_eligible),
            "excluded_special": excluded_counts,
            "draft_proposals": counts["draft_proposals"],
            "active_rules": counts["active_rules"],
            "workflow": (
                "query_classification_batch → preview_classifications → "
                "resolve_classification_clarification（如需要） → 用户确认 → confirm_classifications"
            ),
        }
    finally:
        conn.close()


def get_batch(book_id: str, limit: int = 20) -> dict:
    if limit < 1 or limit > 50:
        raise ClassificationError(Code.INVALID_SUGGESTION, "limit 必须在 1 到 50 之间。")
    _sync_live_if_needed(book_id)
    conn = db.get_conn()
    try:
        book = ledger_loader.read_book(conn, book_id)
        state = ledger_loader.read_state(conn, book_id)
        facts = ledger_loader.raw_transactions(conn, state["members"])
        eligible, excluded_counts, special = _eligible_classification_rows(facts)
        reviewed = store.reviewed_tx_ids(conn, book_id)
        pending = [tx for tx in eligible if int(tx["id"]) not in reviewed]
        nodes = state["nodes"]
        paths = _node_paths(nodes)
        candidates = [
            {
                "node_id": node["id"],
                "path": paths[node["id"]],
                "op": node["op"],
            }
            for node in nodes
            if node.get("kind") == "group"
        ]
        candidates.sort(key=lambda item: item["path"])
        valid_nodes = {item["node_id"] for item in candidates}
        rules = store.list_rules(conn, book_id)
        refund_amounts = special["refund_amounts_by_original"]
        items = []
        for tx in pending[:limit]:
            tx_id = int(tx["id"])
            current_node_id = state["placements"][str(tx_id)]
            amount_cents = cents(tx["amount"])
            refunded_cents = int(refund_amounts.get(tx_id, 0))
            effective_cents = max(0, amount_cents - refunded_cents)
            direction = tx["direction"]
            if direction == "中性":
                direction = "收入" if "收益发放" in str(tx.get("item") or "") else "支出"

            item = {
                "tx_id": tx_id,
                "tx_time": tx["tx_time"],
                "direction": tx["direction"],
                "effective_direction": direction,
                "amount": float(tx["amount"]),
                "effective_amount": effective_cents / 100,
                "refunded_amount": refunded_cents / 100,
                "is_partial_refund": tx_id in special["partial_refund_ids"],
                "source": tx.get("source") or "",
                "counterparty": tx.get("counterparty") or "",
                "item": tx.get("item") or "",
                "pay_method": tx.get("pay_method") or "",
                "tx_type": tx.get("tx_type") or "",
                "status": tx.get("status") or "",
                "remark": tx.get("remark") or "",
                "platform_details": _platform_details(tx),
                "current_node_id": current_node_id,
                "current_path": paths.get(current_node_id, current_node_id),
            }
            hints = _rule_hints(item, rules, valid_nodes)
            distinct_targets = sorted({hint["target_node_id"] for hint in hints})
            item["rule_hints"] = hints
            item["rule_hint_conflict"] = len(distinct_targets) > 1
            item["rule_suggested_target_node_id"] = (
                distinct_targets[0] if len(distinct_targets) == 1 else None
            )
            items.append(item)
        return {
            "book_id": book_id,
            "ledger_seq": book["current_seq"],
            "member_total": len(state["members"]),
            "eligible_total": len(eligible),
            "pending_total": len(pending),
            "returned": len(items),
            "excluded_special": excluded_counts,
            "items": items,
            "allowed_nodes": candidates,
            "instruction": (
                "只分类本批返回的交易。effective_amount 是退款抵扣后的有效金额；"
                "平台退款流水、全额退款原交易、明确内部转账和退款证据冲突交易已排除。"
                "rule_hint_conflict=true 表示用户规则互相冲突，必须先询问用户。"
                "逐笔选择 allowed_nodes 中的 node_id；拿不准时不要猜："
                "设置 needs_clarification=true、target_node_id=null 并给出一个具体问题。"
            ),
        }
    finally:
        conn.close()


def preview(
    book_id: str,
    suggestions: list[ClassificationSuggestion | dict],
    expected_seq: int | None = None,
    created_by: str = "external_mcp",
) -> dict:
    if not suggestions or len(suggestions) > 50:
        raise ClassificationError(Code.INVALID_SUGGESTION, "每份草案必须包含 1 到 50 条建议。")
    _sync_live_if_needed(book_id)
    normalized = [
        item if isinstance(item, ClassificationSuggestion) else ClassificationSuggestion.model_validate(item)
        for item in suggestions
    ]
    ids = [item.tx_id for item in normalized]
    if len(ids) != len(set(ids)):
        raise ClassificationError(Code.DUPLICATE_SUGGESTION, "同一份草案里不能重复提交同一笔交易。")

    proposal_id = f"proposal_{uuid4().hex[:16]}"
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        book = ledger_loader.read_book(conn, book_id)
        base_seq = int(book["current_seq"])
        if expected_seq is not None and int(book["current_seq"]) != expected_seq:
            raise ClassificationError(Code.STALE_PROPOSAL, "账本已变化，请重新读取待分类批次。", 409)
        state = ledger_loader.read_state(conn, book_id)
        reviewed = store.reviewed_tx_ids(conn, book_id)
        members = {int(tx_id) for tx_id in state["members"]}
        facts = ledger_loader.raw_transactions(conn, state["members"])
        eligible_rows, _excluded_counts, _special = _eligible_classification_rows(facts)
        eligible_ids = {int(tx["id"]) for tx in eligible_rows}
        tx_by_id = {int(tx["id"]): tx for tx in facts}
        allowed_nodes = {
            node["id"] for node in state["nodes"] if node.get("kind") == "group"
        }
        paths = _node_paths(state["nodes"])
        rules = store.list_rules(conn, book_id)
        rows = []
        for item in normalized:
            if item.tx_id not in members:
                raise ClassificationError(Code.TX_NOT_IN_BOOK, f"交易 {item.tx_id} 不在这本账中。")
            if item.tx_id not in eligible_ids:
                raise ClassificationError(
                    Code.SPECIAL_TX_NOT_CLASSIFIABLE,
                    f"交易 {item.tx_id} 是退款流水、全额退款、内部转账或退款证据冲突交易，不能进入 AI 分类草案。",
                )
            if item.tx_id in reviewed:
                raise ClassificationError(Code.ALREADY_REVIEWED, f"交易 {item.tx_id} 已由用户确认过；请先撤销原提案。", 409)
            if item.target_node_id is not None and item.target_node_id not in allowed_nodes:
                raise ClassificationError(
                    Code.TARGET_NOT_ALLOWED,
                    f"交易 {item.tx_id} 的目标不是可分类的 group 节点。",
                )
            tx = tx_by_id[item.tx_id]
            hints = _rule_hints(tx, rules, allowed_nodes)
            distinct_targets = {hint["target_node_id"] for hint in hints}
            if len(distinct_targets) > 1 and not item.needs_clarification:
                raise ClassificationError(
                    Code.RULE_HINT_CONFLICT,
                    f"交易 {item.tx_id} 命中多条互相冲突的用户规则，必须先提交澄清问题。",
                    409,
                )
            _validate_remember_rule(item, tx)
            row = item.model_dump()
            row["original_node_id"] = state["placements"][str(item.tx_id)]
            rows.append(row)
        store.create_proposal(
            conn,
            {
                "id": proposal_id,
                "book_id": book_id,
                "base_seq": int(book["current_seq"]),
                "created_by": created_by,
            },
            rows,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    ready = []
    questions = []
    for row in rows:
        summary = {
            "tx_id": row["tx_id"],
            "current_path": paths[row["original_node_id"]],
            "target_node_id": row.get("target_node_id"),
            "target_path": paths.get(row.get("target_node_id"), ""),
            "confidence": row["confidence"],
            "reason": row["reason"],
            "remember_rule": row["remember_rule"],
        }
        if row["needs_clarification"]:
            questions.append({**summary, "question": row["question"]})
        else:
            ready.append(summary)
    return {
        "proposal_id": proposal_id,
        "book_id": book_id,
        "base_seq": base_seq,
        "ready": ready,
        "needs_clarification": questions,
        "requires_user_confirmation": bool(ready),
        "next_action": (
            "先把 ready 的目标、置信度和理由展示给用户；"
            "逐个询问 needs_clarification；只有用户明确同意后才能调用 confirm_classifications。"
        ),
    }


def get_proposal(proposal_id: str) -> dict:
    conn = db.get_conn()
    try:
        proposal = store.get_proposal(conn, proposal_id)
        if not proposal:
            raise ClassificationError(Code.PROPOSAL_NOT_FOUND, "没有找到这份分类草案。", 404)
        book = ledger_loader.read_book(conn, proposal["book_id"])
        state = ledger_loader.read_state(conn, proposal["book_id"])
        paths = _node_paths(state["nodes"])
        tx_by_id = {
            int(tx["id"]): tx
            for tx in ledger_loader.raw_transactions(conn, state["members"])
        }
        suggestions = store.get_suggestions(conn, proposal_id)
        for item in suggestions:
            item["current_path"] = paths.get(item["original_node_id"], item["original_node_id"])
            item["target_path"] = paths.get(item.get("target_node_id"))
            tx = tx_by_id.get(int(item["tx_id"]), {})
            item["counterparty"] = tx.get("counterparty", "")
            item["item"] = tx.get("item", "")
            item["amount"] = tx.get("amount", 0)
            item["direction"] = tx.get("direction", "")
            item["source"] = tx.get("source", "")
        available_categories = [
            {"node_id": node_id, "path": path}
            for node_id, path in paths.items()
            if any(node.get("id") == node_id and node.get("kind") == "group" for node in state["nodes"])
        ]
        ready_count = sum(1 for item in suggestions if not item["needs_clarification"] and item["status"] == "pending")
        clarification_count = sum(1 for item in suggestions if item["needs_clarification"] and item["status"] == "pending")
        return {
            **proposal,
            "current_seq": int(book["current_seq"]),
            "stale": proposal["status"] == "draft" and int(book["current_seq"]) != int(proposal["base_seq"]),
            "ready_count": ready_count,
            "clarification_count": clarification_count,
            "available_categories": available_categories,
            "suggestions": suggestions,
        }
    finally:
        conn.close()


def list_proposals(
    book_id: str, status: str | None = "draft", limit: int = 20
) -> dict:
    if status not in {None, "draft", "applied", "rejected", "undone"}:
        raise ClassificationError(Code.INVALID_SUGGESTION, "status 必须是 draft/applied/rejected/undone 或 null。")
    if not 1 <= limit <= 50:
        raise ClassificationError(Code.INVALID_SUGGESTION, "limit 必须在 1 到 50 之间。")
    conn = db.get_conn()
    try:
        book = ledger_loader.read_book(conn, book_id)
        proposals = store.list_proposals(conn, book_id, status, limit)
        current_seq = int(book["current_seq"])
        for proposal in proposals:
            proposal["stale"] = (
                proposal["status"] == "draft"
                and current_seq != int(proposal["base_seq"])
            )
        return {
            "book_id": book_id,
            "current_seq": current_seq,
            "status_filter": status,
            "returned": len(proposals),
            "proposals": proposals,
        }
    finally:
        conn.close()


def rebase_if_safe(proposal_id: str) -> dict:
    """只在草案涉及的交易归属和目标节点均未变化时，安全更新草案版本。"""
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        proposal = store.get_proposal(conn, proposal_id)
        if not proposal:
            raise ClassificationError(Code.PROPOSAL_NOT_FOUND, "没有找到这份分类草案。", 404)
        if proposal["status"] != "draft":
            raise ClassificationError(Code.PROPOSAL_CLOSED, "只有未处理的草案可以重新校验。", 409)
        book = ledger_loader.read_book(conn, proposal["book_id"])
        actual_seq = int(book["current_seq"])
        if actual_seq == int(proposal["base_seq"]):
            conn.commit()
            return get_proposal(proposal_id)
        state = ledger_loader.read_state(conn, proposal["book_id"])
        members = {int(tx_id) for tx_id in state["members"]}
        valid_nodes = {node["id"] for node in state["nodes"] if node.get("kind") == "group"}
        pending = [item for item in store.get_suggestions(conn, proposal_id) if item["status"] == "pending"]
        reviewed = store.reviewed_tx_ids(conn, proposal["book_id"])
        facts = ledger_loader.raw_transactions(conn, state["members"])
        eligible_rows, _, _ = _eligible_classification_rows(facts)
        eligible = {int(item["id"]) for item in eligible_rows}
        for item in pending:
            tx_id = int(item["tx_id"])
            if tx_id not in members or tx_id in reviewed or tx_id not in eligible:
                raise ClassificationError(Code.STALE_PROPOSAL, "草案中的交易已变化或已被处理，不能安全重新校验。", 409)
            if state["placements"].get(str(tx_id)) != item["original_node_id"]:
                raise ClassificationError(Code.STALE_PROPOSAL, "草案中的交易分类已变化，不能安全重新校验。", 409)
            if item.get("target_node_id") and item["target_node_id"] not in valid_nodes:
                raise ClassificationError(Code.STALE_PROPOSAL, "草案目标分类已不存在，不能安全重新校验。", 409)
        conn.execute(
            "UPDATE ai_classification_proposals SET base_seq=? WHERE id=?",
            (actual_seq, proposal_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_proposal(proposal_id)


def resolve_clarification(
    proposal_id: str,
    suggestion: ClassificationSuggestion | dict,
    *,
    user_answer: str,
    expected_seq: int | None = None,
) -> dict:
    """把用户回答写回现有草案，不新建重复 proposal，也不直接改账。"""
    answer = user_answer.strip()
    if not answer or len(answer) > 1000:
        raise ClassificationError(Code.INVALID_SUGGESTION, "user_answer 必须为 1 到 1000 个字符。")
    item = (
        suggestion if isinstance(suggestion, ClassificationSuggestion)
        else ClassificationSuggestion.model_validate(suggestion)
    )
    if item.needs_clarification:
        raise ClassificationError(Code.INVALID_SUGGESTION, "澄清完成后必须提供明确目标分类，不能继续标记为待澄清。")

    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        proposal = store.get_proposal(conn, proposal_id)
        if not proposal:
            raise ClassificationError(Code.PROPOSAL_NOT_FOUND, "没有找到这份分类草案。", 404)
        if proposal["status"] != "draft":
            raise ClassificationError(Code.PROPOSAL_CLOSED, "只有未处理的草案可以继续澄清。", 409)
        book = ledger_loader.read_book(conn, proposal["book_id"])
        actual_seq = int(book["current_seq"])
        if actual_seq != int(proposal["base_seq"]) or (
            expected_seq is not None and expected_seq != actual_seq
        ):
            raise ClassificationError(Code.STALE_PROPOSAL, "草案生成后账本已变化，请重新读取并生成草案。", 409)

        existing = store.get_suggestion(conn, proposal_id, item.tx_id)
        if not existing:
            raise ClassificationError(Code.INVALID_SUGGESTION, "这笔交易不属于当前草案。")
        if not existing["needs_clarification"] or existing["status"] != "pending":
            raise ClassificationError(Code.CLARIFICATION_NOT_PENDING, "这笔交易当前没有待处理的澄清问题。", 409)

        state = ledger_loader.read_state(conn, proposal["book_id"])
        members = {int(tx_id) for tx_id in state["members"]}
        if item.tx_id not in members:
            raise ClassificationError(Code.TX_NOT_IN_BOOK, f"交易 {item.tx_id} 已不在这本账中。")
        valid_nodes = {node["id"] for node in state["nodes"] if node.get("kind") == "group"}
        if item.target_node_id not in valid_nodes:
            raise ClassificationError(Code.TARGET_NOT_ALLOWED, "澄清后的目标必须是当前账本中的 group 节点。")
        facts = ledger_loader.raw_transactions(conn, state["members"])
        tx_by_id = {int(tx["id"]): tx for tx in facts}
        tx = tx_by_id.get(item.tx_id)
        if tx is None:
            raise ClassificationError(Code.TX_NOT_IN_BOOK, f"交易 {item.tx_id} 不在这本账中。")
        eligible_rows, _excluded, _special = _eligible_classification_rows(facts)
        if item.tx_id not in {int(tx_row["id"]) for tx_row in eligible_rows}:
            raise ClassificationError(Code.SPECIAL_TX_NOT_CLASSIFIABLE, "该交易已变为特殊交易，不能继续分类。", 409)
        if item.tx_id in store.reviewed_tx_ids(conn, proposal["book_id"]):
            raise ClassificationError(Code.ALREADY_REVIEWED, "该交易已经被确认，不能继续澄清。", 409)
        _validate_remember_rule(item, tx)

        # 用户原话写入理由尾注，保留本轮澄清依据，且不新增数据库列。
        reason = item.reason.strip()
        answer_note = f"用户澄清：{answer[:200]}"
        max_reason = 500
        if len(reason) + len(answer_note) + 2 > max_reason:
            reason = reason[: max(0, max_reason - len(answer_note) - 3)].rstrip()
        resolved = item.model_dump()
        resolved["reason"] = f"{reason}；{answer_note}" if reason else answer_note
        if not store.update_suggestion_resolution(conn, proposal_id, item.tx_id, resolved):
            raise ClassificationError(Code.CLARIFICATION_NOT_PENDING, "澄清状态已被其他操作更新，请重新读取草案。", 409)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    result = get_proposal(proposal_id)
    result["message"] = "澄清已写回原草案，账本尚未修改；仍需用户明确确认后才能落账。"
    return result


def confirm(
    proposal_id: str,
    approved_tx_ids: list[int],
    expected_seq: int | None = None,
    confirmed_by: str = "user_via_mcp",
    decision_note: str = "用户通过 MCP 确认",
) -> dict:
    approved_ids = [int(tx_id) for tx_id in approved_tx_ids]
    if not approved_ids or len(approved_ids) != len(set(approved_ids)):
        raise ClassificationError(
            Code.CONFIRMATION_REQUIRED,
            "必须提供用户明确同意的、且不重复的 approved_tx_ids。",
        )
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        proposal = store.get_proposal(conn, proposal_id)
        if not proposal:
            raise ClassificationError(Code.PROPOSAL_NOT_FOUND, "没有找到这份分类草案。", 404)
        if proposal["status"] != "draft":
            raise ClassificationError(Code.PROPOSAL_CLOSED, "这份草案已经处理过，不能重复确认。", 409)
        suggestions = store.get_suggestions(conn, proposal_id)
        by_id = {int(item["tx_id"]): item for item in suggestions}
        if any(tx_id not in by_id for tx_id in approved_ids):
            raise ClassificationError(Code.INVALID_SUGGESTION, "approved_tx_ids 包含不属于该草案的交易。")
        approved = [by_id[tx_id] for tx_id in approved_ids]
        if any(item["needs_clarification"] or not item["target_node_id"] for item in approved):
            raise ClassificationError(
                Code.CONFIRMATION_REQUIRED,
                "仍需澄清的交易不能确认；请根据用户回答重新生成草案。",
            )
        book = ledger_loader.read_book(conn, proposal["book_id"])
        actual_seq = int(book["current_seq"])
        if actual_seq != int(proposal["base_seq"]) or (
            expected_seq is not None and expected_seq != actual_seq
        ):
            raise ClassificationError(Code.STALE_PROPOSAL, "草案生成后账本已变化，请重新生成草案。", 409)
        state = ledger_loader.read_state(conn, proposal["book_id"])
        operations = [
            {"op": "reassign", "tx_id": item["tx_id"], "to_node_id": item["target_node_id"]}
            for item in approved
            if state["placements"].get(str(item["tx_id"])) != item["target_node_id"]
        ]
        if operations:
            applied_seq = ledger_service._apply_ops_in_transaction(
                conn,
                proposal["book_id"],
                operations,
                actual_seq,
                f"AI 分类确认 {proposal_id}",
            )
        else:
            applied_seq = actual_seq
        rejected = [int(item["tx_id"]) for item in suggestions if int(item["tx_id"]) not in approved_ids]
        store.apply_decision(
            conn, proposal, approved, rejected, applied_seq, len(operations),
            confirmed_by=confirmed_by, decision_note=decision_note,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "proposal_id": proposal_id,
        "status": "applied",
        "approved_tx_ids": approved_ids,
        "rejected_tx_ids": rejected,
        "changed_count": len(operations),
        "ledger": ledger_service.get_status(proposal["book_id"]),
        "message": "已通过 Ledger 引擎校验并保存；本次未批准的建议已关闭，整批已批准操作可用 undo_classifications 撤销。",
    }


def reject(proposal_id: str, reason: str) -> dict:
    note = reason.strip()
    if not note:
        raise ClassificationError(Code.INVALID_SUGGESTION, "拒绝草案时请说明原因。")
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        proposal = store.get_proposal(conn, proposal_id)
        if not proposal:
            raise ClassificationError(Code.PROPOSAL_NOT_FOUND, "没有找到这份分类草案。", 404)
        if proposal["status"] != "draft":
            raise ClassificationError(Code.PROPOSAL_CLOSED, "这份草案已经处理过。", 409)
        store.reject_proposal(conn, proposal_id, note)
        conn.commit()
        return {"proposal_id": proposal_id, "status": "rejected", "reason": note}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def undo(proposal_id: str, expected_seq: int | None = None) -> dict:
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        proposal = store.get_proposal(conn, proposal_id)
        if not proposal:
            raise ClassificationError(Code.PROPOSAL_NOT_FOUND, "没有找到这份分类草案。", 404)
        if proposal["status"] != "applied":
            raise ClassificationError(Code.PROPOSAL_CLOSED, "只有已确认的草案可以撤销。", 409)
        book = ledger_loader.read_book(conn, proposal["book_id"])
        actual_seq = int(book["current_seq"])
        if actual_seq != int(proposal["applied_seq"]) or (
            expected_seq is not None and expected_seq != actual_seq
        ):
            raise ClassificationError(
                Code.UNDO_CONFLICT,
                "确认后账本又发生了变化。为避免覆盖后续修改，请先在星图中处理这些变化。",
                409,
            )
        approved = [item for item in store.get_suggestions(conn, proposal_id) if item["status"] == "approved"]
        state = ledger_loader.read_state(conn, proposal["book_id"])
        reverse_ops = [
            {"op": "reassign", "tx_id": item["tx_id"], "to_node_id": item["original_node_id"]}
            for item in approved
            if state["placements"].get(str(item["tx_id"])) != item["original_node_id"]
        ]
        if reverse_ops:
            restored_seq = ledger_service._apply_ops_in_transaction(
                conn,
                proposal["book_id"],
                reverse_ops,
                actual_seq,
                f"撤销 AI 分类 {proposal_id}",
            )
        else:
            restored_seq = actual_seq
        store.mark_undone(conn, proposal_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "proposal_id": proposal_id,
        "status": "undone",
        "restored_tx_ids": [int(item["tx_id"]) for item in approved],
        "ledger_seq": restored_seq,
    }


def list_rules(book_id: str, include_disabled: bool = False) -> dict:
    conn = db.get_conn()
    try:
        ledger_loader.read_book(conn, book_id)
        nodes = ledger_loader.read_state(conn, book_id)["nodes"]
        paths = _node_paths(nodes)
        rules = store.list_rules(conn, book_id, include_disabled)
        for rule in rules:
            rule["target_path"] = paths.get(rule["target_node_id"])
            rule["stale_target"] = rule["target_node_id"] not in paths
        return {"book_id": book_id, "rules": rules}
    finally:
        conn.close()


_RULE_FIELDS = {"counterparty", "item", "pay_method", "source"}


def set_rule_enabled(book_id: str, rule_id: str, enabled: bool) -> dict:
    """启用/停用一条用户确认规则，不会修改任何交易。"""
    if not isinstance(enabled, bool):
        raise ClassificationError(Code.INVALID_RULE, "enabled 必须是布尔值。")
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ledger_loader.read_book(conn, book_id)
        rule = store.get_rule(conn, book_id, rule_id)
        if not rule:
            raise ClassificationError(Code.RULE_NOT_FOUND, "没有找到这条分类规则。", 404)
        store.set_rule_enabled(conn, book_id, rule_id, enabled)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    updated = next(
        item for item in list_rules(book_id, include_disabled=True)["rules"]
        if item["id"] == rule_id
    )
    return {"book_id": book_id, "rule": updated, "message": "规则状态已更新；没有修改账本交易。"}


def update_rule(
    book_id: str,
    rule_id: str,
    *,
    match_field: str | None = None,
    pattern: str | None = None,
    target_node_id: str | None = None,
) -> dict:
    """修改规则匹配字段、文本模式或目标分类，不会直接重分类历史交易。"""
    if match_field is None and pattern is None and target_node_id is None:
        raise ClassificationError(Code.INVALID_RULE, "至少提供一个需要修改的规则字段。")
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ledger_loader.read_book(conn, book_id)
        current = store.get_rule(conn, book_id, rule_id)
        if not current:
            raise ClassificationError(Code.RULE_NOT_FOUND, "没有找到这条分类规则。", 404)

        next_field = match_field if match_field is not None else current["match_field"]
        next_pattern = pattern.strip() if pattern is not None else current["pattern"]
        next_target = target_node_id if target_node_id is not None else current["target_node_id"]

        if next_field not in _RULE_FIELDS:
            raise ClassificationError(
                Code.INVALID_RULE,
                "match_field 只能是 counterparty、item、pay_method 或 source。",
            )
        if not next_pattern or len(next_pattern) > 200:
            raise ClassificationError(Code.INVALID_RULE, "pattern 必须为 1 到 200 个字符。")

        state = ledger_loader.read_state(conn, book_id)
        valid_nodes = {
            node["id"] for node in state["nodes"] if node.get("kind") == "group"
        }
        if next_target not in valid_nodes:
            raise ClassificationError(Code.TARGET_NOT_ALLOWED, "规则目标必须是当前账本中的 group 节点。")

        try:
            store.update_rule(
                conn, book_id, rule_id, next_field, next_pattern,
                next_pattern.casefold(), next_target,
            )
        except sqlite3.IntegrityError as exc:
            raise ClassificationError(
                Code.RULE_CONFLICT,
                "相同匹配字段和文本模式的规则已经存在，请先修改或删除重复规则。",
                409,
            ) from exc
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    updated = next(
        item for item in list_rules(book_id, include_disabled=True)["rules"]
        if item["id"] == rule_id
    )
    return {"book_id": book_id, "rule": updated, "message": "规则已更新；不会追溯修改历史交易。"}


def delete_rule(book_id: str, rule_id: str) -> dict:
    """永久删除一条规则，不会删除该规则此前产生的分类审核记录。"""
    conn = db.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ledger_loader.read_book(conn, book_id)
        rule = store.get_rule(conn, book_id, rule_id)
        if not rule:
            raise ClassificationError(Code.RULE_NOT_FOUND, "没有找到这条分类规则。", 404)
        store.delete_rule(conn, book_id, rule_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"book_id": book_id, "rule_id": rule_id, "deleted": True}
