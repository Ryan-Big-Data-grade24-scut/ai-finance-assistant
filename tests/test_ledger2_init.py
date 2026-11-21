"""账本 V2 · 初始化 / 卸载 / 人工字段（数据层）：

* `[t1..tn; op]_init`：建节点 → 放置 → 分裂 → 组 +2（同父检查）→ 写人工字段；整体原子；可撤销；
* 初始化标记防重复；没初始化的交易在所有读出里被过滤；
* `[t1..tn]_uninst`：注销 4 个初始化字段，并把交易从树里拿走；
* 平台分类预设（presets）= 用 init 的一种拼法。
"""
from __future__ import annotations

import pytest

from backend.ledger2 import facts, ops, presets
from backend.ledger2.errors import LedgerError
from backend.ledger2.model import NetGroup, Node, Tx
from backend.ledger2.ops import init_txs, redo, uninst_txs, undo, update_manual
from backend.ledger2.reader import read_ledger, read_scope
from ledger2_support import add_tx, cat, child, group, init_all, make_db, reset_log, snapshot, table_count, tx_ids


@pytest.fixture()
def raw(tmp_path):
    """7 笔虚构交易，一笔都没初始化。"""
    c = make_db(tmp_path / 'init.db', init=False)
    yield c
    c.close()


def manual(conn, tx_id):
    r = conn.execute("SELECT * FROM ledger2_tx WHERE tx_id=? AND role='primary'", (tx_id,)).fetchone()
    return None if r is None else dict(r)


def place(conn, tx_id, role='primary'):
    r = conn.execute('SELECT node_id, net_group_id FROM ledger2_tx WHERE tx_id=? AND role=?',
                     (tx_id, role)).fetchone()
    return None if r is None else (r['node_id'], r['net_group_id'])


# ── 读出必须以「已初始化」为前提 ─────────────────────────────────────────────

def test_uninitialized_txs_are_filtered_from_every_read(raw):
    assert facts.load_txs(raw) == {}
    root = read_ledger(raw)
    assert root.children == [] and root.value == (0, 0)
    assert facts.scope_by_date(raw, '2026-01-01', '2026-12-31').tx_ids == frozenset()
    assert sorted(facts.uninitialized_ids(raw)) == [1, 2, 3, 4, 5, 6, 8]


def test_only_initialized_txs_show_up(raw):
    init_txs(raw, [1, 3])
    root = read_ledger(raw)
    assert sorted(tx_ids(root)) == [1, 3] and root.value == (5000, 0)
    assert sorted(facts.load_txs(raw)) == [1, 3]
    assert sorted(facts.scope_by_date(raw, '2026-01-01', '2026-12-31').tx_ids) == [1, 3]


def test_uninitialized_original_does_not_affect_a_visible_refund(raw):
    """事实层不配对：只初始化退款那一笔，它就是自己的收入 40.00；原单不可见，也不会改写它。"""
    a = add_tx(raw, tx_time='2026-03-01 10:00:00', direction='支出', amount=40, tx_type='购物',
               counterparty='店', item='鞋', tx_no='ORD-1')
    b = add_tx(raw, tx_time='2026-03-02 10:00:00', direction='收入', amount=40, tx_type='退款',
               counterparty='店', item='退款-鞋', tx_no='ORD-1')
    raw.commit()
    init_txs(raw, [b])
    txs = facts.load_txs(raw)
    assert sorted(txs) == [b] and (txs[b].amount, txs[b].direction) == (4000, 1)
    assert a in facts.uninitialized_ids(raw)


# ── 默认形态：放根下、字段为空 ───────────────────────────────────────────────

def test_plain_init_puts_txs_under_root_with_empty_fields(raw):
    init_txs(raw, [1, 2])
    assert place(raw, 1) == ('root', None) and place(raw, 2) == ('root', None)
    m = manual(raw, 1)
    assert (m['initialized'], m['title'], m['note'], m['category_path']) == (1, '', '', '[]')
    assert (m['node_id'], m['net_group_id']) == ('root', None)
    t = facts.load_txs(raw)[1]
    assert (t.title, t.memo, t.category_path) == ('', '', [])


def test_init_marks_txs_and_forbids_a_second_init(raw):
    init_txs(raw, [1])
    before = snapshot(raw)
    with pytest.raises(LedgerError) as e:
        init_txs(raw, [1, 3])                       # 3 没初始化过，但整批原子：1 已经初始化 → 整体拒绝
    assert e.value.code == 'ALREADY_INITIALIZED'
    assert snapshot(raw) == before and manual(raw, 3) is None


def test_init_is_one_log_entry_and_undo_redo_restores_rows_exactly(raw):
    before = snapshot(raw)
    init_txs(raw, [1, 3], {'nodes': [{'ref': 'a', 'name': '早餐'}], 'place': [{'node': 'a', 'txs': [1, 3]}],
                           'meta': {1: {'title': '包子', 'note': '豆浆'}}})
    after = snapshot(raw)
    assert table_count(raw, 'ledger2_op_log') == 1 and ops.history(raw)[0]['name'] == 'init'
    undo(raw)
    assert snapshot(raw) == before and facts.load_txs(raw) == {}
    redo(raw)
    assert snapshot(raw) == after


# ── op：节点（新旧）/ 路径 ───────────────────────────────────────────────────

def test_op_places_txs_into_new_and_existing_nodes(raw):
    out = init_txs(raw, [1, 3, 4], {
        'nodes': [{'ref': 'food', 'name': '餐饮'}, {'ref': 'brk', 'name': '早餐', 'parent': 'food'}],
        'place': [{'node': 'brk', 'txs': [1]}, {'node': 'food', 'txs': [3]}],
    })
    food, brk = out['nodes']['food'], out['nodes']['brk']
    assert place(raw, 1)[0] == brk and place(raw, 3)[0] == food and place(raw, 4)[0] == 'root'
    paths = {t.id: t.category_path for t in facts.load_txs(raw).values()}
    assert paths == {1: ['餐饮', '早餐'], 3: ['餐饮'], 4: []}          # 完整路径（根以下各级名）
    # 再来一批，放进刚建好的旧节点
    init_txs(raw, [5], {'place': [{'node': brk, 'txs': [5]}]})
    assert facts.load_txs(raw)[5].category_path == ['餐饮', '早餐']
    root = read_ledger(raw)
    assert sorted(tx_ids(child(child(root, '餐饮'), '早餐'))) == [1, 5]


def test_op_new_node_can_pin_its_id(raw):
    out = init_txs(raw, [1], {'nodes': [{'ref': 'x', 'name': 'X', 'id': 'my-node'}], 'place': [{'node': 'my-node', 'txs': [1]}]})
    assert out['nodes'] == {'x': 'my-node'} and place(raw, 1)[0] == 'my-node'


def test_op_meta_sets_title_and_note(raw):
    init_txs(raw, [1, 3], {'meta': {1: {'title': ' 包子 ', 'note': '备注A'}}})
    t = facts.load_txs(raw)
    assert (t[1].title, t[1].memo) == ('包子', '备注A') and (t[3].title, t[3].memo) == ('', '')


# ── op：分裂 ─────────────────────────────────────────────────────────────────

def test_op_split_defaults_to_the_same_path(raw):
    init_txs(raw, [3], {'nodes': [{'ref': 'm', 'name': 'M'}], 'place': [{'node': 'm', 'txs': [3]}],
                        'splits': [{'tx': 3}]})
    m = place(raw, 3)[0]
    assert place(raw, 3, 'mirror') == (m, None)
    assert table_count(raw, 'ledger2_split_tx') == 1
    node = child(read_ledger(raw), 'M')
    assert sorted((c.id, c.role) for c in node.children if isinstance(c, Tx)) == [(3, 'mirror'), (3, 'primary')]
    assert node.value == (2000, 2000)


def test_op_split_can_pick_another_position(raw):
    out = init_txs(raw, [3], {'nodes': [{'ref': 'a', 'name': 'A'}, {'ref': 'b', 'name': 'B'}],
                              'place': [{'node': 'a', 'txs': [3]}], 'splits': [{'tx': 3, 'node': 'b'}]})
    assert place(raw, 3)[0] == out['nodes']['a'] and place(raw, 3, 'mirror')[0] == out['nodes']['b']


def test_op_split_twice_is_rejected(raw):
    with pytest.raises(LedgerError) as e:
        init_txs(raw, [3], {'splits': [{'tx': 3}, {'tx': 3}]})
    assert e.value.code == 'ALREADY_SPLIT' and manual(raw, 3) is None


# ── op：+2 组（在放置、分裂之后做同父检查）──────────────────────────────────

def test_op_group_in_the_same_node(raw):
    out = init_txs(raw, [1, 6], {'nodes': [{'ref': 'm', 'name': 'M'}], 'place': [{'node': 'm', 'txs': [1, 6]}],
                                 'groups': [{'name': '对冲', 'txs': [1, 6]}]})
    node = child(read_ledger(raw), 'M')
    g = group(node, '对冲')
    assert g.id == out['groups'][0] and [m.id for m in g.members] == [1, 6]
    assert g.value == (0, 2000) and node.value == (0, 2000)             # ν(支出30, 收入50)
    assert place(raw, 1) == (node.id, g.id)


def test_op_group_across_nodes_is_rejected_and_atomic(raw):
    before = snapshot(raw)
    with pytest.raises(LedgerError) as e:
        init_txs(raw, [1, 6], {'nodes': [{'ref': 'a', 'name': 'A'}, {'ref': 'b', 'name': 'B'}],
                               'place': [{'node': 'a', 'txs': [1]}, {'node': 'b', 'txs': [6]}],
                               'groups': [{'txs': [1, 6]}]})
    assert e.value.code == 'SAME_PARENT_REQUIRED'
    assert snapshot(raw) == before                                      # 连新节点也没建
    assert table_count(raw, 'ledger2_op_log') == 0


def test_op_group_member_may_be_a_fresh_mirror(raw):
    """组在分裂之后做，所以成员可以是刚分出来的 t′（txm:N）。"""
    init_txs(raw, [1, 3], {'nodes': [{'ref': 'm', 'name': 'M'}], 'place': [{'node': 'm', 'txs': [1, 3]}],
                           'splits': [{'tx': 3}], 'groups': [{'name': 'g', 'txs': ['tx:1', 'txm:3']}]})
    g = group(child(read_ledger(raw), 'M'), 'g')
    assert sorted((m.id, m.role) for m in g.members) == [(1, 'primary'), (3, 'mirror')]


def test_op_group_needs_two_members_and_known_mirror(raw):
    with pytest.raises(LedgerError) as e:
        init_txs(raw, [1], {'groups': [{'txs': [1]}]})
    assert e.value.code == 'GROUP_TOO_SMALL'
    with pytest.raises(LedgerError) as e:
        init_txs(raw, [1, 3], {'groups': [{'txs': ['tx:1', 'txm:3']}]})   # 3 没分裂，没有 t′
    assert e.value.code == 'NOT_IN_INIT_SET'


def test_op_group_member_cannot_be_in_two_groups(raw):
    with pytest.raises(LedgerError) as e:
        init_txs(raw, [1, 3, 4], {'groups': [{'txs': [1, 3]}, {'txs': [3, 4]}]})
    assert e.value.code == 'ALREADY_GROUPED'


# ── 非法输入：一律原子失败 ───────────────────────────────────────────────────

@pytest.mark.parametrize('ids, op, code', [
    ([], None, 'EMPTY_SELECTION'),
    ([1, 1], None, 'DUPLICATE_TX'),
    ([999], None, 'TX_NOT_FOUND'),
    ([1], {'place': [{'node': 'nope', 'txs': [1]}]}, 'NODE_NOT_FOUND'),
    ([1], {'place': [{'node': 'root', 'txs': [3]}]}, 'NOT_IN_INIT_SET'),
    ([1], {'place': [{'node': 'root', 'txs': [1]}, {'node': 'root', 'txs': [1]}]}, 'DUPLICATE_PLACE'),
    ([1], {'nodes': [{'ref': 'a', 'name': '  '}]}, 'EMPTY_NAME'),
    ([1], {'nodes': [{'ref': 'a', 'name': 'A'}, {'ref': 'a', 'name': 'B'}]}, 'BAD_OP'),
    ([1], {'nodes': [{'ref': 'a', 'name': 'A', 'parent': 'ghost'}]}, 'NODE_NOT_FOUND'),
    ([1], {'meta': {3: {'title': 'x'}}}, 'NOT_IN_INIT_SET'),
    ([1], {'splits': [{'tx': 3}]}, 'NOT_IN_INIT_SET'),
])
def test_invalid_init_fails_atomically(raw, ids, op, code):
    before = snapshot(raw)
    with pytest.raises(LedgerError) as e:
        init_txs(raw, ids, op)
    assert e.value.code == code
    assert snapshot(raw) == before and table_count(raw, 'ledger2_op_log') == 0


def test_deleted_tx_cannot_be_initialized(raw):
    raw.execute('INSERT INTO tx_deletions(tx_id) VALUES(1)')
    raw.commit()
    with pytest.raises(LedgerError) as e:
        init_txs(raw, [1])
    assert e.value.code == 'TX_NOT_FOUND'


# ── 平台分类预设 ─────────────────────────────────────────────────────────────

def test_preset_creates_one_node_per_platform_category(raw):
    out = presets.init_by_platform_category(raw)
    assert sorted(out['nodes']) == ['cat:交通', 'cat:其他收入', 'cat:工资', 'cat:购物', 'cat:餐饮']
    root = read_ledger(raw)
    assert sorted(tx_ids(cat(root, '餐饮'))) == [1, 3, 4] and cat(root, '餐饮').value == (7500, 0)
    assert root.value == (22500, 805000)
    assert facts.uninitialized_ids(raw) == []


def test_preset_copies_immutable_fields_into_title_note_and_path(raw):
    raw.execute("UPDATE transactions_raw SET remark='备注一' WHERE id=1")
    raw.commit()
    presets.init_by_platform_category(raw)
    t = facts.load_txs(raw)
    assert (t[1].title, t[1].memo, t[1].category_path) == ('午饭', '备注一', ['餐饮'])
    assert (t[2].title, t[2].memo, t[2].category_path) == ('月薪', '', ['工资'])


def test_preset_title_falls_back_to_counterparty(raw):
    n = add_tx(raw, tx_time='2026-03-01 10:00:00', direction='支出', amount=9, tx_type='购物', counterparty='某店')
    raw.commit()
    presets.init_by_platform_category(raw, [n])
    assert facts.load_txs(raw)[n].title == '某店'


def test_preset_reuses_an_existing_category_node(raw):
    presets.init_by_platform_category(raw, [1])
    nodes = table_count(raw, 'ledger2_node')
    presets.init_by_platform_category(raw, [3, 4])                       # 同属「餐饮」：不再建节点
    assert table_count(raw, 'ledger2_node') == nodes + 0
    assert sorted(tx_ids(cat(read_ledger(raw), '餐饮'))) == [1, 3, 4]


def test_preset_is_idempotent_by_marker_not_by_luck(raw):
    presets.init_by_platform_category(raw)
    with pytest.raises(LedgerError) as e:
        presets.init_by_platform_category(raw)                           # 没有未初始化的了
    assert e.value.code == 'NO_EFFECT'
    with pytest.raises(LedgerError) as e:
        presets.init_by_platform_category(raw, [1])
    assert e.value.code == 'ALREADY_INITIALIZED'


def test_preset_picks_up_late_arrivals(raw):
    presets.init_by_platform_category(raw)
    n = add_tx(raw, tx_time='2026-03-01 10:00:00', direction='支出', amount=9, tx_type='餐饮', counterparty='店', item='夜宵')
    raw.commit()
    assert facts.uninitialized_ids(raw) == [n]
    presets.init_by_platform_category(raw)
    assert n in tx_ids(cat(read_ledger(raw), '餐饮'))


# ── 卸载 ─────────────────────────────────────────────────────────────────────

def test_uninst_clears_the_four_fields_and_removes_the_tx_from_the_tree(raw):
    presets.init_by_platform_category(raw)
    uninst_txs(raw, [1])
    assert manual(raw, 1) is None and place(raw, 1) is None
    assert 1 not in facts.load_txs(raw) and 1 in facts.uninitialized_ids(raw)
    assert 1 not in tx_ids(cat(read_ledger(raw), '餐饮'))
    assert ops.history(raw)[-1]['name'] == 'uninst'


def test_uninst_then_reinit_is_allowed(raw):
    presets.init_by_platform_category(raw)
    uninst_txs(raw, [1])
    init_txs(raw, [1], {'place': [{'node': 'cat:购物', 'txs': [1]}], 'meta': {1: {'title': '重来'}}})
    assert facts.load_txs(raw)[1].title == '重来' and place(raw, 1)[0] == 'cat:购物'


def test_uninst_undo_redo_restores_rows_exactly(raw):
    presets.init_by_platform_category(raw)
    before = snapshot(raw)
    uninst_txs(raw, [1, 2])
    after = snapshot(raw)
    undo(raw)
    assert snapshot(raw) == before
    redo(raw)
    assert snapshot(raw) == after


def test_uninst_removes_the_mirror_too(raw):
    init_txs(raw, [3], {'splits': [{'tx': 3}]})
    uninst_txs(raw, [3])
    assert table_count(raw, 'ledger2_split_tx') == 0 and place(raw, 3, 'mirror') is None
    assert table_count(raw, 'ledger2_tx') == 0


def test_uninst_member_dissolves_a_group_that_drops_below_two(raw):
    init_txs(raw, [1, 6], {'nodes': [{'ref': 'm', 'name': 'M'}], 'place': [{'node': 'm', 'txs': [1, 6]}],
                           'groups': [{'name': 'g', 'txs': [1, 6]}]})
    before = snapshot(raw)
    uninst_txs(raw, [1])
    assert table_count(raw, 'ledger2_net_group') == 0                    # R3：只剩 1 人 → 解散
    m = place(raw, 6)
    assert m is not None and m[1] is None                                # t6 变回普通成员
    assert tx_ids(child(read_ledger(raw), 'M')) == [6]
    undo(raw)
    assert snapshot(raw) == before


def test_uninst_member_keeps_a_group_with_three_members(raw):
    init_txs(raw, [1, 3, 4], {'nodes': [{'ref': 'm', 'name': 'M'}], 'place': [{'node': 'm', 'txs': [1, 3, 4]}],
                              'groups': [{'name': 'g', 'txs': [1, 3, 4]}]})
    uninst_txs(raw, [4])
    g = group(child(read_ledger(raw), 'M'), 'g')
    assert [m.id for m in g.members] == [1, 3]


def test_uninst_whole_group_at_once(raw):
    init_txs(raw, [1, 6], {'groups': [{'name': 'g', 'txs': [1, 6]}]})
    uninst_txs(raw, [1, 6])
    assert table_count(raw, 'ledger2_net_group') == 0 and table_count(raw, 'ledger2_tx') == 0


@pytest.mark.parametrize('ids, code', [([], 'EMPTY_SELECTION'), ([1, 1], 'DUPLICATE_TX'), ([1], 'NOT_INITIALIZED')])
def test_invalid_uninst_is_rejected(raw, ids, code):
    with pytest.raises(LedgerError) as e:
        uninst_txs(raw, ids)
    assert e.value.code == code


def test_uninst_is_atomic_when_one_tx_is_not_initialized(raw):
    init_txs(raw, [1])
    before = snapshot(raw)
    with pytest.raises(LedgerError) as e:
        uninst_txs(raw, [1, 3])
    assert e.value.code == 'NOT_INITIALIZED' and snapshot(raw) == before


# ── 标题 / 备注 ──────────────────────────────────────────────────────────────

def test_update_manual_changes_title_and_note_with_undo(raw):
    init_txs(raw, [1])
    update_manual(raw, 1, title=' 新标题 ')
    t = facts.load_txs(raw)[1]
    assert (t.title, t.memo) == ('新标题', '')
    update_manual(raw, 1, note='n')
    assert facts.load_txs(raw)[1].memo == 'n' and facts.load_txs(raw)[1].title == '新标题'
    undo(raw)
    assert facts.load_txs(raw)[1].memo == ''
    undo(raw)
    assert facts.load_txs(raw)[1].title == ''


def test_update_manual_does_not_touch_category_path(raw):
    presets.init_by_platform_category(raw, [1])
    update_manual(raw, 1, title='x')
    assert facts.load_txs(raw)[1].category_path == ['餐饮']


def test_update_manual_rejects_uninitialized_and_noop(raw):
    with pytest.raises(LedgerError) as e:
        update_manual(raw, 1, title='x')
    assert e.value.code == 'NOT_INITIALIZED'
    init_txs(raw, [1])
    with pytest.raises(LedgerError) as e:
        update_manual(raw, 1, title='')
    assert e.value.code == 'NO_EFFECT'


# ── 分类字段：初始化时写完整路径，之后不静默改（静默刷新是后续课题）──────────

def test_category_path_is_written_once_and_not_refreshed_by_later_moves(raw):
    presets.init_by_platform_category(raw)
    ops.rename_node(raw, 'cat:餐饮', '吃饭')
    ops.move(raw, ['tx:1'], 'cat:购物')
    assert facts.load_txs(raw)[1].category_path == ['餐饮']             # 仍是初始化那一刻的路径
    root = read_ledger(raw)
    assert tx_ids(next(c for c in root.children if c.id == 'cat:餐饮')) == [3, 4]


# ── 范围读出 + 初始化 ────────────────────────────────────────────────────────

def test_scope_projection_ignores_uninitialized_txs(raw):
    init_txs(raw, [1, 4])                                               # t1 在 1 月，t4 在 2 月
    proj = read_scope(read_ledger(raw), facts.scope_by_date(raw, '2026-01-01', '2026-01-31'))
    assert tx_ids(proj) == [1]


# ── 节点的备注：初始化可写，之后可单独改 ────────────────────────────────────

def test_init_can_write_node_note(raw):
    res = init_txs(raw, [1, 3], {
        'nodes': [{'ref': 'a', 'name': '吃饭', 'parent': 'root', 'note': ' 工作日午餐 '}],
        'place': [{'node': 'a', 'txs': [1, 3]}],
    })
    n = next(c for c in read_ledger(raw).children if c.id == res['nodes']['a'])
    assert (n.name, n.note) == ('吃饭', '工作日午餐')
    undo(raw)
    assert read_ledger(raw).children == []


def test_update_node_changes_name_and_note_and_is_undoable(raw):
    init_txs(raw, [1])
    nid = ops.create_node(raw, 'M', ['tx:1'])
    snap = snapshot(raw)

    ops.update_node(raw, nid, note='  备注  ')
    m = child(read_ledger(raw), 'M')
    assert (m.name, m.note) == ('M', '备注')

    ops.update_node(raw, nid, name=' 新名字 ', note='')
    m = child(read_ledger(raw), '新名字')
    assert (m.name, m.note) == ('新名字', '')

    with pytest.raises(LedgerError) as e:
        ops.update_node(raw, nid, name='新名字', note='')
    assert e.value.code == 'NO_EFFECT'

    undo(raw)
    undo(raw)
    assert snapshot(raw) == snap
    redo(raw)
    assert child(read_ledger(raw), 'M').note == '备注'


def test_root_can_note_but_cannot_be_renamed(raw):
    with pytest.raises(LedgerError) as e:
        ops.update_node(raw, 'root', name='x')
    assert e.value.code == 'CANNOT_RENAME_ROOT'
    ops.update_node(raw, 'root', note='总账备注')
    assert read_ledger(raw).note == '总账备注'
