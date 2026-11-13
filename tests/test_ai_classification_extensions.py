from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import db
from backend.classification import service as classification
from backend.classification.errors import ClassificationError, Code
from backend.ledger import service as ledger


def _insert_tx(conn, tx_id, *, direction, amount, tx_type, status, counterparty,
               item, tx_no, merchant_no="", raw=None):
    tx_time = f"2026-10-{tx_id:02d} 12:00:00"
    date = tx_time[:10]
    conn.execute(
        "INSERT INTO transactions_raw"
        "(id,source,platform_id,tx_time,direction,amount,pay_method,counterparty,item,tx_type,"
        "status,tx_no,merchant_no,remark,date,month,year,weekday,hour,dedupe_key,raw_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            tx_id, "微信支付", "wechat", tx_time, direction, amount, "零钱",
            counterparty, item, tx_type, status, tx_no, merchant_no, "",
            date, date[:7], date[:4], 1, 12, f"ai-extension-{tx_id}",
            json.dumps(raw or {}, ensure_ascii=False),
        ),
    )




@pytest.fixture
def classified_book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "ai-classification-rules.db"
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
    for tx_id, tx_time, amount, counterparty, item, category in rows:
        date = tx_time[:10]
        conn.execute(
            "INSERT INTO transactions_raw"
            "(id,source,platform_id,tx_time,direction,amount,pay_method,counterparty,item,tx_type,"
            "status,date,month,year,weekday,hour,dedupe_key,raw_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tx_id, "微信支付", "wechat", tx_time, "支出", amount, "零钱",
                counterparty, item, category, "支付成功", date, date[:7], date[:4],
                1, 8, f"ai-rules-{tx_id}", "{}",
            ),
        )
    conn.commit()
    conn.close()
    return ledger.create_book("AI 规则管理测试", "2026-10-01", "2026-10-31")


def _minor_node(tree: dict, major_name: str) -> str:
    major = next(node for node in tree["nodes"] if node["name"] == major_name)
    return next(
        node["id"] for node in tree["nodes"]
        if node["parent_id"] == major["id"] and node["name"] == "未细分"
    )


def test_classification_batch_exposes_effective_amount_and_excludes_special_rows(tmp_path: Path, monkeypatch):
    path = tmp_path / "classification-special.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    db.init_db(path)
    conn = db.get_conn(path)
    conn.execute(
        "INSERT INTO platforms(id,name,kind,adapter) VALUES('wechat','微信支付','wallet','wechat')"
    )
    _insert_tx(
        conn, 1, direction="支出", amount=100, tx_type="商户消费", status="交易成功",
        counterparty="模拟商户A", item="模拟消费", tx_no="mock-original-1",
        raw={"成功退款（元）": "30.00"},
    )
    _insert_tx(
        conn, 2, direction="收入", amount=30, tx_type="退款", status="退款成功",
        counterparty="模拟商户A", item="退款", tx_no="mock-refund-2",
        raw={"原交易号": "mock-original-1"},
    )
    _insert_tx(
        conn, 3, direction="支出", amount=50, tx_type="转账", status="已全额退款",
        counterparty="模拟联系人", item="微信转账", tx_no="mock-original-3",
        merchant_no="mock-order-3",
    )
    _insert_tx(
        conn, 4, direction="收入", amount=50, tx_type="转账-退款", status="已全额退款",
        counterparty="", item="微信转账退款", tx_no="mock-refund-4",
    )
    _insert_tx(
        conn, 5, direction="支出", amount=40, tx_type="转账", status="对方已收钱",
        counterparty="本人账户", item="本人账户互转", tx_no="mock-transfer-out",
        merchant_no="mock-self-transfer",
    )
    _insert_tx(
        conn, 6, direction="收入", amount=40, tx_type="转账", status="已存入零钱",
        counterparty="本人账户", item="本人账户互转", tx_no="mock-transfer-in",
        merchant_no="mock-self-transfer",
    )
    _insert_tx(
        conn, 7, direction="支出", amount=15, tx_type="商户消费", status="支付成功",
        counterparty="模拟商户B", item="普通消费", tx_no="mock-normal-7",
    )
    conn.commit()
    conn.close()

    book = ledger.create_book("特殊交易分类测试", "2026-10-01", "2026-10-31")
    book_id = book["book"]["id"]
    batch = classification.get_batch(book_id, limit=20)

    assert batch["pending_total"] == 2
    by_id = {item["tx_id"]: item for item in batch["items"]}
    assert set(by_id) == {1, 7}
    assert by_id[1]["amount"] == 100.0
    assert by_id[1]["refunded_amount"] == 30.0
    assert by_id[1]["effective_amount"] == 70.0
    assert by_id[1]["is_partial_refund"] is True
    assert batch["excluded_special"] == {
        "refund_record": 2,
        "fully_refunded": 1,
        "internal_transfer": 2,
    }
    target_node = next(node["id"] for node in book["nodes"] if node.get("kind") == "group")
    with pytest.raises(ClassificationError) as error:
        classification.preview(
            book_id,
            [{"tx_id": 2, "target_node_id": target_node, "confidence": 0.9, "reason": "不能分类退款流水"}],
        )
    assert error.value.code == Code.SPECIAL_TX_NOT_CLASSIFIABLE

    status = classification.get_status(book_id)
    assert status["eligible_total"] == 2
    assert status["pending"] == 2


def test_rule_management_disable_update_and_delete(classified_book):
    book_id = classified_book["book"]["id"]
    traffic = _minor_node(classified_book, "交通")
    draft = classification.preview(
        book_id,
        [{
            "tx_id": 1,
            "target_node_id": traffic,
            "confidence": 0.95,
            "reason": "用户确认的规则管理测试。",
            "remember_rule": True,
            "rule_field": "counterparty",
            "rule_pattern": "虚构咖啡",
        }],
        expected_seq=classified_book["seq"],
    )
    classification.confirm(draft["proposal_id"], [1], expected_seq=0)
    rule = classification.list_rules(book_id)["rules"][0]
    rule_id = rule["id"]

    disabled = classification.set_rule_enabled(book_id, rule_id, False)
    assert disabled["rule"]["enabled"] == 0
    tx2 = next(item for item in classification.get_batch(book_id)["items"] if item["tx_id"] == 2)
    assert tx2["rule_hints"] == []

    updated = classification.update_rule(
        book_id, rule_id, match_field="item", pattern="豆浆", target_node_id=traffic
    )
    assert updated["rule"]["match_field"] == "item"
    assert updated["rule"]["pattern"] == "豆浆"
    assert updated["rule"]["target_node_id"] == traffic

    classification.set_rule_enabled(book_id, rule_id, True)
    tx2 = next(item for item in classification.get_batch(book_id)["items"] if item["tx_id"] == 2)
    assert len(tx2["rule_hints"]) == 1
    assert tx2["rule_suggested_target_node_id"] == traffic
    assert tx2["rule_hint_conflict"] is False

    deleted = classification.delete_rule(book_id, rule_id)
    assert deleted["deleted"] is True
    assert classification.list_rules(book_id, include_disabled=True)["rules"] == []


def test_conflicting_user_rules_are_reported_as_conflict(classified_book):
    book_id = classified_book["book"]["id"]
    traffic = _minor_node(classified_book, "交通")
    dining = _minor_node(classified_book, "餐饮")
    # Create rules through the same persistence table used by confirmed suggestions.
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO ai_classification_rules"
            "(id,book_id,match_field,pattern,pattern_norm,target_node_id) "
            "VALUES(?,?,?,?,?,?)",
            ("rule-conflict-a", book_id, "counterparty", "虚构咖啡", "虚构咖啡", traffic),
        )
        conn.execute(
            "INSERT INTO ai_classification_rules"
            "(id,book_id,match_field,pattern,pattern_norm,target_node_id) "
            "VALUES(?,?,?,?,?,?)",
            ("rule-conflict-b", book_id, "counterparty", "咖啡二店", "咖啡二店", dining),
        )
        conn.commit()
    finally:
        conn.close()

    tx2 = next(item for item in classification.get_batch(book_id)["items"] if item["tx_id"] == 2)
    assert tx2["rule_hint_conflict"] is True
    assert tx2["rule_suggested_target_node_id"] is None
    assert len(tx2["rule_hints"]) == 2
