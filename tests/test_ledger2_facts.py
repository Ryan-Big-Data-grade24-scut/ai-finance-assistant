"""账本 V2 · 事实适配层（backend.ledger2.facts）：真库行 → 产品口径的 Tx（整数分、±1 方向）。"""
from __future__ import annotations

import pytest

from backend.ledger2 import facts
from backend.ledger2.errors import LedgerError
from backend.ledger2.model import Scope
from ledger2_support import DATA, add_tx, init_all, make_db


@pytest.fixture()
def conn(tmp_path):
    c = make_db(tmp_path / 'facts.db')
    yield c
    c.close()


@pytest.fixture()
def empty(tmp_path):
    c = make_db(tmp_path / 'facts_empty.db', rows=[])
    yield c
    c.close()


def _delete(conn, tx_id):
    conn.execute('INSERT INTO tx_deletions(tx_id) VALUES(?)', (tx_id,))
    conn.commit()


# ── load_txs ────────────────────────────────────────────────────────────────

def test_load_txs_units_direction_and_fields(conn):
    txs = facts.load_txs(conn)
    assert sorted(txs) == [1, 2, 3, 4, 5, 6, 8]
    t1, t2 = txs[1], txs[2]
    assert (t1.amount, t1.direction, t1.role, t1.category) == (3000, -1, 'primary', '餐饮')   # 整数分，支出 -1
    assert (t2.amount, t2.direction, t2.category) == (800000, 1, '工资')                       # 收入 +1
    assert t1.tx_time == '2026-01-05 10:00:00' and t1.date == '2026-01-05'
    assert (t1.counterparty, t1.item, t1.source) == ('甲', '午饭', '测试支付')
    assert t1.note is None and t2.note is None


def test_load_txs_rounds_fractional_yuan_to_whole_cents(empty):
    a = add_tx(empty, tx_time='2026-03-05 10:00:00', direction='支出', amount=12.34, counterparty='店', item='x')
    b = add_tx(empty, tx_time='2026-03-05 11:00:00', direction='支出', amount=0.1 + 0.2, counterparty='店', item='y')
    init_all(empty)
    txs = facts.load_txs(empty)
    assert txs[a].amount == 1234 and txs[b].amount == 30          # 不能出现 29/30.000000004 这类浮点残渣


def test_load_txs_neutral_direction_counts_as_expense(empty):
    a = add_tx(empty, tx_time='2026-03-06 10:00:00', direction='中性', amount=9, tx_type='购物', counterparty='店', item='x')
    init_all(empty)
    t = facts.load_txs(empty)[a]
    assert (t.amount, t.direction) == (900, -1)                   # 中性按支出记，与 atom(…, 0) 一致


def test_load_txs_skips_deleted(conn):
    _delete(conn, 2)
    assert 2 not in facts.load_txs(conn)
    assert len(facts.load_txs(conn)) == 6


def test_category_priority_manual_over_tx_type_over_fallback(empty):
    a = add_tx(empty, tx_time='2026-03-01 10:00:00', direction='支出', amount=10, tx_type='餐饮', counterparty='店', item='a')
    b = add_tx(empty, tx_time='2026-03-01 11:00:00', direction='支出', amount=10, counterparty='店', item='b')
    c = add_tx(empty, tx_time='2026-03-01 12:00:00', direction='支出', amount=10, tx_type='餐饮', counterparty='店', item='c')
    empty.execute("INSERT INTO tx_overrides(tx_id, manual_category) VALUES(?, '早餐')", (a,))
    init_all(empty)
    txs = facts.load_txs(empty)
    assert txs[a].category == '早餐'          # 手工分类最高
    assert txs[b].category == '其他'          # tx_type 也没有 → 兜底
    assert txs[c].category == '餐饮'
    assert [facts.category_of(empty, x) for x in (a, b, c)] == ['早餐', '其他', '餐饮']


def test_refund_pair_keeps_both_raw_atoms(empty):
    """事实层不配对：退款和原单各自是各自的样子，净额交给 +2 组。"""
    a = add_tx(empty, tx_time='2026-03-01 10:00:00', direction='支出', amount=40, tx_type='购物',
               counterparty='店', item='鞋', tx_no='O1')
    b = add_tx(empty, tx_time='2026-03-02 10:00:00', direction='收入', amount=40, tx_type='退款',
               counterparty='店', item='退款-鞋', tx_no='O1')
    init_all(empty)
    txs = facts.load_txs(empty)
    assert (txs[a].amount, txs[a].direction, txs[a].note) == (4000, -1, None)
    assert (txs[b].amount, txs[b].direction, txs[b].note) == (4000, 1, None)


def test_partial_refund_does_not_shrink_the_original(empty):
    a = add_tx(empty, tx_time='2026-03-01 10:00:00', direction='支出', amount=100, tx_type='购物',
               counterparty='店', item='鞋', tx_no='O1')
    b = add_tx(empty, tx_time='2026-03-02 10:00:00', direction='收入', amount=30, tx_type='退款',
               counterparty='店', item='退款-鞋', tx_no='O1')
    init_all(empty)
    txs = facts.load_txs(empty)
    assert (txs[a].amount, txs[a].direction, txs[a].note) == (10000, -1, None)
    assert (txs[b].amount, txs[b].direction, txs[b].note) == (3000, 1, None)


def test_own_account_transfer_pair_keeps_both_atoms(empty):
    out = add_tx(empty, tx_time='2026-03-03 10:00:00', direction='支出', amount=500, tx_type='转账',
                 counterparty='本人', item='本人账户互转', merchant_no='M9')
    inn = add_tx(empty, tx_time='2026-03-03 10:01:00', direction='收入', amount=500, tx_type='转账',
                 counterparty='本人', item='本人账户互转', merchant_no='M9')
    init_all(empty)
    txs = facts.load_txs(empty)
    assert (txs[out].amount, txs[out].direction, txs[out].note) == (50000, -1, None)
    assert (txs[inn].amount, txs[inn].direction, txs[inn].note) == (50000, 1, None)


def test_lone_transfer_is_an_ordinary_atom(empty):
    a = add_tx(empty, tx_time='2026-03-03 10:00:00', direction='支出', amount=500, tx_type='转账',
               counterparty='朋友', item='还钱', merchant_no='M9')
    init_all(empty)
    t = facts.load_txs(empty)[a]
    assert (t.amount, t.direction, t.note) == (50000, -1, None)


def test_load_txs_orders_nothing_but_returns_a_dict_keyed_by_id(conn):
    txs = facts.load_txs(conn)
    assert all(k == v.id for k, v in txs.items())


# ── 分类与存活 ──────────────────────────────────────────────────────────────

def test_category_node_id_roundtrip():
    nid = facts.category_node_id('餐饮')
    assert nid == 'cat:餐饮' and facts.is_category_node(nid) and facts.category_name(nid) == '餐饮'
    assert not facts.is_category_node('root') and not facts.is_category_node('abc123')


def test_category_of_rejects_missing_and_deleted(conn):
    with pytest.raises(LedgerError) as e:
        facts.category_of(conn, 999)
    assert e.value.code == 'TX_NOT_FOUND'
    _delete(conn, 1)
    with pytest.raises(LedgerError) as e:
        facts.category_of(conn, 1)
    assert e.value.code == 'TX_NOT_FOUND'


def test_category_of_any_tolerates_deleted_but_not_missing(conn):
    _delete(conn, 1)
    assert facts.category_of_any(conn, 1) == '餐饮'
    with pytest.raises(LedgerError) as e:
        facts.category_of_any(conn, 999)
    assert e.value.code == 'TX_NOT_FOUND'


def test_category_exists_only_counts_live_transactions(conn):
    assert facts.category_exists(conn, '工资') and not facts.category_exists(conn, '不存在')
    _delete(conn, 2)
    assert not facts.category_exists(conn, '工资')       # 唯一一笔被删 → 分类不再存在


def test_live_ids(conn):
    assert facts.live_ids(conn) == {1, 2, 3, 4, 5, 6, 8}
    _delete(conn, 5)
    assert facts.live_ids(conn) == {1, 2, 3, 4, 6, 8}


# ── 范围 ────────────────────────────────────────────────────────────────────

def test_scope_by_date_is_inclusive_and_skips_deleted(conn):
    s = facts.scope_by_date(conn, '2026-01-05', '2026-01-09')
    assert s.tx_ids == frozenset({1, 2, 3})                 # 两端闭区间
    _delete(conn, 2)
    assert facts.scope_by_date(conn, '2026-01-05', '2026-01-09').tx_ids == frozenset({1, 3})
    assert facts.scope_by_date(conn, '2030-01-01', '2030-01-02').tx_ids == frozenset()


def test_scope_by_date_covers_every_fixture_row(conn):
    first = min(r['tx_time'][:10] for r in DATA)
    last = max(r['tx_time'][:10] for r in DATA)
    assert len(facts.scope_by_date(conn, first, last).tx_ids) == len(DATA)


def test_scope_of_ids():
    s = facts.scope_of_ids(['1', 2, 2])
    assert isinstance(s, Scope) and s.tx_ids == frozenset({1, 2})
