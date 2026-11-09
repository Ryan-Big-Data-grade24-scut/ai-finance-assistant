"""AI 分类表的唯一 SQL 写入位置。"""
from __future__ import annotations

from uuid import uuid4


def create_proposal(conn, proposal: dict, suggestions: list[dict]) -> None:
    conn.execute(
        "INSERT INTO ai_classification_proposals"
        "(id,book_id,base_seq,status,created_by) VALUES(?,?,?,'draft',?)",
        (proposal["id"], proposal["book_id"], proposal["base_seq"], proposal["created_by"]),
    )
    conn.executemany(
        "INSERT INTO ai_classification_suggestions"
        "(proposal_id,tx_id,original_node_id,target_node_id,confidence,reason,"
        "needs_clarification,question,remember_rule,rule_field,rule_pattern) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            (
                proposal["id"], item["tx_id"], item["original_node_id"],
                item.get("target_node_id"), item["confidence"], item["reason"],
                int(item["needs_clarification"]), item.get("question"),
                int(item["remember_rule"]), item.get("rule_field"), item.get("rule_pattern"),
            )
            for item in suggestions
        ),
    )


def get_proposal(conn, proposal_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM ai_classification_proposals WHERE id=?", (proposal_id,)
    ).fetchone()
    return dict(row) if row else None


def list_proposals(
    conn, book_id: str, status: str | None = "draft", limit: int = 20
) -> list[dict]:
    sql = (
        "SELECT p.*, "
        "COUNT(s.tx_id) AS suggestion_count, "
        "SUM(CASE WHEN s.needs_clarification=1 AND s.status='pending' THEN 1 ELSE 0 END) "
        "AS clarification_count, "
        "SUM(CASE WHEN s.needs_clarification=0 AND s.status='pending' THEN 1 ELSE 0 END) "
        "AS ready_count, "
        "SUM(CASE WHEN s.status='approved' THEN 1 ELSE 0 END) AS approved_count, "
        "SUM(CASE WHEN s.status='rejected' THEN 1 ELSE 0 END) AS rejected_count "
        "FROM ai_classification_proposals p "
        "LEFT JOIN ai_classification_suggestions s ON s.proposal_id=p.id "
        "WHERE p.book_id=? "
    )
    params: list[object] = [book_id]
    if status is not None:
        sql += "AND p.status=? "
        params.append(status)
    sql += "GROUP BY p.id ORDER BY p.created_at DESC, p.id DESC LIMIT ?"
    params.append(limit)
    return [dict(row) for row in conn.execute(sql, params)]


def get_suggestions(conn, proposal_id: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM ai_classification_suggestions "
            "WHERE proposal_id=? ORDER BY tx_id", (proposal_id,)
        )
    ]


def get_suggestion(conn, proposal_id: str, tx_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM ai_classification_suggestions WHERE proposal_id=? AND tx_id=?",
        (proposal_id, tx_id),
    ).fetchone()
    return dict(row) if row else None


def update_suggestion_resolution(
    conn, proposal_id: str, tx_id: int, suggestion: dict
) -> bool:
    cur = conn.execute(
        "UPDATE ai_classification_suggestions SET target_node_id=?,confidence=?,reason=?,"
        "needs_clarification=0,question=NULL,remember_rule=?,rule_field=?,rule_pattern=? "
        "WHERE proposal_id=? AND tx_id=? AND needs_clarification=1 AND status='pending'",
        (
            suggestion["target_node_id"], suggestion["confidence"], suggestion["reason"],
            int(suggestion["remember_rule"]), suggestion.get("rule_field"),
            suggestion.get("rule_pattern"), proposal_id, tx_id,
        ),
    )
    return cur.rowcount == 1


def reviewed_tx_ids(conn, book_id: str) -> set[int]:
    return {
        int(row["tx_id"])
        for row in conn.execute(
            "SELECT tx_id FROM ai_classification_reviews WHERE book_id=?", (book_id,)
        )
    }


def status_counts(conn, book_id: str) -> dict:
    reviewed = conn.execute(
        "SELECT count(*) FROM ai_classification_reviews r "
        "JOIN ledger_book_tx b ON b.book_id=r.book_id AND b.tx_id=r.tx_id "
        "WHERE r.book_id=?", (book_id,)
    ).fetchone()[0]
    drafts = conn.execute(
        "SELECT count(*) FROM ai_classification_proposals WHERE book_id=? AND status='draft'",
        (book_id,),
    ).fetchone()[0]
    rules = conn.execute(
        "SELECT count(*) FROM ai_classification_rules WHERE book_id=? AND enabled=1",
        (book_id,),
    ).fetchone()[0]
    return {"reviewed": reviewed, "draft_proposals": drafts, "active_rules": rules}


def list_rules(conn, book_id: str, include_disabled: bool = False) -> list[dict]:
    sql = "SELECT * FROM ai_classification_rules WHERE book_id=?"
    if not include_disabled:
        sql += " AND enabled=1"
    sql += " ORDER BY created_at,id"
    return [dict(row) for row in conn.execute(sql, (book_id,))]


def apply_decision(
    conn,
    proposal: dict,
    approved: list[dict],
    rejected_tx_ids: list[int],
    applied_seq: int,
    changed_count: int,
    confirmed_by: str = "user_via_mcp",
    decision_note: str = "用户通过 MCP 确认",
) -> None:
    proposal_id = proposal["id"]
    approved_ids = [int(item["tx_id"]) for item in approved]
    if approved_ids:
        marks = ",".join("?" for _ in approved_ids)
        conn.execute(
            f"UPDATE ai_classification_suggestions SET status='approved' "
            f"WHERE proposal_id=? AND tx_id IN ({marks})",
            (proposal_id, *approved_ids),
        )
    if rejected_tx_ids:
        marks = ",".join("?" for _ in rejected_tx_ids)
        conn.execute(
            f"UPDATE ai_classification_suggestions SET status='rejected' "
            f"WHERE proposal_id=? AND tx_id IN ({marks})",
            (proposal_id, *rejected_tx_ids),
        )

    conn.executemany(
        "INSERT INTO ai_classification_reviews"
        "(book_id,tx_id,proposal_id,target_node_id,confidence,reason,confirmed_by) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            (
                proposal["book_id"], item["tx_id"], proposal_id,
                item["target_node_id"], item["confidence"], item["reason"], confirmed_by,
            )
            for item in approved
        ),
    )

    for item in approved:
        if not item["remember_rule"]:
            continue
        pattern = item["rule_pattern"].strip()
        pattern_norm = pattern.casefold()
        existing = conn.execute(
            "SELECT id FROM ai_classification_rules "
            "WHERE book_id=? AND match_field=? AND pattern_norm=?",
            (proposal["book_id"], item["rule_field"], pattern_norm),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE ai_classification_rules SET pattern=?,target_node_id=?,"
                "source_proposal_id=?,enabled=1,updated_at=datetime('now') WHERE id=?",
                (pattern, item["target_node_id"], proposal_id, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO ai_classification_rules"
                "(id,book_id,match_field,pattern,pattern_norm,target_node_id,source_proposal_id) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    f"rule_{uuid4().hex[:16]}", proposal["book_id"], item["rule_field"],
                    pattern, pattern_norm, item["target_node_id"], proposal_id,
                ),
            )

    conn.execute(
        "UPDATE ai_classification_proposals SET status='applied',applied_seq=?,"
        "changed_count=?,decision_note=?,decided_at=datetime('now') WHERE id=?",
        (applied_seq, changed_count, decision_note, proposal_id),
    )


def reject_proposal(conn, proposal_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE ai_classification_suggestions SET status='rejected' WHERE proposal_id=?",
        (proposal_id,),
    )
    conn.execute(
        "UPDATE ai_classification_proposals SET status='rejected',decision_note=?,"
        "decided_at=datetime('now') WHERE id=?",
        (reason, proposal_id),
    )


def mark_undone(conn, proposal_id: str) -> None:
    conn.execute("DELETE FROM ai_classification_reviews WHERE proposal_id=?", (proposal_id,))
    conn.execute(
        "UPDATE ai_classification_rules SET enabled=0,updated_at=datetime('now') "
        "WHERE source_proposal_id=?",
        (proposal_id,),
    )
    conn.execute(
        "UPDATE ai_classification_proposals SET status='undone',"
        "decision_note='用户撤销 AI 分类',decided_at=datetime('now') WHERE id=?",
        (proposal_id,),
    )


def get_rule(conn, book_id: str, rule_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM ai_classification_rules WHERE book_id=? AND id=?",
        (book_id, rule_id),
    ).fetchone()
    return dict(row) if row else None


def set_rule_enabled(conn, book_id: str, rule_id: str, enabled: bool) -> bool:
    cur = conn.execute(
        "UPDATE ai_classification_rules SET enabled=?,updated_at=datetime('now') "
        "WHERE book_id=? AND id=?",
        (int(enabled), book_id, rule_id),
    )
    return cur.rowcount == 1


def update_rule(
    conn,
    book_id: str,
    rule_id: str,
    match_field: str,
    pattern: str,
    pattern_norm: str,
    target_node_id: str,
) -> bool:
    cur = conn.execute(
        "UPDATE ai_classification_rules SET match_field=?,pattern=?,pattern_norm=?,"
        "target_node_id=?,updated_at=datetime('now') WHERE book_id=? AND id=?",
        (match_field, pattern, pattern_norm, target_node_id, book_id, rule_id),
    )
    return cur.rowcount == 1


def delete_rule(conn, book_id: str, rule_id: str) -> bool:
    cur = conn.execute(
        "DELETE FROM ai_classification_rules WHERE book_id=? AND id=?",
        (book_id, rule_id),
    )
    return cur.rowcount == 1



def upsert_rule(
    conn,
    book_id: str,
    match_field: str,
    pattern: str,
    target_node_id: str,
    source_proposal_id: str | None = None,
) -> str:
    """Create or update a confirmed user classification rule."""
    pattern = pattern.strip()
    pattern_norm = pattern.casefold()
    if match_field not in {"counterparty", "item", "pay_method", "source"}:
        raise ValueError("不支持的分类规则字段。")
    if len(pattern) < 2 or len(pattern) > 120:
        raise ValueError("分类规则关键词长度必须在 2 到 120 个字符之间。")
    existing = conn.execute(
        "SELECT id FROM ai_classification_rules "
        "WHERE book_id=? AND match_field=? AND pattern_norm=?",
        (book_id, match_field, pattern_norm),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE ai_classification_rules SET pattern=?,target_node_id=?,"
            "source_proposal_id=?,enabled=1,updated_at=datetime('now') WHERE id=?",
            (pattern, target_node_id, source_proposal_id, existing["id"]),
        )
        return str(existing["id"])
    rule_id = f"rule_{uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO ai_classification_rules"
        "(id,book_id,match_field,pattern,pattern_norm,target_node_id,source_proposal_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (rule_id, book_id, match_field, pattern, pattern_norm, target_node_id, source_proposal_id),
    )
    return rule_id
