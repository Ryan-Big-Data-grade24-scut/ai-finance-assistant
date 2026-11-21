"""账本代数 V2 原型 · 契约测试。

对照 docs/设计/第二轮/账本需求原文.md 的 N1–N21。
"""
from __future__ import annotations

import pytest

from prototypes.ledger2 import (
    LedgerError, init_db, seed_transactions, read_ledger, read_scope, scope_by_date, to_json,
    create_node, move, dissolve_node, create_net_group, add_group_member, remove_group_member,
    split_tx, unsplit_tx, undo, redo, history,
)
from prototypes.ledger2.algebra import atom, mirror, net, plus1
from prototypes.ledger2.model import NetGroup, Node, Scope, Tx

DATA = [
    dict(id=1, tx_time='2026-01-05 10:00:00', date='2026-01-05', direction=-1, amount=3000, category='餐饮', counterparty='甲', item='午饭', source='t'),
    dict(id=2, tx_time='2026-01-08 09:00:00', date='2026-01-08', direction=1, amount=800000, category='工资', counterparty='乙', item='月薪', source='t'),
    dict(id=3, tx_time='2026-01-09 19:00:00', date='2026-01-09', direction=-1, amount=2000, category='餐饮', counterparty='甲', item='晚饭', source='t'),
    dict(id=4, tx_time='2026-02-02 12:00:00', date='2026-02-02', direction=-1, amount=2500, category='餐饮', counterparty='甲', item='外卖', source='t'),
    dict(id=5, tx_time='2026-02-03 08:00:00', date='2026-02-03', direction=-1, amount=10000, category='交通', counterparty='丙', item='月票', source='t'),
    dict(id=6, tx_time='2026-01-20 21:00:00', date='2026-01-20', direction=1, amount=5000, category='退款', counterparty='丁', item='退款', source='t'),
    dict(id=8, tx_time='2026-01-21 11:00:00', date='2026-01-21', direction=-1, amount=5000, category='购物', counterparty='戊', item='日用品', source='t'),
]


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / 'ledger.db')
    seed_transactions(c, DATA)
    return c


def snapshot(c) -> dict:
    """全部账本表的逐行快照 —— 用来验证撤销是逐字段还原，不是「看起来差不多」。"""
    out = {}
    for table in ('ledger_node', 'ledger_placement', 'ledger_net_group', 'ledger_split_tx'):
        rows = c.execute(f'SELECT * FROM {table}').fetchall()
        out[table] = sorted(tuple(dict(r).values()) for r in rows)
    return out


def children_of(node: Node, name: str) -> Node:
    for c in node.children:
        if isinstance(c, Node) and c.name == name:
            return c
    raise AssertionError(f'没有找到节点 {name}；实际子项：{[getattr(x, "name", x) for x in node.children]}')


def group_of(node: Node, name: str) -> NetGroup:
    for c in node.children:
        if isinstance(c, NetGroup) and c.name == name:
            return c
    raise AssertionError(f'没有找到组 {name}')


# ── 代数 ────────────────────────────────────────────────────────────────────

def test_atom_and_net():
    assert atom(3000, -1) == (3000, 0)      # 支出
    assert atom(800000, 1) == (0, 800000)    # 收入
    assert atom(500, 0) == (500, 0)          # 中性记支出
    # ν：先 +1，再两个分量同时减去较小者
    assert net([(3000, 0), (0, 5000)]) == (0, 2000)
    # ν 幂等：嵌套任意多层都只扣一次
    x = net([(3000, 0), (0, 5000)])
    assert net([x, (1000, 0)]) == net([(3000, 0), (0, 5000), (1000, 0)])
    assert net([net([x])]) == net([x])


def test_plus1_is_componentwise():
    assert plus1([(1, 2), (3, 4), (5, 6)]) == (9, 12)


# ── N2 / N5：基础账本零写入 ──────────────────────────────────────────────────

def test_base_ledger_writes_nothing(conn):
    root = read_ledger(conn)
    assert conn.execute('SELECT COUNT(*) FROM ledger_placement').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM ledger_net_group').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM ledger_node').fetchone()[0] == 1   # 只有根
    # 读出 = [[t1],[t2],…]+1
    assert root.value == (22500, 805000)      # e=支出 225.00，i=收入 8050.00
    assert len(root.children) == 7


# ── N16：建 M 时自动排除被覆盖的子项 ────────────────────────────────────────

def test_create_node_folds_covered_items(conn):
    inner = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    outer = create_node(conn, '餐饮（合并）', ['tx:1', 'tx:3', inner])
    root = read_ledger(conn)
    merged = children_of(root, '餐饮（合并）')
    # 被覆盖的 t1/t3 不再作为直接子项
    assert not any(isinstance(c, Tx) for c in merged.children)
    # inner 节点整体搬进了新节点，结构保留
    assert children_of(merged, '餐饮').value == (5000, 0)


def test_create_node_uses_lca_of_parents(conn):
    inner = create_node(conn, '餐饮', ['tx:1'])
    mid = create_node(conn, '餐饮·子类', ['tx:3'])
    # t1 在 inner 下，t3 在 mid 下 → LCA 是根
    new = create_node(conn, '合并', ['tx:1', 'tx:3'])
    root = read_ledger(conn)
    merged = children_of(root, '合并')
    assert merged.id == new
    assert merged.value == (5000, 0)


# ── N17：移动的集合唯一、内部不合并 ──────────────────────────────────────────

def test_move_keeps_structure(conn):
    inner = create_node(conn, '餐饮', ['tx:1'])
    outer = create_node(conn, '餐饮组', [inner])
    root = read_ledger(conn)
    target = children_of(root, '餐饮组')
    move(conn, ['tx:3'], target.id)
    root = read_ledger(conn)
    target = children_of(root, '餐饮组')
    # tx3 直接挂到目标节点下，不与 inner 合并
    assert any(isinstance(c, Tx) and c.id == 3 for c in target.children)
    assert children_of(target, '餐饮').value == (3000, 0)


def test_move_into_own_subtree_is_rejected(conn):
    outer = create_node(conn, '外层', ['tx:1'])
    inner = create_node(conn, '内层', ['tx:1'])      # 从外层里的交易建 → 内层成为外层的子节点
    # 把外层塞进内层 → 内层在外层子树里 → 成环
    with pytest.raises(LedgerError) as e:
        move(conn, [outer], inner)
    assert e.value.code == 'CYCLE'


# ── N18 / Q1：+2 必须同父，不能放宽 ─────────────────────────────────────────

def test_group_across_parents_is_rejected(conn):
    food = create_node(conn, '餐饮', ['tx:1'])
    create_node(conn, '工资', ['tx:2'])
    with pytest.raises(LedgerError) as e:
        create_net_group(conn, ['tx:1', 'tx:2'], '跨组')
    assert e.value.code == 'SAME_PARENT_REQUIRED'
    assert '餐饮' in e.value.message and '工资' in e.value.message


def test_group_member_must_be_sibling(conn):
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')
    with pytest.raises(LedgerError) as e:
        add_group_member(conn, g, 'tx:2')     # t2 在根，组在餐饮 → 不同父
    assert e.value.code == 'SAME_PARENT_REQUIRED'


# ── N8：按标记成组用 +2 读出 ────────────────────────────────────────────────

def test_group_reads_as_plus2(conn):
    food = create_node(conn, '餐饮', ['tx:1'])
    move(conn, ['tx:6'], food)
    create_net_group(conn, ['tx:1', 'tx:6'], '对冲')
    root = read_ledger(conn)
    node = children_of(root, '餐饮')
    g = group_of(node, '对冲')
    assert g.value == (0, 2000)             # 支出 30 - 收入 50 → 净支出 20
    # 组里的交易不再作为节点的直接子项
    assert not any(isinstance(c, Tx) and c.id in (1, 6) for c in node.children)
    # 节点整体 = +1
    assert node.value == (0, 2000)


def test_same_direction_members_are_not_cancelled(conn):
    """ν 只在组里同时有收入和支出时才抵消；同向的两笔原样相加。"""
    create_node(conn, '餐饮', ['tx:3', 'tx:4'])
    create_net_group(conn, ['tx:3', 'tx:4'], '同向')
    root = read_ledger(conn)
    node = children_of(root, '餐饮')
    assert group_of(node, '同向').value == (4500, 0)


def test_opposing_members_cancel_out(conn):
    """ν(支出50, 收入50) = (0, 0) —— 完全抵消。"""
    shopping = create_node(conn, '购物', ['tx:8'])
    move(conn, ['tx:6'], shopping)
    create_net_group(conn, ['tx:8', 'tx:6'], '对冲')
    root = read_ledger(conn)
    node = children_of(root, '购物')
    assert group_of(node, '对冲').value == (0, 0)
    assert node.value == (0, 0)


# ── N9：组内成员可以不在范围内 ──────────────────────────────────────────────

def test_group_net_uses_intersection_with_scope(conn):
    food = create_node(conn, '餐饮', ['tx:1', 'tx:6'])
    create_net_group(conn, ['tx:1', 'tx:6'], '对冲')
    # 一月范围：t1(1月) 在，t6(1月) 也在；改用只有 t1 的范围
    scope = Scope(frozenset({1}))
    root = read_ledger(conn)
    proj = read_scope(root, scope)
    node = children_of(proj, '餐饮')
    g = group_of(node, '对冲')
    assert [m.id for m in g.members] == [1]
    assert g.value == (3000, 0)             # 只有 t1 → ν 不扣


# ── N11 / N14：范围内没有交易的分类整个不出现 ───────────────────────────────

def test_empty_category_disappears_in_scope(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_node(conn, '交通', ['tx:5'])
    scope = scope_by_date(conn, '2026-01-01', '2026-01-31')
    root = read_ledger(conn)
    proj = read_scope(root, scope)
    names = [c.name for c in proj.children if isinstance(c, Node)]
    assert '交通' not in names
    assert '餐饮' in names
    # 一月命中 tx1 tx2 tx3 tx6 tx8：支出 30+20+50=100，收入 8000+50=8050
    assert proj.value == (10000, 805000)


def test_root_always_survives_empty_scope(conn):
    create_node(conn, '餐饮', ['tx:1'])
    scope = Scope(frozenset())
    proj = read_scope(read_ledger(conn), scope)
    assert proj.children == []
    assert proj.value == (0, 0)


# ── N19：分裂 ───────────────────────────────────────────────────────────────

def test_split_mirrors_vector_and_inherits_path(conn):
    food = create_node(conn, '餐饮', ['tx:3'])
    split_tx(conn, 3)
    root = read_ledger(conn)
    node = children_of(root, '餐饮')
    mirror_tx = [c for c in node.children if isinstance(c, Tx) and c.role == 'mirror']
    assert len(mirror_tx) == 1
    assert mirror_tx[0].value() == (0, 2000)      # t3 是 (2000,0)，t′ 刚好反过来
    assert mirror_tx[0].id == 3


def test_split_cannot_happen_twice(conn):
    split_tx(conn, 3)
    with pytest.raises(LedgerError) as e:
        split_tx(conn, 3)
    assert e.value.code == 'ALREADY_SPLIT'


def test_split_does_not_inherit_group(conn):
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    split_tx(conn, 3)
    root = read_ledger(conn)
    node = children_of(root, '餐饮')
    g = group_of(node, '组')
    assert [m.id for m in g.members] == [1, 3]     # 组里仍然是原始的 t1/t3
    assert any(isinstance(c, Tx) and c.role == 'mirror' for c in node.children)


def test_unsplit_only_cuts_the_mirror(conn):
    food = create_node(conn, '餐饮', ['tx:3'])
    split_tx(conn, 3)
    unsplit_tx(conn, 3)
    root = read_ledger(conn)
    node = children_of(root, '餐饮')
    assert [c.id for c in node.children if isinstance(c, Tx)] == [3]
    assert conn.execute('SELECT COUNT(*) FROM ledger_split_tx').fetchone()[0] == 0


# ── Q2：组成员可以是 t′ ─────────────────────────────────────────────────────

def test_mirror_can_be_group_member(conn):
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    split_tx(conn, 3)                     # t′3 与 t3 同父（继承路径）
    create_net_group(conn, ['tx:1', 'txm:3'], '含副本的组')
    root = read_ledger(conn)
    node = children_of(root, '餐饮')
    g = group_of(node, '含副本的组')
    assert sorted((m.id, m.role) for m in g.members) == [(1, 'primary'), (3, 'mirror')]


# ── 组的位置是显式存下来的 ───────────────────────────────────────────────────

def test_group_position_is_stored_explicitly(conn):
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')
    row = conn.execute('SELECT node_id FROM ledger_net_group WHERE id=?', (g,)).fetchone()
    assert row['node_id'] == food


def test_group_position_drift_is_detected(conn):
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    # 人为把一个成员挪到别的节点 → 组的位置与成员不再一致
    conn.execute("UPDATE ledger_placement SET node_id='root' WHERE tx_id=1")
    conn.commit()
    with pytest.raises(LedgerError) as e:
        read_ledger(conn)
    assert e.value.code == 'GROUP_POSITION_DRIFT'


def test_group_below_two_members_is_rejected(conn):
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')
    remove_group_member(conn, g, 'tx:1')      # 只剩 1 个 → 自动解散
    assert conn.execute('SELECT COUNT(*) FROM ledger_net_group').fetchone()[0] == 0
    root = read_ledger(conn)
    node = children_of(root, '餐饮')
    assert sorted(c.id for c in node.children if isinstance(c, Tx)) == [1, 3]


# ── Q4：所有操作可逆 ─────────────────────────────────────────────────────────

def test_undo_restores_every_field(conn):
    before = snapshot(conn)
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    move(conn, ['tx:5'], food)
    split_tx(conn, 5)
    assert snapshot(conn) != before
    while True:
        try:
            undo(conn)
        except LedgerError:
            break
    assert snapshot(conn) == before


def test_redo_restores_every_field(conn):
    before = snapshot(conn)
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    move(conn, ['tx:5'], food)
    after = snapshot(conn)
    undo(conn)
    undo(conn)
    undo(conn)
    assert snapshot(conn) != after
    redo(conn)
    redo(conn)
    redo(conn)
    assert snapshot(conn) == after


def test_new_operation_discards_the_redo_stack(conn):
    """撤销之后再做别的事 → 旧操作不能再重做（否则会「复活」，状态和历史对不上）。"""
    create_node(conn, 'A', ['tx:1'])
    create_node(conn, 'B', ['tx:3'])
    undo(conn)                                  # 撤 B
    create_node(conn, 'C', ['tx:2'])            # 新操作
    with pytest.raises(LedgerError) as e:
        redo(conn)
    assert e.value.code == 'NOTHING_TO_REDO'
    root = read_ledger(conn)
    names = sorted(c.name for c in root.children if isinstance(c, Node))
    assert names == ['A', 'C']
    # B 那条日志被标成作废，而不是消失
    states = [h['state'] for h in history(conn)]
    assert 'discarded' in states


def test_redo_picks_the_most_recently_undone(conn):
    """撤销两步 → 重做一步，重做的是「最后撤掉的那一步」，不是最早那步。"""
    create_node(conn, 'A', ['tx:1'])
    create_node(conn, 'B', ['tx:3'])
    undo(conn)                                  # 撤 B
    undo(conn)                                  # 撤 A
    redo(conn)                                  # 重做 A
    root = read_ledger(conn)
    names = sorted(c.name for c in root.children if isinstance(c, Node))
    assert names == ['A']


def test_undo_of_create_node_puts_transactions_back(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    undo(conn)
    root = read_ledger(conn)
    assert len(root.children) == 7                     # 全部回到根
    assert root.value == (22500, 805000)


def test_undo_of_group_creation(conn):
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    undo(conn)
    root = read_ledger(conn)
    node = children_of(root, '餐饮')
    assert not any(isinstance(c, NetGroup) for c in node.children)
    assert sorted(c.id for c in node.children if isinstance(c, Tx)) == [1, 3]


def test_undo_of_dissolve_node(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    inner = create_node(conn, '餐饮·子类', ['tx:3'])
    dissolve_node(conn, inner)
    undo(conn)
    root = read_ledger(conn)
    node = children_of(root, '餐饮')
    assert any(isinstance(c, Node) and c.name == '餐饮·子类' for c in node.children)


def test_moving_a_group_member_moves_the_whole_group(conn):
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    other = create_node(conn, '其它', ['tx:5'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    move(conn, ['tx:1'], other)                  # 只点了一笔 → 整组跟着走
    root = read_ledger(conn)
    node = children_of(root, '其它')
    g = group_of(node, '组')
    assert sorted(m.id for m in g.members) == [1, 3]
    assert node.value == (15000, 0)      # t5 支出 100 + 组(t1,t3) 支出 50


# ── 读出结构（JSON）──────────────────────────────────────────────────────────

def test_to_json_shape(conn):
    food = create_node(conn, '餐饮', ['tx:1'])
    move(conn, ['tx:6'], food)
    create_net_group(conn, ['tx:1', 'tx:6'], '对冲')
    js = to_json(read_ledger(conn))
    assert js['type'] == 'node' and js['name'] == '总账'
    # t1(支出30) + t6(收入50) 进了组 → ν = (0, 20)，根值从 (22500,805000) 变成 (19500,802000)
    assert (js['e'], js['i']) == (19500, 802000)
    node = js['children'][0]
    assert node['type'] == 'node' and node['name'] == '餐饮'
    grp = node['children'][0]
    assert grp['type'] == 'group'
    assert (grp['e'], grp['i']) == (0, 2000)
    assert [(m['tx_id'], m['role']) for m in grp['members']] == [(1, 'primary'), (6, 'primary')]
    # 组里的交易不再作为节点的直接子项
    assert all(c['type'] != 'tx' for c in node['children'])


def test_to_json_scope_drops_empty_categories(conn):
    create_node(conn, '餐饮', ['tx:1'])
    create_node(conn, '交通', ['tx:5'])
    scope = scope_by_date(conn, '2026-01-01', '2026-01-31')
    js = to_json(read_scope(read_ledger(conn), scope))
    names = [c['name'] for c in js['children'] if c['type'] == 'node']
    assert '交通' not in names and '餐饮' in names


def test_to_json_mirror_member(conn):
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    split_tx(conn, 3)
    create_net_group(conn, ['tx:1', 'txm:3'], '含副本的组')
    js = to_json(read_ledger(conn))
    grp = js['children'][0]['children'][0]
    assert grp['type'] == 'group'
    assert [(m['tx_id'], m['role']) for m in grp['members']] == [(1, 'primary'), (3, 'mirror')]


# ── 其它 ────────────────────────────────────────────────────────────────────

def test_dissolve_node_moves_children_up(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    inner = create_node(conn, '子类', ['tx:3'])
    dissolve_node(conn, inner)
    root = read_ledger(conn)
    node = children_of(root, '餐饮')
    assert sorted(c.id for c in node.children if isinstance(c, Tx)) == [1, 3]


# ── 边界 1：+2 组一部分在 scope 内、一部分在 scope 外 ───────────────────────

def test_group_partially_in_scope_is_read_as_plus2(conn):
    """组里 3 笔，scope 只含 2 笔 → 只按这 2 笔做 +2，范围外的那笔**不能重复计数**。"""
    create_node(conn, '餐饮', ['tx:1', 'tx:4', 'tx:6'])          # t1支出30 / t4支出25 / t6收入50
    create_net_group(conn, ['tx:1', 'tx:4', 'tx:6'], '对冲组')
    scope = scope_by_date(conn, '2026-01-01', '2026-01-31')      # 含 t1、t6；t4 是二月
    proj = read_scope(read_ledger(conn), scope)
    grp = group_of(children_of(proj, '餐饮'), '对冲组')
    # 只剩范围内的成员
    assert [m.id for m in grp.members] == [1, 6]
    # ν(支出30, 收入50) = 净收入 20；t4（支出25）在范围外，不参与
    assert grp.value == (0, 2000)
    # 关键：t4 不能又作为节点的直接子项冒出来（否则就是重复计数）
    node = children_of(proj, '餐饮')
    assert node.value == (0, 2000)
    assert not any(isinstance(c, Tx) and c.id == 4 for c in node.children)


def test_group_with_one_visible_member_still_reads(conn):
    """scope 里只剩 1 个成员 → 组还在（范围内有东西），但 ν 不抵消，原样相加。"""
    create_node(conn, '餐饮', ['tx:1', 'tx:4'])
    create_net_group(conn, ['tx:1', 'tx:4'], '对冲组')
    scope = scope_by_date(conn, '2026-02-01', '2026-02-28')      # 只有 t4（支出25）
    proj = read_scope(read_ledger(conn), scope)
    grp = group_of(children_of(proj, '餐饮'), '对冲组')
    assert [m.id for m in grp.members] == [4]
    assert grp.value == (2500, 0)


def test_group_with_no_visible_member_disappears(conn):
    """组里一个成员都不在 scope 内 → 组整个不出现（节点还在，因为节点下有别的交易）。"""
    create_node(conn, '餐饮', ['tx:1', 'tx:3', 'tx:4'])          # t1/t3 一月，t4 二月
    create_net_group(conn, ['tx:1', 'tx:3'], '对冲组')           # 组成员全在一月
    scope = scope_by_date(conn, '2026-02-01', '2026-02-28')      # 范围内只有 t4
    proj = read_scope(read_ledger(conn), scope)
    node = children_of(proj, '餐饮')                             # 节点在（t4 在范围内）
    assert not any(isinstance(c, NetGroup) for c in node.children)
    assert node.value == (2500, 0)      # 节点只剩范围内的 t4


def test_group_with_nothing_in_scope_disappears_entirely(conn):
    """scope 里什么都没有 → 组和它所在的节点一起整个不出现。"""
    create_node(conn, '餐饮', ['tx:1', 'tx:4'])
    create_net_group(conn, ['tx:1', 'tx:4'], '对冲组')
    scope = scope_by_date(conn, '2026-03-01', '2026-03-31')
    proj = read_scope(read_ledger(conn), scope)
    assert not any(isinstance(c, Node) and c.name == '餐饮' for c in proj.children)
    assert not any(isinstance(c, NetGroup) for c in proj.children)
    assert proj.value == (0, 0)


# ── 边界 2：建 M 时拖进来的是「节点 + 交易」的混合 ───────────────────────────

def test_create_node_folds_selected_node_and_its_transactions(conn):
    """用户的例子：t1 和 M_12 一起拖进来 → t2/t3 被 M_12 覆盖、自动排除。

    结构：M_1 下有 t1；M_12 在 M_1 下，M_12 下有 t2/t3。
    """
    m1 = create_node(conn, 'M_1', ['tx:1'])
    m12 = create_node(conn, 'M_12', ['tx:3', 'tx:4'])
    move(conn, [m12], m1)                       # M_12 挂到 M_1 下
    mx = create_node(conn, 'M_x', ['tx:1', m12])
    root = read_ledger(conn)
    # 新节点挂到 LCA = M_1 下（不是总账）
    assert children_of(children_of(root, 'M_1'), 'M_x').id == mx
    # M_12 和 t1 进了新节点；t2/t3 仍留在 M_12 下，没被搬走
    mx_node = children_of(children_of(root, 'M_1'), 'M_x')
    assert sorted(c.id for c in mx_node.children if isinstance(c, Node)) == [m12]
    assert any(isinstance(c, Tx) and c.id == 1 for c in mx_node.children)
    inner = children_of(mx_node, 'M_12')
    assert sorted(c.id for c in inner.children if isinstance(c, Tx)) == [3, 4]


def test_create_node_from_single_tx_takes_its_parent(conn):
    """只拖一笔 t → 新 M 成为 t 的新父亲，且新 M 落在 t **原来所在的位置**（LCA）。"""
    outer = create_node(conn, '外层', ['tx:1'])
    inner = create_node(conn, '内层', ['tx:1'])   # 内层挂到外层下
    new = create_node(conn, '新层', ['tx:1'])      # t1 现在在内层下 → LCA = 内层
    root = read_ledger(conn)
    inner_node = children_of(children_of(root, '外层'), '内层')
    new_node = children_of(inner_node, '新层')    # 新层挂在内层下（t 原来所在的位置）
    assert new_node.id == new
    # t1 成了新层的孩子 —— 新 M 确实是 t 的父亲
    assert any(isinstance(c, Tx) and c.id == 1 for c in new_node.children)


def test_create_node_across_branches_falls_back_to_root(conn):
    """拖进来的项没有更近的公共祖先 → LCA 升到总账（不是报错）。"""
    create_node(conn, '甲', ['tx:1'])
    create_node(conn, '乙', ['tx:3'])
    new = create_node(conn, '合并', ['tx:1', 'tx:3'])
    root = read_ledger(conn)
    merged = children_of(root, '合并')
    assert merged.id == new
    assert merged.value == (5000, 0)


def test_create_node_carries_whole_group(conn):
    """拖进来的 t 属于某个 +2 组 → 整组跟着走（R2：组成员不可分割）。"""
    create_node(conn, '餐饮', ['tx:1', 'tx:4'])
    gid = create_net_group(conn, ['tx:1', 'tx:4'], '对冲组')
    create_node(conn, '新组', ['tx:1'])          # 只拖了组里的 t1
    root = read_ledger(conn)
    # 新组挂在 LCA（餐饮）下，整组跟着 t1 一起进去
    grp = group_of(children_of(children_of(root, '餐饮'), '新组'), '对冲组')
    assert grp.id == gid
    assert [m.id for m in grp.members] == [1, 4]


# ── 边界 3：移动的四种组合 + 搬空之后 ───────────────────────────────────────

def test_move_single_tx(conn):
    """移动 1 个 t → 挂到目标节点下，别的不动。"""
    create_node(conn, '甲', ['tx:1'])
    target = create_node(conn, '乙', ['tx:3'])
    move(conn, ['tx:1'], target)
    node = children_of(read_ledger(conn), '乙')
    assert any(isinstance(c, Tx) and c.id == 1 for c in node.children)


def test_move_multiple_txs_from_different_parents(conn):
    """移动集合唯一：来自不同父节点的多个 t 一起搬，但不合并成新的 M。"""
    create_node(conn, '甲', ['tx:1'])
    create_node(conn, '乙', ['tx:3'])
    target = create_node(conn, '目标', ['tx:5'])
    move(conn, ['tx:1', 'tx:3'], target)
    node = children_of(read_ledger(conn), '目标')
    assert sorted(c.id for c in node.children if isinstance(c, Tx)) == [1, 3, 5]   # 5 本来就在
    assert not any(isinstance(c, Node) for c in node.children)   # 没有凭空多出 M


def test_move_node_carries_its_subtree(conn):
    """移动一个 M → 它整棵子树跟着走，结构不变。"""
    outer = create_node(conn, '外层', ['tx:1'])
    inner = create_node(conn, '内层', ['tx:3'])
    move(conn, [inner], outer)                    # 内层先挂到外层下
    target = create_node(conn, '目标', ['tx:4'])
    move(conn, [inner], target)
    node = children_of(read_ledger(conn), '目标')
    assert children_of(node, '内层').id == inner


def test_move_multiple_nodes(conn):
    """移动多个 M → 集合唯一：各换父亲，彼此不合并。"""
    a = create_node(conn, '甲', ['tx:1'])
    b = create_node(conn, '乙', ['tx:3'])
    target = create_node(conn, '目标', ['tx:4'])
    move(conn, [a, b], target)
    node = children_of(read_ledger(conn), '目标')
    assert sorted(c.id for c in node.children if isinstance(c, Node)) == sorted([a, b])


def test_move_node_and_tx_together(conn):
    """M-t 组合：节点和交易一起搬，集合唯一、结构保留。"""
    a = create_node(conn, '甲', ['tx:1'])
    target = create_node(conn, '目标', ['tx:4'])
    move(conn, [a, 'tx:3'], target)
    node = children_of(read_ledger(conn), '目标')
    assert any(isinstance(c, Node) and c.id == a for c in node.children)
    assert any(isinstance(c, Tx) and c.id == 3 for c in node.children)


def test_emptied_node_stays_in_full_tree_but_leaves_scope(conn):
    """搬空之后：全量树里**还有**这个空节点；scope 投影里它**整个不出现**。"""
    create_node(conn, '甲', ['tx:1'])
    empty_me = create_node(conn, '待搬空', ['tx:3'])
    target = create_node(conn, '目标', ['tx:4'])
    move(conn, ['tx:3'], target)

    root = read_ledger(conn)
    # 全量：节点还在，只是空了
    assert any(isinstance(c, Node) and c.id == empty_me for c in root.children)
    assert children_of(root, '待搬空').value == (0, 0)

    # scope：待搬空下面没有任何交易 → 整个不出现（N11）
    scope = scope_by_date(conn, '2026-01-01', '2026-01-31')
    proj = read_scope(root, scope)
    assert not any(isinstance(c, Node) and c.id == empty_me for c in proj.children)


# ── 边界 4：解散一个 M，+2 条目和里面的 t 的父亲一起变 ───────────────────────

def test_dissolve_moves_group_and_its_members_to_parent(conn):
    """M_1 下有 [t1] [t2,t3]+2 —— 解散 M_1 后，组和组里的 t 都上移到 M_1 的父节点。"""
    m1 = create_node(conn, 'M_1', ['tx:1', 'tx:3', 'tx:4'])
    gid = create_net_group(conn, ['tx:3', 'tx:4'], '组A')
    root_id = conn.execute('SELECT id FROM ledger_node WHERE parent_id IS NULL').fetchone()['id']

    dissolve_node(conn, m1)

    # 组的父节点变了
    row = conn.execute('SELECT node_id FROM ledger_net_group WHERE id=?', (gid,)).fetchone()
    assert row['node_id'] == root_id
    # 组里 t 的父节点也变了
    for tx_id in (3, 4):
        row = conn.execute('SELECT node_id, net_group_id FROM ledger_placement WHERE tx_id=?',
                           (tx_id,)).fetchone()
        assert row['node_id'] == root_id
        assert row['net_group_id'] == gid
    # 读出来：组还在，成员关系没变
    root = read_ledger(conn)
    grp = group_of(root, '组A')
    assert [m.id for m in grp.members] == [3, 4]


def test_dissolve_node_with_child_group_keeps_group(conn):
    """解散 M_1，而 M_1 的子 M 里有组 → 子 M 上移，组留在子 M 下（位置不变）。"""
    m1 = create_node(conn, 'M_1', ['tx:1'])
    m2 = create_node(conn, 'M_2', ['tx:3', 'tx:4'])
    move(conn, [m2], m1)
    gid = create_net_group(conn, ['tx:3', 'tx:4'], '子组')
    dissolve_node(conn, m1)
    row = conn.execute('SELECT node_id FROM ledger_net_group WHERE id=?', (gid,)).fetchone()
    assert row['node_id'] == m2
    root = read_ledger(conn)
    assert group_of(children_of(root, 'M_2'), '子组').id == gid

