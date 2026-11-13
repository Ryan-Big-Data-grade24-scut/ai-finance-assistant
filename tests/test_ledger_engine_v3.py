"""第二轮记账代数引擎回归：原子腿、退款、内部转账、外部额度、NET 与支付宝格式验收。"""
from __future__ import annotations
import json
import random
from decimal import Decimal
from pathlib import Path

import pytest
from backend import db
from backend.importer import import_file
from backend.ledger import service
from backend.ledger.amount import cents, net
from backend.ledger.atoms import analyze_special_transactions
from backend.ledger.errors import LedgerError
from backend.ledger.invariants import validate

FIXTURE = Path(__file__).resolve().parents[1] / 'sample_data' / '支付宝格式_引擎验收_虚构数据.csv'


@pytest.fixture
def alipay_acceptance_db(tmp_path: Path, monkeypatch):
    path = tmp_path / 'alipay-acceptance.db'
    monkeypatch.setattr(db, 'DB_PATH', path)
    db.init_db(path)
    imported = import_file(FIXTURE, platform_id='alipay')
    conn = db.get_conn(path)
    external = conn.execute("SELECT id FROM transactions_raw WHERE tx_no='T202609100010'").fetchone()
    assert external is not None
    conn.execute("INSERT INTO tx_overrides(tx_id, funding, updated_by) VALUES(?, '亲情卡', 'test')",
                 (external['id'],))
    conn.commit(); conn.close()
    return {'path': path, 'imported': imported}


def test_amount_is_integer_cent_and_net_is_idempotent():
    assert cents('0.105') == 11
    assert cents('12.34') == 1234
    assert net(12500, 11240) == (1260, 0)
    for e1 in range(8):
        for i1 in range(8):
            for e2 in range(8):
                for i2 in range(8):
                    assert net(*[a + b for a, b in zip(net(e1, i1), net(e2, i2))]) == net(e1 + e2, i1 + i2)


def test_refund_and_internal_transfer_are_only_zeroed_with_clear_evidence():
    facts = [
        {'id': 1, 'direction': '支出', 'amount': 80, 'item': '虚构耳机购买', 'tx_type': '消费',
         'status': '交易成功', 'merchant_no': 'REF-1', 'tx_no': 'BUY-1', 'raw_json': json.dumps({'成功退款（元）':'80.00'})},
        {'id': 2, 'direction': '收入', 'amount': 80, 'item': '虚构耳机退款', 'tx_type': '退款',
         'status': '退款成功', 'merchant_no': 'REF-1', 'tx_no': 'REF-1', 'raw_json': '{}'},
        {'id': 3, 'direction': '收入', 'amount': 12, 'item': '孤立退款', 'tx_type': '退款',
         'status': '退款成功', 'merchant_no': '', 'tx_no': 'REF-UNMATCHED', 'raw_json': '{}'},
        {'id': 4, 'direction': '支出', 'amount': 500, 'item': '本人账户互转', 'tx_type': '转账',
         'status': '交易成功', 'merchant_no': 'MOVE-1', 'tx_no': 'MOVE-OUT', 'raw_json': '{}'},
        {'id': 5, 'direction': '收入', 'amount': 500, 'item': '本人账户互转', 'tx_type': '转账',
         'status': '交易成功', 'merchant_no': 'MOVE-1', 'tx_no': 'MOVE-IN', 'raw_json': '{}'},
        {'id': 6, 'direction': '支出', 'amount': 500, 'item': '普通商品购买', 'tx_type': '消费',
         'status': '交易成功', 'merchant_no': 'OTHER-1', 'tx_no': 'OTHER', 'raw_json': '{}'},
        {'id': 7, 'direction': '支出', 'amount': 25, 'item': '转账给朋友', 'tx_type': '转账',
         'status': '交易成功', 'merchant_no': 'PERSON-TRANSFER', 'counterparty': '朋友甲', 'tx_no': 'P-OUT', 'raw_json': '{}'},
        {'id': 8, 'direction': '收入', 'amount': 25, 'item': '收到朋友转账', 'tx_type': '转账',
         'status': '交易成功', 'merchant_no': 'PERSON-TRANSFER', 'counterparty': '朋友甲', 'tx_no': 'P-IN', 'raw_json': '{}'},
    ]
    result = analyze_special_transactions(facts)
    assert result['refund_zero_ids'] == {1, 2, 3}
    assert result['refund_pairs'] == [(1, 2)]
    assert result['transfer_zero_ids'] == {4, 5}
    assert 6 not in result['transfer_zero_ids'], '普通消费不能误判为内部划转'
    assert 7 not in result['transfer_zero_ids'] and 8 not in result['transfer_zero_ids'], '转给朋友不等于自有账户互转'
    assert any('未找到' in warning for warning in result['warnings'])


def test_alipay_like_csv_expected_baseline_and_refund_warnings(alipay_acceptance_db):
    imported = alipay_acceptance_db['imported']
    assert imported.inserted == 13
    assert imported.skipped == 0
    conn = db.get_conn()
    assert conn.execute("SELECT 1 FROM transactions_raw WHERE tx_no='T202609140014'").fetchone() is None
    conn.close()

    tree = service.get_live_tree()
    assert tree['count'] == 13
    assert tree['root_e'] == 216.75
    assert tree['root_i'] == 201.20
    assert tree['root_net'] == -15.55
    assert tree['invariants_ok'] is True and tree['audit_ok'] is True
    assert len(tree['invariants']) == 6
    assert len(tree['legs']) == 13
    assert len(tree['refund_pairs']) == 2
    assert len(tree['internal_transfer_pairs']) == 1
    assert any('部分退款' in warning for warning in tree['warnings'])
    partial_original = next(tx for tx in tree['transactions'] if tx['tx_no'] == 'T202609120012')
    partial_refund = next(tx for tx in tree['transactions'] if tx['tx_no'] == 'T202609130013')
    assert partial_original['amount'] == 50.0
    assert partial_original['refunded_amount'] == 10.0
    assert partial_original['effective_amount'] == 40.0
    assert partial_original['partial_refund'] is True
    assert partial_original['refund_zeroed'] is False
    assert partial_refund['refund_zeroed'] is True
    assert partial_refund['effective_amount'] == 0.0
    assert tree['audit']['root_net'] == tree['audit']['expected_net']
    audit = tree['audit']
    assert audit['raw_net'] == 1.25
    assert audit['neutral_direction_delta'] == -4.8
    assert audit['refund_delta'] == -12.0
    assert audit['transfer_delta'] == 0
    assert audit['split_delta'] == 0
    assert audit['root_net'] == -15.55

    tx_by_no = {r['tx_no']: r for r in service.get_live_tree()['transactions']}
    assert tx_by_no['T202609050005']['refund_zeroed'] is True
    assert tx_by_no['T202609070006']['refund_zeroed'] is True
    assert tx_by_no['T202609080007']['refund_zeroed'] is True
    assert tx_by_no['T202609090008']['internal_transfer_zeroed'] is True
    assert tx_by_no['T202609090009']['internal_transfer_zeroed'] is True



def test_split_external_funding_is_symmetric_and_undoable(alipay_acceptance_db):
    before = service.get_live_tree()
    conn = db.get_conn()
    ext_id = int(conn.execute("SELECT id FROM transactions_raw WHERE tx_no='T202609100010'").fetchone()['id'])
    normal_id = int(conn.execute("SELECT id FROM transactions_raw WHERE tx_no='T202609010001'").fetchone()['id'])
    conn.close()

    with pytest.raises(LedgerError) as exc:
        service.apply_op('star-live', {'op': 'split', 'tx_id': normal_id}, before['seq'])
    assert exc.value.code == 'SPLIT_NOT_APPLICABLE'

    split = service.apply_op('star-live', {'op': 'split', 'tx_id': ext_id}, before['seq'])
    assert split['split_count'] == 1
    assert split['root_e'] == 216.75
    assert split['root_i'] == 321.20
    assert split['root_net'] == 104.45
    mirror = next(leg for leg in split['legs'] if leg['tx_id'] == ext_id and leg['role'] == 'mirror')
    assert (mirror['e'], mirror['i']) == (0.0, 120.0)
    assert split['audit']['split_delta'] == 120.0
    ext_node = next(node for node in split['nodes'] if node['id'] == mirror['node_id'])
    assert ext_node['kind'] == 'external_income' and ext_node['op'] == 'ADD'
    assert split['invariants_ok']

    undone = service.undo('star-live', split['seq'])
    assert undone['root_e'] == before['root_e'] and undone['root_i'] == before['root_i']
    assert undone['split_count'] == 0
    redone = service.redo('star-live', undone['seq'])
    assert redone['root_net'] == 104.45 and redone['split_count'] == 1
    unsplit = service.apply_op('star-live', {'op': 'unsplit', 'tx_id': ext_id}, redone['seq'])
    assert unsplit['root_net'] == -15.55 and unsplit['split_count'] == 0


def test_net_group_changes_gross_display_but_preserves_net(alipay_acceptance_db):
    before = service.get_live_tree()
    conn = db.get_conn()
    expense_id = int(conn.execute("SELECT id FROM transactions_raw WHERE tx_no='T202609010001'").fetchone()['id'])
    income_id = int(conn.execute("SELECT id FROM transactions_raw WHERE tx_no='T202609040004'").fetchone()['id'])
    conn.close()
    grouped = service.apply_op('star-live', {
        'op': 'create_node', 'parent_id': before['root_id'], 'name': '验收 NET 组',
        'op_value': 'NET', 'tx_ids': [expense_id, income_id],
    }, before['seq'])
    node = next(n for n in grouped['nodes'] if n['name'] == '验收 NET 组')
    assert node['e'] == 0.0 and node['i'] == 164.50
    assert grouped['root_net'] == before['root_net']
    assert grouped['invariants_ok']



def test_refund_cannot_be_split_even_if_funding_is_external(alipay_acceptance_db):
    tree = service.get_live_tree()
    conn = db.get_conn()
    refund_id = int(conn.execute("SELECT id FROM transactions_raw WHERE tx_no='T202609070006'").fetchone()['id'])
    conn.execute("INSERT INTO tx_overrides(tx_id, funding, updated_by) VALUES(?, '亲情卡', 'test')",
                 (refund_id,))
    conn.commit(); conn.close()
    with pytest.raises(LedgerError) as exc:
        service.apply_op('star-live', {'op': 'split', 'tx_id': refund_id}, tree['seq'])
    assert exc.value.code == 'SPLIT_NOT_APPLICABLE'


def test_live_date_filter_keeps_split_projection_consistent(alipay_acceptance_db):
    base = service.get_live_tree()
    conn = db.get_conn()
    ext_id = int(conn.execute("SELECT id FROM transactions_raw WHERE tx_no='T202609100010'").fetchone()['id'])
    conn.close()
    split = service.apply_op('star-live', {'op': 'split', 'tx_id': ext_id}, base['seq'])
    selected = service.get_live_tree('2026-09-10', '2026-09-10')
    assert selected['count'] == 1
    assert selected['root_e'] == 120.0 and selected['root_i'] == 120.0
    assert selected['root_net'] == 0.0 and selected['split_count'] == 1
    assert selected['invariants_ok']
    earlier = service.get_live_tree('2026-09-01', '2026-09-09')
    assert earlier['split_count'] == 0
    assert not any(node['kind'] == 'external_income' for node in earlier['nodes'])
    assert earlier['count'] == 9 and earlier['invariants_ok']

def test_corrupt_cycle_is_rejected():
    state = {'members': [], 'placements': {}, 'splits': {}, 'nodes': [
        {'id': 'b:root', 'parent_id': None, 'name': 'root', 'op': 'ADD', 'kind': 'root'},
        {'id': 'b:a', 'parent_id': 'b:b', 'name': 'a', 'op': 'ADD', 'kind': 'group'},
        {'id': 'b:b', 'parent_id': 'b:a', 'name': 'b', 'op': 'ADD', 'kind': 'group'},
    ]}
    with pytest.raises(LedgerError) as exc:
        validate(state, [], 'b')
    assert exc.value.code in {'TREE_CYCLE', 'DISCONNECTED_TREE'}


def _refund_fact(
    tx_id: int,
    direction: str,
    amount: str,
    merchant_no: str,
    tx_no: str,
    *,
    tx_type: str = "退款",
    status: str = "退款成功",
    raw_json: str = "{}",
    source: str = "支付宝",
) -> dict:
    return {
        "id": tx_id,
        "direction": direction,
        "amount": amount,
        "source": source,
        "merchant_no": merchant_no,
        "tx_no": tx_no,
        "tx_type": tx_type,
        "status": status,
        "item": "退款记录" if tx_type == "退款" else "原始交易",
        "raw_json": raw_json,
    }


def test_multiple_partial_refunds_accumulate_once_and_zero_refund_rows():
    facts = [
        _refund_fact(1, "支出", "100.00", "ORDER-MULTI", "BUY-MULTI", tx_type="消费", status="交易成功"),
        _refund_fact(2, "收入", "10.00", "ORDER-MULTI", "REFUND-1"),
        _refund_fact(3, "收入", "20.00", "ORDER-MULTI", "REFUND-2"),
    ]
    special = analyze_special_transactions(facts)
    assert special["refund_amounts_by_original"] == {1: 3000}
    assert special["refund_zero_ids"] == {2, 3}
    assert special["partial_refund_ids"] == {1, 2, 3}
    assert special["refund_pairs"] == [(1, 2), (1, 3)]

    state = {
        "members": [1, 2, 3],
        "placements": {"1": "multi:root", "2": "multi:root", "3": "multi:root"},
        "splits": {},
        "nodes": [
            {"id": "multi:root", "parent_id": None, "name": "总账", "op": "ADD", "kind": "root"}
        ],
    }
    proof = validate(state, facts, "multi")
    assert proof["root_e"] == 7000
    assert proof["root_i"] == 0
    assert proof["root_net"] == -7000
    assert proof["audit_ok"] is True


def test_refund_can_link_through_explicit_original_order_reference():
    original = _refund_fact(
        11, "支出", "100.00", "ORDER-EXPLICIT", "BUY-EXPLICIT",
        tx_type="消费", status="交易成功",
    )
    refund = _refund_fact(
        12, "收入", "25.00", "REFUND-ORDER", "REFUND-TX",
        raw_json=json.dumps({"原订单号": "ORDER-EXPLICIT"}, ensure_ascii=False),
    )
    special = analyze_special_transactions([original, refund])
    assert special["refund_pairs"] == [(11, 12)]
    assert special["refund_amounts_by_original"] == {11: 2500}


def test_ambiguous_refund_does_not_reduce_any_candidate_original():
    facts = [
        _refund_fact(21, "支出", "100.00", "DUPLICATE-ORDER", "BUY-A", tx_type="消费", status="交易成功"),
        _refund_fact(22, "支出", "80.00", "DUPLICATE-ORDER", "BUY-B", tx_type="消费", status="交易成功"),
        _refund_fact(23, "收入", "30.00", "DUPLICATE-ORDER", "REFUND-AMBIGUOUS"),
    ]
    special = analyze_special_transactions(facts)
    assert special["refund_pairs"] == []
    assert special["refund_amounts_by_original"] == {}
    assert special["refund_zero_ids"] == {23}
    assert any("多笔候选" in warning for warning in special["warnings"])


def test_equal_amount_without_order_or_transaction_link_is_not_a_refund_pair():
    facts = [
        _refund_fact(31, "支出", "30.00", "ORDER-ORIGINAL", "BUY-31", tx_type="消费", status="交易成功"),
        _refund_fact(32, "收入", "30.00", "UNRELATED-ORDER", "REFUND-32"),
    ]
    special = analyze_special_transactions(facts)
    assert special["refund_pairs"] == []
    assert special["refund_amounts_by_original"] == {}
    assert special["refund_zero_ids"] == {32}
    assert any("未找到明确关联" in warning for warning in special["warnings"])


def test_platform_cumulative_refund_field_works_without_refund_detail_row():
    original = _refund_fact(
        41, "支出", "100.00", "ORDER-RAW-ONLY", "BUY-41",
        tx_type="消费", status="交易成功",
        raw_json=json.dumps({"成功退款（元）": "30.00"}, ensure_ascii=False),
    )
    special = analyze_special_transactions([original])
    assert special["refund_amounts_by_original"] == {41: 3000}
    assert special["partial_refund_ids"] == {41}
    assert special["refund_zero_ids"] == set()


def test_platform_cumulative_amount_is_authoritative_but_mismatch_blocks_split():
    original = _refund_fact(
        51, "支出", "100.00", "ORDER-MISMATCH", "BUY-51",
        tx_type="消费", status="交易成功",
        raw_json=json.dumps({"成功退款（元）": "30.00"}, ensure_ascii=False),
    )
    refund = _refund_fact(52, "收入", "10.00", "ORDER-MISMATCH", "REFUND-52")
    special = analyze_special_transactions([original, refund])
    assert special["refund_amounts_by_original"] == {51: 3000}
    assert special["refund_conflict_ids"] == {51, 52}
    assert any("不一致" in warning for warning in special["warnings"])

    state = {
        "members": [51, 52],
        "placements": {"51": "mismatch:root", "52": "mismatch:root"},
        "splits": {"51": {"funding": "亲情卡"}},
        "nodes": [
            {"id": "mismatch:root", "parent_id": None, "name": "总账", "op": "ADD", "kind": "root"},
            {"id": "mismatch:external", "parent_id": "mismatch:root", "name": "外部收入", "op": "ADD", "kind": "external_income"},
        ],
    }
    with pytest.raises(LedgerError) as exc:
        validate(state, [original, refund], "mismatch")
    assert exc.value.code == "LEG_GENERATION_FAILED"


def test_over_refund_is_flagged_and_never_silently_applied():
    facts = [
        _refund_fact(61, "支出", "100.00", "ORDER-OVER", "BUY-61", tx_type="消费", status="交易成功"),
        _refund_fact(62, "收入", "120.00", "ORDER-OVER", "REFUND-62"),
    ]
    special = analyze_special_transactions(facts)
    assert special["refund_amounts_by_original"] == {}
    assert special["refund_conflict_ids"] == {61, 62}
    assert special["refund_zero_ids"] == {62}
    assert any("超过原交易金额" in warning for warning in special["warnings"])


def test_partial_refund_on_external_funding_splits_only_the_remaining_amount(alipay_acceptance_db):
    conn = db.get_conn()
    original_id = int(conn.execute(
        "SELECT id FROM transactions_raw WHERE tx_no='T202609120012'"
    ).fetchone()["id"])
    conn.execute(
        "INSERT INTO tx_overrides(tx_id,funding,updated_by) VALUES(?,'亲情卡','test')",
        (original_id,),
    )
    conn.commit()
    conn.close()

    before = service.get_live_tree()
    split = service.apply_op(
        "star-live", {"op": "split", "tx_id": original_id}, before["seq"]
    )
    primary = next(leg for leg in split["legs"] if leg["tx_id"] == original_id and leg["role"] == "primary")
    mirror = next(leg for leg in split["legs"] if leg["tx_id"] == original_id and leg["role"] == "mirror")
    assert (primary["e"], primary["i"]) == (40.0, 0.0)
    assert (mirror["e"], mirror["i"]) == (0.0, 40.0)
    assert split["split_count"] == 1
    assert split["invariants_ok"] and split["audit_ok"]


def test_live_date_filter_keeps_partial_refund_amount_from_full_book(alipay_acceptance_db):
    selected = service.get_live_tree(
        date_from="2026-09-12",
        date_to="2026-09-12",
    )
    assert selected["count"] == 1
    assert selected["root_e"] == 40.0
    assert selected["root_i"] == 0.0
    tx = selected["transactions"][0]
    assert tx["partial_refund"] is True
    assert tx["refunded_amount"] == 10.0
    assert tx["effective_amount"] == 40.0
    original_id = tx["id"]
    refund_id = next(
        item["id"] for item in service.get_live_tree()["transactions"]
        if item["tx_no"] == "T202609130013"
    )
    assert (original_id, refund_id) in selected["refund_pairs"]


def _insert_refund_row(conn, *, merchant_no: str, amount: float, tx_no: str, date: str):
    conn.execute(
        "INSERT INTO transactions_raw "
        "(source,platform_id,tx_time,direction,amount,counterparty,item,tx_type,status,"
        "tx_no,merchant_no,remark,date,month,year,weekday,hour,dedupe_key,raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "支付宝", "alipay", f"{date} 12:00:00", "收入", amount,
            "虚构退款商户", "虚构退款到账", "退款", "退款成功",
            tx_no, merchant_no, "测试退款关联", date, date[:7], date[:4],
            0, 12, f"v31-{tx_no}", "{}",
        ),
    )


def test_live_import_of_partial_refund_keeps_existing_split_and_reduces_both_legs(alipay_acceptance_db):
    conn = db.get_conn()
    original_id = int(conn.execute(
        "SELECT id FROM transactions_raw WHERE tx_no='T202609100010'"
    ).fetchone()["id"])
    conn.commit()
    conn.close()

    base = service.get_live_tree()
    split = service.apply_op(
        "star-live", {"op": "split", "tx_id": original_id}, base["seq"]
    )
    conn = db.get_conn()
    _insert_refund_row(
        conn,
        merchant_no="ORDER-EXTERNAL-FUND-010",
        amount=30.0,
        tx_no="V31-REFUND-EXTERNAL-PART",
        date="2026-09-15",
    )
    conn.commit()
    conn.close()

    updated = service.get_live_tree()
    assert updated["split_count"] == 1
    primary = next(leg for leg in updated["legs"] if leg["tx_id"] == original_id and leg["role"] == "primary")
    mirror = next(leg for leg in updated["legs"] if leg["tx_id"] == original_id and leg["role"] == "mirror")
    assert (primary["e"], primary["i"]) == (90.0, 0.0)
    assert (mirror["e"], mirror["i"]) == (0.0, 90.0)
    assert updated["invariants_ok"] and updated["audit_ok"]


def test_live_import_of_full_refund_auto_unsplits_existing_external_transaction(alipay_acceptance_db):
    conn = db.get_conn()
    original_id = int(conn.execute(
        "SELECT id FROM transactions_raw WHERE tx_no='T202609100010'"
    ).fetchone()["id"])
    conn.commit()
    conn.close()

    base = service.get_live_tree()
    split = service.apply_op(
        "star-live", {"op": "split", "tx_id": original_id}, base["seq"]
    )
    conn = db.get_conn()
    _insert_refund_row(
        conn,
        merchant_no="ORDER-EXTERNAL-FUND-010",
        amount=120.0,
        tx_no="V31-REFUND-EXTERNAL-FULL",
        date="2026-09-15",
    )
    conn.commit()
    conn.close()

    updated = service.get_live_tree()
    assert updated["split_count"] == 0
    assert not any(node["kind"] == "external_income" for node in updated["nodes"])
    original = next(tx for tx in updated["transactions"] if tx["id"] == original_id)
    assert original["refund_zeroed"] is True
    assert original["effective_amount"] == 0.0
    assert updated["invariants_ok"] and updated["audit_ok"]


def test_multiple_external_splits_share_one_mirror_node_and_prune_after_last_unsplit(alipay_acceptance_db):
    conn = db.get_conn()
    first_id = int(conn.execute(
        "SELECT id FROM transactions_raw WHERE tx_no='T202609100010'"
    ).fetchone()["id"])
    second_id = int(conn.execute(
        "SELECT id FROM transactions_raw WHERE tx_no='T202609110011'"
    ).fetchone()["id"])
    conn.execute(
        "INSERT INTO tx_overrides(tx_id,funding,updated_by) VALUES(?,'他人代付','test')",
        (second_id,),
    )
    conn.commit()
    conn.close()

    base = service.get_live_tree()
    first = service.apply_op("star-live", {"op": "split", "tx_id": first_id}, base["seq"])
    second = service.apply_op("star-live", {"op": "split", "tx_id": second_id}, first["seq"])
    external_nodes = [node for node in second["nodes"] if node["kind"] == "external_income"]
    assert len(external_nodes) == 1
    assert second["split_count"] == 2

    one_unsplit = service.apply_op("star-live", {"op": "unsplit", "tx_id": first_id}, second["seq"])
    assert one_unsplit["split_count"] == 1
    assert len([node for node in one_unsplit["nodes"] if node["kind"] == "external_income"]) == 1

    all_unsplit = service.apply_op("star-live", {"op": "unsplit", "tx_id": second_id}, one_unsplit["seq"])
    assert all_unsplit["split_count"] == 0
    assert not any(node["kind"] == "external_income" for node in all_unsplit["nodes"])
    assert all_unsplit["invariants_ok"]


def test_seeded_random_operation_sequences_preserve_invariants_and_m0(alipay_acceptance_db):
    rng = random.Random(20261031)
    initial = service.get_live_tree()
    book_id = "star-live"
    initial_net = initial["root_net"]
    tx_ids = [tx["id"] for tx in initial["transactions"]]
    external_id = next(
        tx["id"] for tx in initial["transactions"]
        if tx["tx_no"] == "T202609100010"
    )

    for step in range(35):
        tree = service.get_tree(book_id)
        group_nodes = [node for node in tree["nodes"] if node["kind"] == "group"]
        tx_id = rng.choice(tx_ids)
        action = rng.choice(["reassign", "set_op", "split", "unsplit", "create", "undo", "redo"])

        try:
            if action == "reassign" and group_nodes:
                target = rng.choice(group_nodes)
                result = service.apply_op(
                    book_id,
                    {"op": "reassign", "tx_id": tx_id, "to_node_id": target["id"]},
                    tree["seq"],
                )
            elif action == "set_op" and group_nodes:
                node = rng.choice(group_nodes)
                result = service.apply_op(
                    book_id,
                    {"op": "set_op", "node_id": node["id"], "op_value": rng.choice(["ADD", "NET"])},
                    tree["seq"],
                )
            elif action == "split":
                result = service.apply_op(
                    book_id, {"op": "split", "tx_id": external_id}, tree["seq"]
                )
            elif action == "unsplit":
                result = service.apply_op(
                    book_id, {"op": "unsplit", "tx_id": external_id}, tree["seq"]
                )
            elif action == "create" and group_nodes:
                result = service.apply_op(
                    book_id,
                    {
                        "op": "create_node",
                        "parent_id": tree["root_id"],
                        "name": f"随机组-{step}",
                        "op_value": rng.choice(["ADD", "NET"]),
                        "tx_ids": [tx_id],
                    },
                    tree["seq"],
                )
            elif action == "undo" and tree["can_undo"]:
                result = service.undo(book_id, tree["seq"])
            elif action == "redo" and tree["can_redo"]:
                result = service.redo(book_id, tree["seq"])
            else:
                continue
        except LedgerError:
            # Duplicate split / unsplit and invalid reassign targets are expected
            # rejected operations; state must remain valid after each rejection.
            result = service.get_tree(book_id)

        assert result["invariants_ok"] is True
        assert result["audit_ok"] is True
        assert round(result["root_i"] - result["root_e"], 2) == round(result["root_net"], 2)
        assert len(result["legs"]) == len(tx_ids) + result["split_count"]

    current = service.get_tree(book_id)
    reset = service.reset_to_m0(book_id, current["seq"])
    assert reset["split_count"] == 0
    assert reset["root_net"] == initial_net
    assert reset["invariants_ok"] and reset["audit_ok"]



def test_randomized_nested_net_idempotence_property():
    rng = random.Random(314159)
    for _ in range(2500):
        e1, i1, e2, i2 = (rng.randrange(0, 10_000_000) for _ in range(4))
        left_e, left_i = net(e1, i1)
        right_e, right_i = net(e2, i2)
        nested = net(left_e + right_e, left_i + right_i)
        direct = net(e1 + e2, i1 + i2)
        assert nested == direct
