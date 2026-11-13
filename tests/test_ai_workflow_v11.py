from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import db
from backend.classification import service as classification
from backend.classification.errors import ClassificationError, Code
from backend.ledger import service as ledger


@pytest.fixture
def workflow_book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "ai-workflow-v11.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    db.init_db(path)
    conn = db.get_conn(path)
    conn.execute(
        "INSERT INTO platforms(id,name,kind,adapter) VALUES('wechat','微信支付','wallet','wechat')"
    )
    rows = [
        (1, "2026-10-01 08:10:00", 18.0, "虚构咖啡店", "拿铁", "餐饮"),
        (2, "2026-10-02 09:20:00", 12.0, "虚构咖啡二店", "豆浆", "餐饮"),
        (3, "2026-10-03 10:30:00", 6.0, "虚构公交", "公交乘车", "交通"),
    ]
    for tx_id, tx_time, amount, counterparty, item, tx_type in rows:
        date = tx_time[:10]
        conn.execute(
            "INSERT INTO transactions_raw"
            "(id,source,platform_id,tx_time,direction,amount,pay_method,counterparty,item,tx_type,"
            "status,date,month,year,weekday,hour,dedupe_key,raw_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tx_id, "微信支付", "wechat", tx_time, "支出", amount, "零钱",
                counterparty, item, tx_type, "支付成功", date, date[:7], date[:4],
                1, 8, f"ai-v11-{tx_id}", json.dumps({}, ensure_ascii=False),
            ),
        )
    conn.commit()
    conn.close()
    return ledger.create_book("AI 工作流 V1.1 测试", "2026-10-01", "2026-10-31")


def _minor_node(tree: dict, major_name: str) -> str:
    major = next(node for node in tree["nodes"] if node["name"] == major_name)
    return next(
        node["id"] for node in tree["nodes"]
        if node["parent_id"] == major["id"] and node["name"] == "未细分"
    )


def _change_book_seq(book: dict) -> None:
    major = next(node for node in book["nodes"] if node["name"] == "餐饮")
    ledger.apply_op(
        book["book"]["id"],
        {"op": "set_op", "node_id": major["id"], "op_value": "NET"},
        expected_seq=0,
    )


def test_draft_can_be_listed_and_resumed_after_ai_context_reset(workflow_book):
    book_id = workflow_book["book"]["id"]
    draft = classification.preview(
        book_id,
        [{
            "tx_id": 3, "target_node_id": None, "confidence": 0.2,
            "reason": "无法确定这是通勤还是旅行。",
            "needs_clarification": True,
            "question": "这笔公交费用于日常通勤吗？",
        }],
        expected_seq=0,
    )

    listed = classification.list_proposals(book_id, status="draft", limit=10)
    assert listed["returned"] == 1
    assert listed["proposals"][0]["id"] == draft["proposal_id"]
    assert listed["proposals"][0]["clarification_count"] == 1
    assert listed["proposals"][0]["stale"] is False

    resumed = classification.get_proposal(draft["proposal_id"])
    assert resumed["clarification_count"] == 1
    assert resumed["ready_count"] == 0
    assert resumed["current_seq"] == 0
    assert resumed["suggestions"][0]["current_path"]


def test_user_answer_resolves_original_clarification_without_writing_ledger(workflow_book):
    book_id = workflow_book["book"]["id"]
    batch = classification.get_batch(book_id, limit=10)
    tx = next(item for item in batch["items"] if item["tx_id"] == 3)
    target = _minor_node(workflow_book, "交通")
    draft = classification.preview(
        book_id,
        [{
            "tx_id": 3, "target_node_id": None, "confidence": 0.25,
            "reason": "需要确认用途。", "needs_clarification": True,
            "question": "这是日常通勤吗？",
        }],
        expected_seq=batch["ledger_seq"],
    )

    resolved = classification.resolve_clarification(
        draft["proposal_id"],
        {
            "tx_id": 3, "target_node_id": target, "confidence": 0.98,
            "reason": "用户说明这是每天上下班乘坐公交。",
        },
        user_answer="这是每天上下班的公交费用。",
        expected_seq=batch["ledger_seq"],
    )

    assert resolved["id"] == draft["proposal_id"]
    assert resolved["clarification_count"] == 0
    assert resolved["ready_count"] == 1
    assert "用户澄清" in resolved["suggestions"][0]["reason"]
    assert ledger.get_tree(book_id)["seq"] == 0

    confirmed = classification.confirm(draft["proposal_id"], [3], expected_seq=0)
    assert confirmed["status"] == "applied"
    assert next(tx for tx in ledger.get_tree(book_id)["transactions"] if tx["id"] == 3)["node_id"] == target


def test_conflicting_user_rules_are_blocked_at_preview_not_only_prompted(workflow_book):
    book_id = workflow_book["book"]["id"]
    traffic = _minor_node(workflow_book, "交通")
    dining = _minor_node(workflow_book, "餐饮")
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO ai_classification_rules"
            "(id,book_id,match_field,pattern,pattern_norm,target_node_id) VALUES(?,?,?,?,?,?)",
            ("rule-a", book_id, "counterparty", "虚构", "虚构", traffic),
        )
        conn.execute(
            "INSERT INTO ai_classification_rules"
            "(id,book_id,match_field,pattern,pattern_norm,target_node_id) VALUES(?,?,?,?,?,?)",
            ("rule-b", book_id, "counterparty", "公交", "公交", dining),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ClassificationError) as error:
        classification.preview(
            book_id,
            [{"tx_id": 3, "target_node_id": traffic, "confidence": 0.9, "reason": "模型擅自选择。"}],
            expected_seq=0,
        )
    assert error.value.code == Code.RULE_HINT_CONFLICT

    clarification = classification.preview(
        book_id,
        [{
            "tx_id": 3, "target_node_id": None, "confidence": 0.2,
            "reason": "两条用户规则冲突。", "needs_clarification": True,
            "question": "这笔交易应归入交通还是餐饮？",
        }],
        expected_seq=0,
    )
    assert clarification["needs_clarification"]


def test_stale_draft_is_marked_and_cannot_be_resolved(workflow_book):
    book_id = workflow_book["book"]["id"]
    draft = classification.preview(
        book_id,
        [{
            "tx_id": 3, "target_node_id": None, "confidence": 0.2,
            "reason": "需要用户确认。", "needs_clarification": True,
            "question": "这笔公交费用于通勤吗？",
        }],
        expected_seq=0,
    )
    _change_book_seq(workflow_book)

    listed = classification.list_proposals(book_id, status="draft")
    assert listed["proposals"][0]["stale"] is True
    with pytest.raises(ClassificationError) as error:
        classification.resolve_clarification(
            draft["proposal_id"],
            {"tx_id": 3, "target_node_id": _minor_node(workflow_book, "交通"),
             "confidence": 0.9, "reason": "用户确认。"},
            user_answer="这是通勤。",
        )
    assert error.value.code == Code.STALE_PROPOSAL


def test_resolve_only_accepts_a_pending_clarification(workflow_book):
    book_id = workflow_book["book"]["id"]
    target = _minor_node(workflow_book, "交通")
    draft = classification.preview(
        book_id,
        [{"tx_id": 3, "target_node_id": target, "confidence": 0.9, "reason": "已明确。"}],
        expected_seq=0,
    )
    with pytest.raises(ClassificationError) as error:
        classification.resolve_clarification(
            draft["proposal_id"],
            {"tx_id": 3, "target_node_id": target, "confidence": 0.9, "reason": "重复处理。"},
            user_answer="这笔是公交。",
        )
    assert error.value.code == Code.CLARIFICATION_NOT_PENDING


def test_remember_rule_pattern_must_match_real_transaction_field(workflow_book):
    book_id = workflow_book["book"]["id"]
    target = _minor_node(workflow_book, "餐饮")
    with pytest.raises(ClassificationError) as error:
        classification.preview(
            book_id,
            [{
                "tx_id": 1, "target_node_id": target, "confidence": 0.9,
                "reason": "分类。", "remember_rule": True,
                "rule_field": "counterparty", "rule_pattern": "完全不相关的关键词",
            }],
            expected_seq=0,
        )
    assert error.value.code == Code.INVALID_SUGGESTION


def test_partial_confirmation_closes_unapproved_suggestions_explicitly(workflow_book):
    book_id = workflow_book["book"]["id"]
    target = _minor_node(workflow_book, "交通")
    draft = classification.preview(
        book_id,
        [
            {"tx_id": 1, "target_node_id": target, "confidence": 0.9, "reason": "用户确认的第一笔。"},
            {"tx_id": 2, "target_node_id": target, "confidence": 0.8, "reason": "用户确认的第二笔。"},
        ],
        expected_seq=0,
    )
    result = classification.confirm(draft["proposal_id"], [1], expected_seq=0)
    assert result["approved_tx_ids"] == [1]
    assert result["rejected_tx_ids"] == [2]
    proposal = classification.get_proposal(draft["proposal_id"])
    statuses = {item["tx_id"]: item["status"] for item in proposal["suggestions"]}
    assert statuses == {1: "approved", 2: "rejected"}


def test_get_proposal_reports_stale_state_after_ledger_change(workflow_book):
    book_id = workflow_book["book"]["id"]
    draft = classification.preview(
        book_id,
        [{
            "tx_id": 3, "target_node_id": None, "confidence": 0.2,
            "reason": "等待澄清。", "needs_clarification": True,
            "question": "请确认用途。",
        }],
        expected_seq=0,
    )
    _change_book_seq(workflow_book)
    proposal = classification.get_proposal(draft["proposal_id"])
    assert proposal["stale"] is True
    assert proposal["current_seq"] == 1
