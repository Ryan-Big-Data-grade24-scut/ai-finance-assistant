"""账本代数 V2 · 操作层契约测试（backend.ledger2，真 schema 的库）。

前半 = 原型 prototypes/ledger2/test_ledger2.py 的 51 条移植（N 编号意图不变）；
后半 = V2 新增语义：初始化后的分类节点（普通节点）、改名/解散组、不变量、已删除交易。

和原型的结构差异（不是放宽）：
* 夹具先按平台分类初始化（presets.init_by_platform_category）：每笔交易都有显式归属行，
  分类节点 `cat:<名>` 是**真节点**（kind='category' 只是个标签），可改名、可解散、可被搬动；
* 新 M 的父亲 = 被选项所在节点的 LCA —— 同分类通常是那个分类节点，跨分类才是根；
* 金额一律整数分。
"""
from __future__ import annotations

import pytest

from backend.ledger2 import ops, reader
from backend.ledger2.algebra import atom, net, plus1
from backend.ledger2.errors import LedgerError
from backend.ledger2.model import NetGroup, Node, Scope, Tx
from backend.ledger2.ops import (
    add_group_member, create_net_group, create_node, dissolve_group, dissolve_node, history, move,
    remove_group_member, rename_group, rename_node, split_tx, undo, redo, unsplit_tx,
)
from backend.ledger2.reader import read_ledger, read_scope, to_json
from backend.ledger2 import facts
from backend.ledger2 import invariants
from ledger2_support import (
    ROOT_VALUE, cat, child, group, make_db, snapshot, table_count, tx_ids,
)


@pytest.fixture()
def conn(tmp_path):
    c = make_db(tmp_path / 'ledger2.db')
    yield c
    c.close()


def scope_by_date(c, a, b):
    return facts.scope_by_date(c, a, b)


def find_node(node: Node, nid: str):
    if node.id == nid:
        return node
    for c in node.children:
        if isinstance(c, Node):
            hit = find_node(c, nid)
            if hit is not None:
                return hit
    return None


def m_names(node: Node) -> list[str]:
    """树里所有用户建的 M 的名字（不含根和分类节点）。"""
    out = []
    for c in node.children:
        if isinstance(c, Node):
            if c.kind == 'node':
                out.append(c.name)
            out += m_names(c)
    return sorted(out)


def undo_all(c):
    while True:
        try:
            undo(c)
        except LedgerError as e:
            assert e.code == 'NOTHING_TO_UNDO'
            return


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
    """初始化后的基础账本：每笔交易一行归属、每个平台分类一个节点；没有组、没有分裂、没有操作日志。"""
    root = read_ledger(conn)
    assert table_count(conn, 'ledger2_tx') == 7
    assert table_count(conn, 'ledger2_net_group') == 0
    assert table_count(conn, 'ledger2_node') == 6     # 根 + 5 个分类节点
    assert table_count(conn, 'ledger2_op_log') == 0
    assert root.value == ROOT_VALUE                   # e=支出 225.00，i=收入 8050.00
    assert sorted(c.name for c in root.children) == ['交通', '其他收入', '工资', '购物', '餐饮']
    assert all(isinstance(c, Node) and c.kind == 'category' for c in root.children)
    assert sum(len(tx_ids(c)) for c in root.children) == 7


# ── N16：建 M 时自动排除被覆盖的子项 ────────────────────────────────────────

def test_create_node_folds_covered_items(conn):
    inner = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_node(conn, '餐饮（合并）', ['tx:1', 'tx:3', inner])
    root = read_ledger(conn)
    merged = child(cat(root, '餐饮'), '餐饮（合并）')
    # 被覆盖的 t1/t3 不再作为直接子项
    assert not any(isinstance(c, Tx) for c in merged.children)
    # inner 节点整体搬进了新节点，结构保留
    assert child(merged, '餐饮').value == (5000, 0)


def test_create_node_uses_lca_of_parents(conn):
    create_node(conn, '餐饮', ['tx:1'])
    create_node(conn, '餐饮·子类', ['tx:3'])
    # t1、t3 分别在两个 M 下，两个 M 同在分类节点「餐饮」下 → LCA 是 cat:餐饮
    new = create_node(conn, '合并', ['tx:1', 'tx:3'])
    root = read_ledger(conn)
    merged = child(cat(root, '餐饮'), '合并')
    assert merged.id == new
    assert merged.value == (5000, 0)


# ── N17：移动的集合唯一、内部不合并 ──────────────────────────────────────────

def test_move_keeps_structure(conn):
    inner = create_node(conn, '餐饮', ['tx:1'])
    create_node(conn, '餐饮组', [inner])
    target = child(cat(read_ledger(conn), '餐饮'), '餐饮组')
    move(conn, ['tx:3'], target.id)
    target = child(cat(read_ledger(conn), '餐饮'), '餐饮组')
    # tx3 直接挂到目标节点下，不与 inner 合并
    assert any(isinstance(c, Tx) and c.id == 3 for c in target.children)
    assert child(target, '餐饮').value == (3000, 0)


def test_move_into_own_subtree_is_rejected(conn):
    outer = create_node(conn, '外层', ['tx:1'])
    inner = create_node(conn, '内层', ['tx:1'])      # 从外层里的交易建 → 内层成为外层的子节点
    with pytest.raises(LedgerError) as e:
        move(conn, [outer], inner)
    assert e.value.code == 'CYCLE'


# ── N18 / Q1：+2 必须同父，不能放宽 ─────────────────────────────────────────

def test_group_across_parents_is_rejected(conn):
    create_node(conn, '餐饮', ['tx:1'])
    create_node(conn, '工资', ['tx:2'])
    with pytest.raises(LedgerError) as e:
        create_net_group(conn, ['tx:1', 'tx:2'], '跨组')
    assert e.value.code == 'SAME_PARENT_REQUIRED'
    assert '餐饮' in e.value.message and '工资' in e.value.message


def test_group_across_two_untouched_categories_is_rejected(conn):
    """还没动过的交易也有父亲（它的分类节点）：跨分类同样不是同父。"""
    with pytest.raises(LedgerError) as e:
        create_net_group(conn, ['tx:1', 'tx:2'], '跨类')
    assert e.value.code == 'SAME_PARENT_REQUIRED'
    assert '餐饮' in e.value.message and '工资' in e.value.message
    assert table_count(conn, 'ledger2_net_group') == 0 and table_count(conn, 'ledger2_op_log') == 0


def test_group_member_must_be_sibling(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')
    with pytest.raises(LedgerError) as e:
        add_group_member(conn, g, 'tx:2')     # t2 在分类节点「工资」，组在 M「餐饮」→ 不同父
    assert e.value.code == 'SAME_PARENT_REQUIRED'


# ── N8：按标记成组用 +2 读出 ────────────────────────────────────────────────

def test_group_reads_as_plus2(conn):
    food = create_node(conn, '餐饮', ['tx:1'])
    move(conn, ['tx:6'], food)
    create_net_group(conn, ['tx:1', 'tx:6'], '对冲')
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    g = group(node, '对冲')
    assert g.value == (0, 2000)             # 支出 30 - 收入 50 → 净收入 20
    # 组里的交易不再作为节点的直接子项
    assert not any(isinstance(c, Tx) and c.id in (1, 6) for c in node.children)
    # 节点整体 = +1
    assert node.value == (0, 2000)


def test_same_direction_members_are_not_cancelled(conn):
    """ν 只在组里同时有收入和支出时才抵消；同向的两笔原样相加。"""
    create_node(conn, '餐饮', ['tx:3', 'tx:4'])
    create_net_group(conn, ['tx:3', 'tx:4'], '同向')
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    assert group(node, '同向').value == (4500, 0)


def test_opposing_members_cancel_out(conn):
    """ν(支出50, 收入50) = (0, 0) —— 完全抵消。"""
    shopping = create_node(conn, '购物', ['tx:8'])
    move(conn, ['tx:6'], shopping)
    create_net_group(conn, ['tx:8', 'tx:6'], '对冲')
    node = child(cat(read_ledger(conn), '购物'), '购物')
    assert group(node, '对冲').value == (0, 0)
    assert node.value == (0, 0)


# ── N9：组内成员可以不在范围内 ──────────────────────────────────────────────

def test_group_net_uses_intersection_with_scope(conn):
    # t1（餐饮）和 t6（其他收入）不同分类 → 新 M 的父亲是根
    create_node(conn, '餐饮', ['tx:1', 'tx:6'])
    create_net_group(conn, ['tx:1', 'tx:6'], '对冲')
    proj = read_scope(read_ledger(conn), Scope(frozenset({1})))
    g = group(child(proj, '餐饮'), '对冲')
    assert [m.id for m in g.members] == [1]
    assert g.value == (3000, 0)             # 只有 t1 → ν 不扣


# ── N11 / N14：范围内没有交易的分类整个不出现 ───────────────────────────────

def test_empty_category_disappears_in_scope(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_node(conn, '交通', ['tx:5'])
    proj = read_scope(read_ledger(conn), scope_by_date(conn, '2026-01-01', '2026-01-31'))
    ids = [c.id for c in proj.children if isinstance(c, Node)]
    assert 'cat:交通' not in ids                    # 交通整条线（分类节点 + M）都消失
    assert find_node(proj, 'cat:交通') is None
    assert 'cat:餐饮' in ids
    # 一月命中 tx1 tx2 tx3 tx6 tx8：支出 30+20+50=100，收入 8000+50=8050
    assert proj.value == (10000, 805000)


def test_root_always_survives_empty_scope(conn):
    create_node(conn, '餐饮', ['tx:1'])
    proj = read_scope(read_ledger(conn), Scope(frozenset()))
    assert proj.children == []
    assert proj.value == (0, 0)


# ── N19：分裂 ───────────────────────────────────────────────────────────────

def test_split_mirrors_vector_and_inherits_path(conn):
    create_node(conn, '餐饮', ['tx:3'])
    split_tx(conn, 3)
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
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
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    split_tx(conn, 3)
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    g = group(node, '组')
    assert [m.id for m in g.members] == [1, 3]     # 组里仍然是原始的 t1/t3
    assert all(m.role == 'primary' for m in g.members)
    assert any(isinstance(c, Tx) and c.role == 'mirror' for c in node.children)


def test_unsplit_only_cuts_the_mirror(conn):
    create_node(conn, '餐饮', ['tx:3'])
    split_tx(conn, 3)
    unsplit_tx(conn, 3)
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    assert [c.id for c in node.children if isinstance(c, Tx)] == [3]
    assert table_count(conn, 'ledger2_split_tx') == 0


# ── Q2：组成员可以是 t′ ─────────────────────────────────────────────────────

def test_mirror_can_be_group_member(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    split_tx(conn, 3)                     # t′3 与 t3 同父（继承路径）
    create_net_group(conn, ['tx:1', 'txm:3'], '含副本的组')
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    g = group(node, '含副本的组')
    assert sorted((m.id, m.role) for m in g.members) == [(1, 'primary'), (3, 'mirror')]


# ── 组的位置是显式存下来的 ───────────────────────────────────────────────────

def test_group_position_is_stored_explicitly(conn):
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')
    row = conn.execute('SELECT node_id FROM ledger2_net_group WHERE id=?', (g,)).fetchone()
    assert row['node_id'] == food


def test_group_position_drift_is_detected(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    # 人为把一个成员挪到别的节点 → 组的位置与成员不再一致
    conn.execute("UPDATE ledger2_tx SET node_id='root' WHERE tx_id=1 AND role='primary'")
    conn.commit()
    with pytest.raises(LedgerError) as e:
        read_ledger(conn)
    assert e.value.code == 'GROUP_POSITION_DRIFT'


def test_group_below_two_members_is_rejected(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')
    remove_group_member(conn, g, 'tx:1')      # 只剩 1 个 → 自动解散
    assert table_count(conn, 'ledger2_net_group') == 0
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    assert tx_ids(node) == [1, 3]


# ── Q4：所有操作可逆 ─────────────────────────────────────────────────────────

def test_undo_restores_every_field(conn):
    before = snapshot(conn)
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    move(conn, ['tx:5'], food)
    split_tx(conn, 5)
    assert snapshot(conn) != before
    undo_all(conn)
    assert snapshot(conn) == before


def test_redo_restores_every_field(conn):
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
    assert m_names(read_ledger(conn)) == ['A', 'C']
    # B 那条日志被标成作废，而不是消失
    assert 'discarded' in [h['state'] for h in history(conn)]


def test_redo_picks_the_most_recently_undone(conn):
    """撤销两步 → 重做一步，重做的是「最后撤掉的那一步」，不是最早那步。"""
    create_node(conn, 'A', ['tx:1'])
    create_node(conn, 'B', ['tx:3'])
    undo(conn)                                  # 撤 B
    undo(conn)                                  # 撤 A
    redo(conn)                                  # 重做 A
    assert m_names(read_ledger(conn)) == ['A']


def test_undo_of_create_node_puts_transactions_back(conn):
    before = snapshot(conn)
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    undo(conn)
    root = read_ledger(conn)
    assert len(root.children) == 5                     # 5 个分类节点，全部回到虚拟
    assert root.value == ROOT_VALUE
    assert snapshot(conn) == before


def test_undo_of_group_creation(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    undo(conn)
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    assert not any(isinstance(c, NetGroup) for c in node.children)
    assert tx_ids(node) == [1, 3]


def test_undo_of_dissolve_node(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    inner = create_node(conn, '餐饮·子类', ['tx:3'])
    before = snapshot(conn)
    dissolve_node(conn, inner)
    undo(conn)
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    assert any(isinstance(c, Node) and c.name == '餐饮·子类' for c in node.children)
    assert snapshot(conn) == before


def test_moving_a_group_member_moves_the_whole_group(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    other = create_node(conn, '其它', ['tx:5'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    move(conn, ['tx:1'], other)                  # 只点了一笔 → 整组跟着走
    node = child(cat(read_ledger(conn), '交通'), '其它')
    g = group(node, '组')
    assert sorted(m.id for m in g.members) == [1, 3]
    assert node.value == (15000, 0)      # t5 支出 100 + 组(t1,t3) 支出 50


# ── 读出结构（JSON）──────────────────────────────────────────────────────────

def test_to_json_shape(conn):
    food = create_node(conn, '餐饮', ['tx:1'])
    move(conn, ['tx:6'], food)
    create_net_group(conn, ['tx:1', 'tx:6'], '对冲')
    js = to_json(read_ledger(conn))
    assert js['type'] == 'node' and js['name'] == '总账' and js['kind'] == 'root'
    # t1(支出30) + t6(收入50) 进了组 → ν = (0, 20)，根值从 (22500,805000) 变成 (19500,802000)
    assert (js['e'], js['i']) == (19500, 802000)
    cat_js = next(c for c in js['children'] if c['id'] == 'cat:餐饮')
    assert cat_js['type'] == 'node' and cat_js['kind'] == 'category' and cat_js['name'] == '餐饮'
    node = next(c for c in cat_js['children'] if c['type'] == 'node')
    assert node['name'] == '餐饮' and node['kind'] == 'node'
    grp = node['children'][0]
    assert grp['type'] == 'group'
    assert (grp['e'], grp['i']) == (0, 2000)
    assert [(m['tx_id'], m['role']) for m in grp['members']] == [(1, 'primary'), (6, 'primary')]
    assert grp['members'][0]['ref'] == 'tx:1' and grp['members'][0]['kind'] == 'expense'
    assert grp['members'][1]['kind'] == 'income'
    # 组里的交易不再作为节点的直接子项
    assert all(c['type'] != 'tx' for c in node['children'])


def test_to_json_scope_drops_empty_categories(conn):
    create_node(conn, '餐饮', ['tx:1'])
    create_node(conn, '交通', ['tx:5'])
    js = to_json(read_scope(read_ledger(conn), scope_by_date(conn, '2026-01-01', '2026-01-31')))
    ids = [c['id'] for c in js['children']]
    assert 'cat:交通' not in ids and 'cat:餐饮' in ids


def test_to_json_mirror_member(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    split_tx(conn, 3)
    create_net_group(conn, ['tx:1', 'txm:3'], '含副本的组')
    js = to_json(read_ledger(conn))
    cat_js = next(c for c in js['children'] if c['id'] == 'cat:餐饮')
    node = next(c for c in cat_js['children'] if c['type'] == 'node')
    grp = next(c for c in node['children'] if c['type'] == 'group')
    assert [(m['tx_id'], m['role']) for m in grp['members']] == [(1, 'primary'), (3, 'mirror')]
    assert grp['members'][1]['ref'] == 'txm:3'
    assert grp['members'][1]['kind'] == 'income'          # t3 是支出，t′ 反过来是收入


# ── 其它 ────────────────────────────────────────────────────────────────────

def test_dissolve_node_moves_children_up(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    inner = create_node(conn, '子类', ['tx:3'])
    dissolve_node(conn, inner)
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    assert tx_ids(node) == [1, 3]


# ── 边界 1：+2 组一部分在 scope 内、一部分在 scope 外 ───────────────────────

def test_group_partially_in_scope_is_read_as_plus2(conn):
    """组里 3 笔，scope 只含 2 笔 → 只按这 2 笔做 +2，范围外的那笔**不能重复计数**。"""
    create_node(conn, '餐饮', ['tx:1', 'tx:4', 'tx:6'])          # t6 在「其他收入」→ M 挂根
    create_net_group(conn, ['tx:1', 'tx:4', 'tx:6'], '对冲组')
    proj = read_scope(read_ledger(conn), scope_by_date(conn, '2026-01-01', '2026-01-31'))
    node = child(proj, '餐饮')
    grp = group(node, '对冲组')
    assert [m.id for m in grp.members] == [1, 6]
    # ν(支出30, 收入50) = 净收入 20；t4（支出25）在范围外，不参与
    assert grp.value == (0, 2000)
    # 关键：t4 不能又作为节点的直接子项冒出来（否则就是重复计数）
    assert node.value == (0, 2000)
    assert not any(isinstance(c, Tx) and c.id == 4 for c in node.children)


def test_group_with_one_visible_member_still_reads(conn):
    """scope 里只剩 1 个成员 → 组还在（范围内有东西），但 ν 不抵消，原样相加。"""
    create_node(conn, '餐饮', ['tx:1', 'tx:4'])
    create_net_group(conn, ['tx:1', 'tx:4'], '对冲组')
    proj = read_scope(read_ledger(conn), scope_by_date(conn, '2026-02-01', '2026-02-28'))
    grp = group(child(cat(proj, '餐饮'), '餐饮'), '对冲组')
    assert [m.id for m in grp.members] == [4]
    assert grp.value == (2500, 0)


def test_group_with_no_visible_member_disappears(conn):
    """组里一个成员都不在 scope 内 → 组整个不出现（节点还在，因为节点下有别的交易）。"""
    create_node(conn, '餐饮', ['tx:1', 'tx:3', 'tx:4'])          # t1/t3 一月，t4 二月
    create_net_group(conn, ['tx:1', 'tx:3'], '对冲组')
    proj = read_scope(read_ledger(conn), scope_by_date(conn, '2026-02-01', '2026-02-28'))
    node = child(cat(proj, '餐饮'), '餐饮')                      # 节点在（t4 在范围内）
    assert not any(isinstance(c, NetGroup) for c in node.children)
    assert node.value == (2500, 0)


def test_group_with_nothing_in_scope_disappears_entirely(conn):
    """scope 里什么都没有 → 组和它所在的节点一起整个不出现。"""
    create_node(conn, '餐饮', ['tx:1', 'tx:4'])
    create_net_group(conn, ['tx:1', 'tx:4'], '对冲组')
    proj = read_scope(read_ledger(conn), scope_by_date(conn, '2026-03-01', '2026-03-31'))
    assert find_node(proj, 'cat:餐饮') is None
    assert proj.children == []
    assert proj.value == (0, 0)


# ── 边界 2：建 M 时拖进来的是「节点 + 交易」的混合 ───────────────────────────

def test_create_node_folds_selected_node_and_its_transactions(conn):
    """t1 和 M_12 一起拖进来 → M_12 下的 t3/t4 被 M_12 覆盖、自动排除。

    结构：M_1 下有 t1；M_12 在 M_1 下，M_12 下有 t3/t4。
    """
    m1 = create_node(conn, 'M_1', ['tx:1'])
    m12 = create_node(conn, 'M_12', ['tx:3', 'tx:4'])
    move(conn, [m12], m1)                       # M_12 挂到 M_1 下
    mx = create_node(conn, 'M_x', ['tx:1', m12])
    root = read_ledger(conn)
    # 新节点挂到 LCA = M_1 下（不是总账，也不是分类节点）
    m1_node = child(cat(root, '餐饮'), 'M_1')
    mx_node = child(m1_node, 'M_x')
    assert mx_node.id == mx
    # M_12 和 t1 进了新节点；t3/t4 仍留在 M_12 下，没被搬走
    assert sorted(c.id for c in mx_node.children if isinstance(c, Node)) == [m12]
    assert any(isinstance(c, Tx) and c.id == 1 for c in mx_node.children)
    assert tx_ids(child(mx_node, 'M_12')) == [3, 4]


def test_create_node_from_single_tx_takes_its_parent(conn):
    """只拖一笔 t → 新 M 成为 t 的新父亲，且新 M 落在 t **原来所在的位置**（LCA）。"""
    create_node(conn, '外层', ['tx:1'])
    create_node(conn, '内层', ['tx:1'])          # 内层挂到外层下
    new = create_node(conn, '新层', ['tx:1'])    # t1 现在在内层下 → LCA = 内层
    inner_node = child(child(cat(read_ledger(conn), '餐饮'), '外层'), '内层')
    new_node = child(inner_node, '新层')         # 新层挂在内层下（t 原来所在的位置）
    assert new_node.id == new
    assert any(isinstance(c, Tx) and c.id == 1 for c in new_node.children)


def test_create_node_in_one_category_lands_under_the_category_node(conn):
    """同分类的两个 M 之间没有更近的公共祖先 → LCA 是它们的分类节点。"""
    create_node(conn, '甲', ['tx:1'])
    create_node(conn, '乙', ['tx:3'])
    new = create_node(conn, '合并', ['tx:1', 'tx:3'])
    merged = child(cat(read_ledger(conn), '餐饮'), '合并')
    assert merged.id == new
    assert merged.value == (5000, 0)


def test_create_node_across_categories_falls_back_to_root(conn):
    """拖进来的项没有更近的公共祖先 → LCA 升到总账（不是报错）。"""
    create_node(conn, '甲', ['tx:1'])          # 餐饮
    create_node(conn, '乙', ['tx:5'])          # 交通
    new = create_node(conn, '合并', ['tx:1', 'tx:5'])
    root = read_ledger(conn)
    merged = child(root, '合并')
    assert merged.id == new and merged.parent_id == 'root'
    assert merged.value == (13000, 0)


def test_create_node_carries_whole_group(conn):
    """拖进来的 t 属于某个 +2 组 → 整组跟着走（R2：组成员不可分割）。"""
    create_node(conn, '餐饮', ['tx:1', 'tx:4'])
    gid = create_net_group(conn, ['tx:1', 'tx:4'], '对冲组')
    create_node(conn, '新组', ['tx:1'])          # 只拖了组里的 t1
    # 新组挂在 LCA（M 餐饮）下，整组跟着 t1 一起进去
    grp = group(child(child(cat(read_ledger(conn), '餐饮'), '餐饮'), '新组'), '对冲组')
    assert grp.id == gid
    assert [m.id for m in grp.members] == [1, 4]


# ── 边界 3：移动的四种组合 + 搬空之后 ───────────────────────────────────────

def test_move_single_tx(conn):
    create_node(conn, '甲', ['tx:1'])
    target = create_node(conn, '乙', ['tx:3'])
    move(conn, ['tx:1'], target)
    node = child(cat(read_ledger(conn), '餐饮'), '乙')
    assert any(isinstance(c, Tx) and c.id == 1 for c in node.children)


def test_move_multiple_txs_from_different_parents(conn):
    """移动集合唯一：来自不同父节点的多个 t 一起搬，但不合并成新的 M。"""
    create_node(conn, '甲', ['tx:1'])
    create_node(conn, '乙', ['tx:3'])
    target = create_node(conn, '目标', ['tx:5'])
    move(conn, ['tx:1', 'tx:3'], target)
    node = child(cat(read_ledger(conn), '交通'), '目标')
    assert tx_ids(node) == [1, 3, 5]                              # 5 本来就在
    assert not any(isinstance(c, Node) for c in node.children)    # 没有凭空多出 M


def test_move_node_carries_its_subtree(conn):
    outer = create_node(conn, '外层', ['tx:1'])
    inner = create_node(conn, '内层', ['tx:3'])
    move(conn, [inner], outer)                    # 内层先挂到外层下
    target = create_node(conn, '目标', ['tx:4'])
    move(conn, [inner], target)
    node = child(cat(read_ledger(conn), '餐饮'), '目标')
    assert child(node, '内层').id == inner


def test_move_multiple_nodes(conn):
    a = create_node(conn, '甲', ['tx:1'])
    b = create_node(conn, '乙', ['tx:3'])
    target = create_node(conn, '目标', ['tx:4'])
    move(conn, [a, b], target)
    node = child(cat(read_ledger(conn), '餐饮'), '目标')
    assert sorted(c.id for c in node.children if isinstance(c, Node)) == sorted([a, b])


def test_move_node_and_tx_together(conn):
    a = create_node(conn, '甲', ['tx:1'])
    target = create_node(conn, '目标', ['tx:4'])
    move(conn, [a, 'tx:3'], target)
    node = child(cat(read_ledger(conn), '餐饮'), '目标')
    assert any(isinstance(c, Node) and c.id == a for c in node.children)
    assert any(isinstance(c, Tx) and c.id == 3 for c in node.children)


def test_emptied_node_stays_in_full_tree_but_leaves_scope(conn):
    """搬空之后：全量树里**还有**这个空节点；scope 投影里它**整个不出现**。"""
    create_node(conn, '甲', ['tx:1'])
    empty_me = create_node(conn, '待搬空', ['tx:3'])
    target = create_node(conn, '目标', ['tx:4'])
    move(conn, ['tx:3'], target)

    root = read_ledger(conn)
    assert find_node(root, empty_me) is not None
    assert child(cat(root, '餐饮'), '待搬空').value == (0, 0)

    proj = read_scope(root, scope_by_date(conn, '2026-01-01', '2026-01-31'))
    assert find_node(proj, empty_me) is None


# ── 边界 4：解散一个 M，+2 条目和里面的 t 的父亲一起变 ───────────────────────

def test_dissolve_moves_group_and_its_members_to_parent(conn):
    """M_1 下有 [t1] [t3,t4]+2 —— 解散 M_1 后，组和组里的 t 都上移到 M_1 的父节点（分类节点）。"""
    m1 = create_node(conn, 'M_1', ['tx:1', 'tx:3', 'tx:4'])
    gid = create_net_group(conn, ['tx:3', 'tx:4'], '组A')

    dissolve_node(conn, m1)

    row = conn.execute('SELECT node_id FROM ledger2_net_group WHERE id=?', (gid,)).fetchone()
    assert row['node_id'] == 'cat:餐饮'
    for tx_id in (3, 4):
        row = conn.execute("SELECT node_id, net_group_id FROM ledger2_tx WHERE tx_id=? AND role='primary'", (tx_id,)).fetchone()
        assert row['node_id'] == 'cat:餐饮'
        assert row['net_group_id'] == gid
    # t1 不在组里 → 同样上移到分类节点（归属行始终存在）
    row = conn.execute("SELECT node_id, net_group_id FROM ledger2_tx WHERE tx_id=1 AND role='primary'").fetchone()
    assert row['node_id'] == 'cat:餐饮' and row['net_group_id'] is None
    node = cat(read_ledger(conn), '餐饮')
    assert [m.id for m in group(node, '组A').members] == [3, 4]
    assert tx_ids(node) == [1]


def test_dissolve_node_with_child_group_keeps_group(conn):
    """解散 M_1，而 M_1 的子 M 里有组 → 子 M 上移，组留在子 M 下（位置不变）。"""
    m1 = create_node(conn, 'M_1', ['tx:1'])
    m2 = create_node(conn, 'M_2', ['tx:3', 'tx:4'])
    move(conn, [m2], m1)
    gid = create_net_group(conn, ['tx:3', 'tx:4'], '子组')
    dissolve_node(conn, m1)
    row = conn.execute('SELECT node_id FROM ledger2_net_group WHERE id=?', (gid,)).fetchone()
    assert row['node_id'] == m2
    assert group(child(cat(read_ledger(conn), '餐饮'), 'M_2'), '子组').id == gid


# ════════════════════════════════════════════════════════════════════════════
# V2 新增：平台分类预设（cat: 真节点）
# ════════════════════════════════════════════════════════════════════════════

def test_pure_read_writes_zero_rows(conn):
    from backend.ledger2 import service
    before = snapshot(conn)
    read_ledger(conn)
    service.get_tree(conn)
    service.get_tree(conn, '2026-01-01', '2026-01-31')
    assert snapshot(conn) == before
    assert table_count(conn, 'ledger2_op_log') == 0


def test_preset_category_nodes_have_kind_and_root_parent(conn):
    root = read_ledger(conn)
    for c in root.children:
        assert c.kind == 'category' and c.id == f'cat:{c.name}' and c.parent_id == 'root'
    assert root.kind == 'root'
    assert cat(root, '餐饮').value == (7500, 0)
    assert cat(root, '工资').value == (0, 800000)


def test_move_between_category_nodes_writes_one_row_change(conn):
    nodes_before = snapshot(conn)['ledger2_node']
    move(conn, ['tx:8'], 'cat:餐饮')
    assert snapshot(conn)['ledger2_node'] == nodes_before      # 搬交易不增删节点
    assert table_count(conn, 'ledger2_tx') == 7
    assert 8 in tx_ids(cat(read_ledger(conn), '餐饮'))
    assert 8 not in tx_ids(cat(read_ledger(conn), '购物'))


def test_moving_back_to_the_original_category_restores_the_state(conn):
    before = snapshot(conn)
    move(conn, ['tx:8'], 'cat:餐饮')
    move(conn, ['tx:8'], 'cat:购物')
    assert snapshot(conn) == before                            # 没有「默认父节点」这回事：行始终在
    assert table_count(conn, 'ledger2_tx') == 7
    assert 8 in tx_ids(cat(read_ledger(conn), '购物'))


def test_move_to_own_current_node_is_a_noop_error(conn):
    with pytest.raises(LedgerError) as e:
        move(conn, ['tx:8'], 'cat:购物')
    assert e.value.code == 'NO_EFFECT'
    assert table_count(conn, 'ledger2_op_log') == 0


def test_move_to_unknown_category_is_rejected(conn):
    with pytest.raises(LedgerError) as e:
        move(conn, ['tx:1'], 'cat:不存在的分类')
    assert e.value.code == 'NODE_NOT_FOUND'
    assert snapshot(conn)['ledger2_node'] == snapshot(make_empty_after(conn))['ledger2_node']


def make_empty_after(conn):
    return conn


def test_undo_redo_of_a_move_restores_rows_exactly(conn):
    before = snapshot(conn)
    move(conn, ['tx:8'], 'cat:餐饮')
    after = snapshot(conn)
    assert after != before
    undo(conn)
    assert snapshot(conn) == before
    redo(conn)
    assert snapshot(conn) == after


def test_create_node_under_a_category_node_is_one_op(conn):
    before = snapshot(conn)
    create_node(conn, 'X', ['tx:1'])
    assert table_count(conn, 'ledger2_op_log') == 1          # 一次操作，不是两次
    undo(conn)
    assert snapshot(conn) == before


def test_category_node_can_be_moved_under_an_m(conn):
    before = snapshot(conn)
    transport_m = create_node(conn, '杂项', ['tx:5'])         # M 在分类节点「交通」下
    mid = snapshot(conn)
    move(conn, ['cat:购物'], transport_m)
    root = read_ledger(conn)
    assert find_node(root, 'cat:购物').parent_id == transport_m
    assert all(c.id != 'cat:购物' for c in root.children)
    shop = child(cat(root, '交通'), '杂项').children
    assert any(isinstance(c, Node) and c.id == 'cat:购物' and tx_ids(c) == [8] for c in shop)
    assert root.value == ROOT_VALUE                           # 搬分类节点不改总额
    undo(conn)
    assert snapshot(conn) == mid
    undo(conn)
    assert snapshot(conn) == before


def test_category_node_cannot_move_into_its_own_subtree(conn):
    m = create_node(conn, '杂项', ['tx:5'])
    with pytest.raises(LedgerError) as e:
        move(conn, ['cat:交通'], m)
    assert e.value.code == 'CYCLE'


def test_category_nodes_are_ordinary_nodes_can_rename_and_dissolve(conn):
    """平台分类节点只是初始化时建的普通节点：能改名、能解散（交易上移到根），撤销逐行还原。"""
    before = snapshot(conn)
    rename_node(conn, 'cat:餐饮', '吃吃吃')
    root = read_ledger(conn)
    assert find_node(root, 'cat:餐饮').name == '吃吃吃'
    assert find_node(root, 'cat:餐饮').kind == 'category'      # kind 由 id 约定派生，改名不影响
    dissolve_node(conn, 'cat:餐饮')
    root = read_ledger(conn)
    assert find_node(root, 'cat:餐饮') is None
    assert {1, 3, 4} <= set(tx_ids(root))                      # 交易上移到根
    assert root.value == ROOT_VALUE
    undo(conn)
    undo(conn)
    assert snapshot(conn) == before


def test_root_cannot_be_dissolved_or_renamed(conn):
    with pytest.raises(LedgerError) as e:
        dissolve_node(conn, 'root')
    assert e.value.code == 'CANNOT_DISSOLVE_ROOT'
    with pytest.raises(LedgerError) as e:
        rename_node(conn, 'root', '新总账')
    assert e.value.code == 'CANNOT_RENAME_ROOT'


def test_preset_category_comes_from_manual_override(tmp_path):
    c = make_db(tmp_path / 'override.db', init=False)
    c.execute("INSERT INTO tx_overrides(tx_id, manual_category) VALUES(1, '早餐')")
    c.commit()
    from backend.ledger2 import presets
    presets.init_by_platform_category(c)
    root = read_ledger(c)
    assert tx_ids(cat(root, '早餐')) == [1]
    assert tx_ids(cat(root, '餐饮')) == [3, 4]
    c.close()


def test_placed_tx_stays_put_when_its_platform_category_changes_later(conn):
    """初始化之后，平台分类再变，不会把交易从它现在的位置带走；分类字段（category_path）等静默刷新再说。"""
    create_node(conn, 'X', ['tx:1'])
    conn.execute("INSERT INTO tx_overrides(tx_id, manual_category) VALUES(1, '早餐')")
    conn.commit()
    root = read_ledger(conn)
    assert tx_ids(child(cat(root, '餐饮'), 'X')) == [1]
    assert facts.load_txs(conn)[1].category_path == ['餐饮']   # 初始化时的完整路径；静默刷新尚未实现
    with pytest.raises(LedgerError) as e:
        move(conn, ['tx:1'], 'cat:早餐')                         # 没人建过这个节点
    assert e.value.code == 'NODE_NOT_FOUND'


# ════════════════════════════════════════════════════════════════════════════
# V2 新增：改名 / 解散组
# ════════════════════════════════════════════════════════════════════════════

def test_rename_node_with_undo_redo(conn):
    m = create_node(conn, '旧名', ['tx:1'])
    created = snapshot(conn)
    rename_node(conn, m, '  新名 ')
    assert find_node(read_ledger(conn), m).name == '新名'
    undo(conn)
    assert find_node(read_ledger(conn), m).name == '旧名' and snapshot(conn) == created
    redo(conn)
    assert find_node(read_ledger(conn), m).name == '新名'


def test_rename_node_rejects_empty_and_unchanged(conn):
    m = create_node(conn, '名字', ['tx:1'])
    with pytest.raises(LedgerError) as e:
        rename_node(conn, m, '   ')
    assert e.value.code == 'EMPTY_NAME'
    with pytest.raises(LedgerError) as e:
        rename_node(conn, m, '名字')
    assert e.value.code == 'NO_EFFECT'
    with pytest.raises(LedgerError) as e:
        rename_node(conn, 'nope', 'x')
    assert e.value.code == 'NODE_NOT_FOUND'


def test_rename_group_with_undo_redo(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    g = create_net_group(conn, ['tx:1', 'tx:3'], '旧组名')
    created = snapshot(conn)
    rename_group(conn, g, '新组名')
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    assert group(node, '新组名').id == g
    undo(conn)
    assert snapshot(conn) == created
    redo(conn)
    assert group(child(cat(read_ledger(conn), '餐饮'), '餐饮'), '新组名').id == g
    with pytest.raises(LedgerError) as e:
        rename_group(conn, 'nope', 'x')
    assert e.value.code == 'GROUP_NOT_FOUND'


def test_dissolve_group_in_an_m_keeps_members_in_the_m(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')
    grouped = snapshot(conn)
    dissolve_group(conn, g)
    assert table_count(conn, 'ledger2_net_group') == 0
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    assert tx_ids(node) == [1, 3] and not any(isinstance(c, NetGroup) for c in node.children)
    assert node.value == (5000, 0)
    undo(conn)
    assert snapshot(conn) == grouped
    redo(conn)
    assert table_count(conn, 'ledger2_net_group') == 0


def test_dissolve_group_directly_under_a_category_node_keeps_member_rows(conn):
    before = snapshot(conn)
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')           # t1/t3 同在分类节点「餐饮」
    assert table_count(conn, 'ledger2_tx') == 7
    grouped = snapshot(conn)
    dissolve_group(conn, g)
    assert table_count(conn, 'ledger2_tx') == 7           # 行始终在，只是不再带组
    assert snapshot(conn) == before
    assert tx_ids(cat(read_ledger(conn), '餐饮')) == [1, 3, 4]
    undo(conn)
    assert snapshot(conn) == grouped


def test_dissolve_unknown_group_is_rejected(conn):
    with pytest.raises(LedgerError) as e:
        dissolve_group(conn, 'nope')
    assert e.value.code == 'GROUP_NOT_FOUND'


# ════════════════════════════════════════════════════════════════════════════
# V2 新增：不变量与回滚
# ════════════════════════════════════════════════════════════════════════════

def test_violations_empty_on_healthy_states(conn):
    assert invariants.violations(conn) == []
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    move(conn, ['tx:5'], food)
    split_tx(conn, 5)
    assert invariants.violations(conn) == []
    undo_all(conn)
    assert invariants.violations(conn) == []


def test_violations_detects_row_marked_uninitialized(conn):
    conn.execute("UPDATE ledger2_tx SET initialized=0 WHERE tx_id=8 AND role='primary'")
    conn.commit()
    found = invariants.violations(conn)
    assert any('未初始化' in v for v in found)
    with pytest.raises(LedgerError) as e:
        invariants.check(conn)
    assert e.value.code == 'INVARIANT_VIOLATION' and e.value.detail['violations'] == found
    conn.execute("UPDATE ledger2_tx SET initialized=1 WHERE tx_id=8 AND role='primary'")
    conn.commit()
    assert invariants.violations(conn) == []


def test_violations_detects_group_with_one_member_and_split_parents(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')
    conn.execute("UPDATE ledger2_tx SET node_id='root' WHERE tx_id=1 AND role='primary'")
    conn.commit()
    assert any('不同父' in v for v in invariants.violations(conn))
    conn.execute("UPDATE ledger2_tx SET node_id=(SELECT node_id FROM ledger2_net_group WHERE id=?) "
                 "WHERE tx_id=1", (g,))
    conn.execute('DELETE FROM ledger2_tx WHERE tx_id=3')
    conn.commit()
    assert any('只有 1 个成员' in v for v in invariants.violations(conn))


def test_violations_detects_cycle_and_missing_root_and_orphan_mirror(conn):
    a = create_node(conn, 'A', ['tx:1'])
    b = create_node(conn, 'B', ['tx:3'])
    conn.execute('UPDATE ledger2_node SET parent_id=? WHERE id=?', (b, a))
    conn.execute('UPDATE ledger2_node SET parent_id=? WHERE id=?', (a, b))
    conn.commit()
    assert any('成环' in v for v in invariants.violations(conn))

    conn.execute("INSERT INTO ledger2_tx(tx_id, role, node_id) VALUES(5, 'mirror', 'root')")
    conn.commit()
    assert any('镜像' in v for v in invariants.violations(conn))


def test_violations_detects_missing_root(tmp_path):
    c = make_db(tmp_path / 'noroot.db', init=False)
    c.execute('DELETE FROM ledger2_node')
    c.commit()
    assert any('根节点数量' in v for v in invariants.violations(c))
    c.close()


def test_failing_op_rolls_back_and_leaves_op_log_untouched(conn):
    create_node(conn, 'A', ['tx:1'])
    undo(conn)                                                  # A 现在在重做栈上
    conn.execute("UPDATE ledger2_tx SET initialized=0 WHERE tx_id=8 AND role='primary'")  # 人为造坏
    conn.commit()
    snap = snapshot(conn)
    log_before = [dict(r) for r in conn.execute('SELECT * FROM ledger2_op_log ORDER BY seq')]

    with pytest.raises(LedgerError) as e:
        create_node(conn, 'B', ['tx:3'])
    assert e.value.code == 'INVARIANT_VIOLATION'

    assert snapshot(conn) == snap                                # 一行都没多
    log_after = [dict(r) for r in conn.execute('SELECT * FROM ledger2_op_log ORDER BY seq')]
    assert log_after == log_before                               # 日志原样，重做栈也没被误清
    assert not conn.in_transaction


def test_undo_and_redo_also_run_the_invariant_check(conn):
    create_node(conn, 'A', ['tx:1'])
    conn.execute("UPDATE ledger2_tx SET initialized=0 WHERE tx_id=8 AND role='primary'")  # 人为造坏
    conn.commit()
    snap = snapshot(conn)
    with pytest.raises(LedgerError) as e:
        undo(conn)
    assert e.value.code == 'INVARIANT_VIOLATION'
    assert snapshot(conn) == snap
    assert [h['state'] for h in history(conn)] == ['applied']


# ════════════════════════════════════════════════════════════════════════════
# V2 新增：已删除交易的宽容读
# ════════════════════════════════════════════════════════════════════════════

def _delete(conn, tx_id):
    conn.execute('INSERT INTO tx_deletions(tx_id) VALUES(?)', (tx_id,))
    conn.commit()


def test_dead_member_is_dropped_but_group_still_shows(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    _delete(conn, 1)                                           # 归属行还在，交易已被删
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    g = group(node, '组')
    assert [m.id for m in g.members] == [3]
    assert g.value == (2000, 0)
    assert node.value == (2000, 0)


def test_dead_placed_tx_disappears_without_error(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    _delete(conn, 1)
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    assert tx_ids(node) == [3]


def test_group_with_all_members_dead_is_not_shown(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    create_net_group(conn, ['tx:1', 'tx:3'], '组')
    _delete(conn, 1)
    _delete(conn, 3)
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮')
    assert not any(isinstance(c, NetGroup) for c in node.children) and node.value == (0, 0)


def test_dead_tx_leaves_its_category_node_but_the_node_stays(conn):
    _delete(conn, 2)
    root = read_ledger(conn)
    salary = cat(root, '工资')                                  # 分类节点是真节点：最后一笔没了，节点还在
    assert tx_ids(salary) == [] and salary.value == (0, 0)
    assert root.value == (22500, 5000)
    proj = read_scope(root, scope_by_date(conn, '2026-01-01', '2026-12-31'))
    assert all(c.id != 'cat:工资' for c in proj.children)      # 范围投影里空节点不出现（N11）


def test_dead_tx_cannot_be_operated_on(conn):
    _delete(conn, 1)
    for call in (lambda: move(conn, ['tx:1'], 'cat:购物'),
                 lambda: create_node(conn, 'X', ['tx:1']),
                 lambda: split_tx(conn, 1)):
        with pytest.raises(LedgerError) as e:
            call()
        assert e.value.code == 'TX_NOT_FOUND'


def test_dissolving_an_m_that_holds_a_dead_tx_still_works(conn):
    m = create_node(conn, 'X', ['tx:1', 'tx:3'])
    _delete(conn, 1)
    dissolve_node(conn, m)                                     # 死交易的归属行跟着上移，不报错
    assert tx_ids(cat(read_ledger(conn), '餐饮')) == [3, 4]
    assert invariants.violations(conn) == []


def test_moving_a_group_that_has_a_dead_member_still_works(conn):
    create_node(conn, '餐饮', ['tx:1', 'tx:3'])
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')
    target = create_node(conn, '目标', ['tx:5'])
    _delete(conn, 1)
    move(conn, ['tx:3'], target)                               # 整组（含死成员）一起走
    node = child(cat(read_ledger(conn), '交通'), '目标')
    assert group(node, '组').id == g and [m.id for m in group(node, '组').members] == [3]
    assert invariants.violations(conn) == []


def test_reader_output_is_deterministic_for_group_members(conn):
    """组成员按 (时间, id) 排序，不依赖表的物理顺序。"""
    create_node(conn, '餐饮', ['tx:1', 'tx:3', 'tx:4'])
    # 反着加：先 t4 再 t1
    g = create_net_group(conn, ['tx:4', 'tx:3'], '组')
    add_group_member(conn, g, 'tx:1')
    grp = group(child(cat(read_ledger(conn), '餐饮'), '餐饮'), '组')
    assert [m.id for m in grp.members] == [1, 3, 4]


# ════════════════════════════════════════════════════════════════════════════
# 分裂的路径继承：t 在不同位置时 t′ 的落点（回归：split 曾对非默认位置的 t 报 TX_NOT_FOUND）
# ════════════════════════════════════════════════════════════════════════════

def test_split_of_a_category_level_tx_writes_one_mirror_row(conn):
    split_tx(conn, 3)
    assert table_count(conn, 'ledger2_tx') == 8         # t′ 也必须有显式归属行
    assert table_count(conn, 'ledger2_split_tx') == 1
    node = cat(read_ledger(conn), '餐饮')
    assert [c.role for c in node.children if isinstance(c, Tx) and c.id == 3] == ['mirror', 'primary']


def test_split_of_tx_moved_into_an_m_puts_mirror_in_the_same_m(conn):
    m = create_node(conn, '餐饮M', ['tx:1', 'tx:3'])
    before = snapshot(conn)
    split_tx(conn, 3)
    row = conn.execute("SELECT node_id, net_group_id FROM ledger2_tx WHERE tx_id=3 AND role='mirror'").fetchone()
    assert row['node_id'] == m and row['net_group_id'] is None
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮M')
    assert sorted((c.id, c.role) for c in node.children if isinstance(c, Tx)) == [
        (1, 'primary'), (3, 'mirror'), (3, 'primary')]
    assert node.value == (5000, 2000)         # t1 支出30 + t3 支出20 + t′3 收入20
    undo(conn)
    assert snapshot(conn) == before
    redo(conn)
    assert table_count(conn, 'ledger2_split_tx') == 1


def test_split_of_tx_in_a_group_does_not_enter_the_group(conn):
    m = create_node(conn, '餐饮M', ['tx:1', 'tx:3'])
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')
    before = snapshot(conn)
    split_tx(conn, 3)
    row = conn.execute("SELECT node_id, net_group_id FROM ledger2_tx WHERE tx_id=3 AND role='mirror'").fetchone()
    assert row['node_id'] == m and row['net_group_id'] is None   # 与 t 同父，但不在组里
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮M')
    assert [(x.id, x.role) for x in group(node, '组').members] == [(1, 'primary'), (3, 'primary')]
    assert [(c.id, c.role) for c in node.children if isinstance(c, Tx)] == [(3, 'mirror')]
    undo(conn)
    assert snapshot(conn) == before
    unsplit_tx_after = snapshot(conn)
    redo(conn)
    unsplit_tx(conn, 3)
    assert snapshot(conn) == unsplit_tx_after
    assert group(child(cat(read_ledger(conn), '餐饮'), '餐饮M'), '组').id == g


def test_split_of_tx_directly_under_a_group_at_category_level(conn):
    g = create_net_group(conn, ['tx:1', 'tx:3'], '组')           # 组直接挂在分类节点下
    split_tx(conn, 3)
    node = cat(read_ledger(conn), '餐饮')
    assert [m.role for m in group(node, '组').members] == ['primary', 'primary']
    assert [(c.id, c.role) for c in node.children if isinstance(c, Tx) and c.role == 'mirror'] == [(3, 'mirror')]
    assert node.value == (7500, 2000)         # 组(t1,t3)=支出50 + t4 支出25；t′3 收入20
    assert group(node, '组').id == g


def test_unsplit_of_a_mirror_in_a_group_dissolves_a_two_member_group(conn):
    create_node(conn, '餐饮M', ['tx:1', 'tx:3'])
    split_tx(conn, 3)
    create_net_group(conn, ['tx:1', 'txm:3'], '含副本的组')
    snap_grouped = snapshot(conn)
    unsplit_tx(conn, 3)
    assert table_count(conn, 'ledger2_net_group') == 0 and table_count(conn, 'ledger2_split_tx') == 0
    node = child(cat(read_ledger(conn), '餐饮'), '餐饮M')
    assert tx_ids(node) == [1, 3]
    undo(conn)
    assert snapshot(conn) == snap_grouped


def test_split_of_unknown_tx_is_rejected(conn):
    with pytest.raises(LedgerError) as e:
        split_tx(conn, 999)
    assert e.value.code == 'TX_NOT_FOUND'
    with pytest.raises(LedgerError) as e:
        unsplit_tx(conn, 3)
    assert e.value.code == 'NOT_SPLIT'
