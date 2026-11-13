"""V3.1 外部 AI 分类：草案零写入、确认原子落账、规则复用与整体撤销。"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import db
from backend.classification import service as classification
from backend.classification.errors import ClassificationError
from backend.ledger import service as ledger


@pytest.fixture
def classified_book(tmp_path: Path, monkeypatch):
    path = tmp_path / "ai-classification.db"
    monkeypatch.setattr(db, "DB_PATH", path)
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
                1, 8, f"ai-test-{tx_id}", "{}",
            ),
        )
    conn.commit()
    conn.close()
    tree = ledger.create_book("AI 分类测试", "2026-10-01", "2026-10-31")
    return tree


def _minor_node(tree: dict, major_name: str) -> str:
    by_id = {node["id"]: node for node in tree["nodes"]}
    major = next(node for node in tree["nodes"] if node["name"] == major_name)
    return next(
        node["id"] for node in tree["nodes"]
        if node["parent_id"] == major["id"] and node["name"] == "未细分"
    )


def test_preview_confirm_rule_reuse_and_undo(classified_book):
    book_id = classified_book["book"]["id"]
    traffic = _minor_node(classified_book, "交通")
    original_node = next(
        tx["node_id"] for tx in classified_book["transactions"] if tx["id"] == 1
    )

    batch = classification.get_batch(book_id, limit=10)
    assert batch["pending_total"] == 3
    assert {item["tx_id"] for item in batch["items"]} == {1, 2, 3}

    draft = classification.preview(
        book_id,
        [
            {
                "tx_id": 1,
                "target_node_id": traffic,
                "confidence": 0.92,
                "reason": "用户希望把这家店按通勤消费处理。",
                "remember_rule": True,
                "rule_field": "counterparty",
                "rule_pattern": "虚构咖啡",
            }
        ],
        expected_seq=classified_book["seq"],
    )
    assert draft["requires_user_confirmation"] is True
    assert ledger.get_tree(book_id)["seq"] == 0, "预览不得改账"
    assert next(tx for tx in ledger.get_tree(book_id)["transactions"] if tx["id"] == 1)["node_id"] == original_node

    applied = classification.confirm(draft["proposal_id"], [1], expected_seq=0)
    assert applied["changed_count"] == 1
    tree = ledger.get_tree(book_id)
    assert tree["seq"] == 1
    assert next(tx for tx in tree["transactions"] if tx["id"] == 1)["node_id"] == traffic
    assert classification.get_status(book_id)["reviewed"] == 1

    next_batch = classification.get_batch(book_id, limit=10)
    tx2 = next(item for item in next_batch["items"] if item["tx_id"] == 2)
    assert tx2["rule_hints"][0]["target_node_id"] == traffic
    assert classification.list_rules(book_id)["rules"][0]["enabled"] == 1

    undone = classification.undo(draft["proposal_id"], expected_seq=1)
    assert undone["status"] == "undone" and undone["ledger_seq"] == 2
    restored = ledger.get_tree(book_id)
    assert next(tx for tx in restored["transactions"] if tx["id"] == 1)["node_id"] == original_node
    assert classification.get_status(book_id)["reviewed"] == 0
    assert classification.list_rules(book_id)["rules"] == []


def test_clarification_cannot_be_confirmed(classified_book):
    book_id = classified_book["book"]["id"]
    draft = classification.preview(
        book_id,
        [
            {
                "tx_id": 3,
                "target_node_id": None,
                "confidence": 0.3,
                "reason": "看不出这是通勤还是旅行。",
                "needs_clarification": True,
                "question": "这笔公交费属于日常通勤吗？",
            }
        ],
        expected_seq=0,
    )
    assert draft["needs_clarification"][0]["question"]
    with pytest.raises(ClassificationError) as error:
        classification.confirm(draft["proposal_id"], [3], expected_seq=0)
    assert error.value.code == "CONFIRMATION_REQUIRED"
    assert ledger.get_tree(book_id)["seq"] == 0


def test_stale_draft_is_rejected_without_partial_write(classified_book):
    book_id = classified_book["book"]["id"]
    traffic = _minor_node(classified_book, "交通")
    draft = classification.preview(
        book_id,
        [{"tx_id": 1, "target_node_id": traffic, "confidence": 0.8, "reason": "测试草案"}],
        expected_seq=0,
    )
    dining_major = next(node for node in classified_book["nodes"] if node["name"] == "餐饮")
    ledger.apply_op(
        book_id,
        {"op": "set_op", "node_id": dining_major["id"], "op_value": "NET"},
        expected_seq=0,
    )
    with pytest.raises(ClassificationError) as error:
        classification.confirm(draft["proposal_id"], [1], expected_seq=0)
    assert error.value.code == "STALE_PROPOSAL"
    assert classification.get_status(book_id)["reviewed"] == 0
