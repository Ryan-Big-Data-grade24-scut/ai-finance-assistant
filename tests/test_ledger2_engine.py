"""账本 V2 · 引擎层（service 的 6 点体系）：初始化 / 最简 / 最繁 / 改名备注 / 批撤销。

树形一律现搭现用，绝不碰 db/bills.db。
"""
from __future__ import annotations

import pytest

from backend.ledger2 import facts, ops, service
from backend.ledger2.errors import LedgerError
from ledger2_support import make_db, snapshot, table_count


@pytest.fixture()
def conn(tmp_path):
    c = make_db(tmp_path / 'engine.db')
    yield c
    c.close()


def _node(conn, name, items):
    return service.apply(conn, {'type': 'create_node', 'name': name, 'items': items})['created']['node_id']


def _build_example(conn):
    """搭出用户的例子：M(t1, M_1)；M_1(M_12, t2, t3)；M_12(t4, t5)。"""
    m12 = _node(conn, 'M12', ['tx:4', 'tx:5'])
    m1 = _node(conn, 'M1', ['tx:2', 'tx:3'])
    service.apply(conn, {'type': 'move', 'items': [m12], 'target': m1})
    m = _node(conn, 'M', ['tx:1', m1])
    return m, m1, m12


# ── 最简 / 最繁（用户的原例子）────────────────────────────────────────────

def test_fold_minimal_equals_t1_m12_t3(conn):
    """选 t1, M_12, t3, t4 → 最简等价于 t1 + M_12 + t3（t4 被 M_12 覆盖）。"""
    _m, _m1, m12 = _build_example(conn)
    nodes, groups, txs = ops.fold_selection(conn, ['tx:1', m12, 'tx:3', 'tx:4'])
    assert nodes == [m12] and groups == []
    assert {t.key for t in txs} == {(1, 'primary'), (3, 'primary')}


def test_expand_maximal_covers_whole_subtree(conn):
    """同一次选择的最繁：节点取 M_12，交易取整棵 M_12 的 t4/t5，外加 t1/t3。"""
    _m, _m1, m12 = _build_example(conn)
    nodes, groups, txs = ops.expand_selection(conn, ['tx:1', m12, 'tx:3', 'tx:4'])
    assert nodes == {m12} and groups == set()
    assert txs == {(1, 'primary'), (3, 'primary'), (4, 'primary'), (5, 'primary')}


def test_move_with_minimal_fold_changes_only_three_rows(conn):
    """移动 = 只改 t1.node、t3.node、M_12.parent；M_1 里面的结构原样。"""
    m, m1, m12 = _build_example(conn)
    before = snapshot(conn)
    service.apply(conn, {'type': 'move', 'items': ['tx:1', m12, 'tx:3', 'tx:4'], 'target': 'root'})
    after = snapshot(conn)

    changed = {
        table for table in before
        if before[table] != after[table]
    }
    assert changed == {'ledger2_node', 'ledger2_tx'}      # 节点父指针 + 两笔交易的归属
    assert ops.history(conn)[-1]['name'] == 'move'
    # 结构没散：M_12 下面还是 t4、t5；M_1 下面还是 t2、M? 自己
    rows = conn.execute("SELECT tx_id FROM ledger2_tx WHERE node_id=? ORDER BY tx_id", (m12,)).fetchall()
    assert [r[0] for r in rows] == [4, 5]


def test_fold_keeps_group_whole(conn):
    """最简：选中的交易在一个 +2 组里 → 收拢成整组引用，交易本身不再单独处理。"""
    gid = service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '组'})['created']['group_id']
    nodes, groups, txs = ops.fold_selection(conn, [f'g:{gid}', 'tx:1', 'tx:4'])
    assert nodes == [] and [g.id for g in groups] == [gid]
    assert {t.key for t in txs} == {(4, 'primary')}


def test_move_group_keeps_members_at_same_parent(conn):
    """移动一个 +2：组和每个成员的位置行一起变（R1 不破）。"""
    target = _node(conn, '别处', ['tx:4'])
    gid = service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '组'})['created']['group_id']
    service.apply(conn, {'type': 'move', 'items': [f'g:{gid}'], 'target': target})
    g = conn.execute('SELECT node_id FROM ledger2_net_group WHERE id=?', (gid,)).fetchone()
    rows = conn.execute('SELECT node_id FROM ledger2_tx WHERE net_group_id=?', (gid,)).fetchall()
    assert g[0] == target and {r[0] for r in rows} == {target}


# ── 解散（最繁）/ 取消分裂（最繁）─────────────────────────────────────────

def test_dissolve_batch_nested_one_step(conn):
    """父子一起解散：都删掉，内容落到「最近的没被解散的祖先」，一次撤销整体还原。"""
    m, m1, m12 = _build_example(conn)
    before = snapshot(conn)
    nodes_before = table_count(conn, 'ledger2_node')
    service.apply(conn, {'type': 'dissolve', 'items': [m1, m12]})
    assert table_count(conn, 'ledger2_node') == nodes_before - 2
    assert ops.history(conn)[-1]['name'] == 'dissolve'
    rows = conn.execute('SELECT tx_id FROM ledger2_tx WHERE node_id=? ORDER BY tx_id', (m,)).fetchall()
    assert [r[0] for r in rows] == [1, 2, 3, 4, 5]

    service.undo(conn)
    assert snapshot(conn) == before


def test_dissolve_group_via_engine(conn):
    gid = service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '组'})['created']['group_id']
    service.apply(conn, {'type': 'dissolve', 'items': [f'g:{gid}']})
    assert table_count(conn, 'ledger2_net_group') == 0
    assert conn.execute('SELECT COUNT(*) FROM ledger2_tx WHERE net_group_id IS NOT NULL').fetchone()[0] == 0


def test_dissolve_root_is_ignored(conn):
    """选中里混进根：忽略根，解散其余；只选根 → NOTHING_TO_DISSOLVE。"""
    m = _node(conn, 'M', ['tx:1'])
    service.apply(conn, {'type': 'dissolve', 'items': ['root', m]})
    assert conn.execute('SELECT 1 FROM ledger2_node WHERE id=?', (m,)).fetchone() is None
    with pytest.raises(LedgerError) as e:
        service.apply(conn, {'type': 'dissolve', 'items': ['root']})
    assert e.value.code == 'NOTHING_TO_DISSOLVE'


def test_unsplit_batch_dissolves_small_group(conn):
    """同一组的两个 t′ 一起取消分裂：组剩 0 人，R3 自动解散。"""
    _node(conn, 'M', ['tx:1', 'tx:3', 'tx:4'])
    service.apply(conn, {'type': 'split', 'items': ['tx:1', 'tx:3']})
    gid = service.apply(conn,
                        {'type': 'create_group', 'items': ['txm:1', 'txm:3'], 'name': 't′组'})['created']['group_id']
    assert conn.execute('SELECT COUNT(*) FROM ledger2_tx WHERE net_group_id=?', (gid,)).fetchone()[0] == 2

    result = service.apply(conn, {'type': 'unsplit', 'items': ['txm:1', 'txm:3']})
    assert result['created']['unsplit'] == [1, 3]       # 前端据此选中被还原 t′ 的原型 t
    assert table_count(conn, 'ledger2_split_tx') == 0
    assert table_count(conn, 'ledger2_net_group') == 0
    assert ops.history(conn)[-1]['name'] == 'unsplit'


def test_split_expands_selection_and_filters(conn):
    """选 M / +2 走最繁：子树里的原始 t 都分裂；t′ 和已分裂过的 t 跳过，不整批失败。"""
    m = _node(conn, 'M', ['tx:1', 'tx:3', 'tx:4'])
    service.apply(conn, {'type': 'split', 'items': ['tx:1']})       # t1 先裂开
    result = service.apply(conn, {'type': 'split', 'items': [m, 'txm:1']})
    assert result['created']['split'] == [3, 4]                      # 只报真正分裂的源交易
    rows = conn.execute('SELECT src_tx_id FROM ledger2_split_tx ORDER BY src_tx_id').fetchall()
    assert [r[0] for r in rows] == [1, 3, 4]
    assert ops.history(conn)[-1]['name'] == 'split'

    # 整个 M 都分裂过了 → 没有可分裂的
    with pytest.raises(LedgerError) as e:
        service.apply(conn, {'type': 'split', 'items': [m]})
    assert e.value.code == 'NOTHING_TO_SPLIT'


def test_split_group_members_via_expand(conn):
    """选 +2 组：最繁展开成全部成员，逐个分裂。"""
    gid = service.apply(conn, {'type': 'create_group', 'items': ['tx:3', 'tx:4'], 'name': '组'})['created']['group_id']
    service.apply(conn, {'type': 'split', 'items': [f'g:{gid}']})
    rows = conn.execute('SELECT src_tx_id FROM ledger2_split_tx ORDER BY src_tx_id').fetchall()
    assert [r[0] for r in rows] == [3, 4]
    # t′ 不在组里（分裂不继承 +2 关系）
    assert conn.execute('SELECT COUNT(*) FROM ledger2_tx WHERE net_group_id=?', (gid,)).fetchone()[0] == 2


# ── 加入 / 新建 +2 的「不能有 M」──────────────────────────────────────────

def test_join_and_create_group_reject_nodes(conn):
    m = _node(conn, 'M', ['tx:1', 'tx:3', 'tx:4'])
    gid = service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '组'})['created']['group_id']

    with pytest.raises(LedgerError) as e:
        service.apply(conn, {'type': 'join', 'items': [m, 'tx:4'], 'group_id': gid})
    assert e.value.code == 'JOIN_REQUIRES_TX'
    with pytest.raises(LedgerError) as e:
        service.apply(conn, {'type': 'create_group', 'items': [m], 'name': 'x'})
    assert e.value.code == 'JOIN_REQUIRES_TX'
    # 数据层兜底：就算绕过引擎，M 也进不了 +2
    with pytest.raises(LedgerError) as e:
        ops.add_group_members(conn, gid, [m])
    assert e.value.code == 'NOT_A_TX'


def test_join_adds_txs(conn):
    _node(conn, 'M', ['tx:1', 'tx:3', 'tx:4'])
    gid = service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '组'})['created']['group_id']
    service.apply(conn, {'type': 'join', 'items': ['tx:4'], 'group_id': gid})
    assert conn.execute('SELECT COUNT(*) FROM ledger2_tx WHERE net_group_id=?', (gid,)).fetchone()[0] == 3
    assert ops.history(conn)[-1]['name'] == 'join'


def test_create_group_requires_name(conn):
    with pytest.raises(LedgerError) as e:
        service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3']})
    assert e.value.code == 'BAD_OP'
    with pytest.raises(LedgerError) as e:
        service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '  '})
    assert e.value.code == 'EMPTY_NAME'


# ── 改名 / 备注 ────────────────────────────────────────────────────────────

def test_update_dispatch_and_undo(conn):
    m = _node(conn, 'M', ['tx:1', 'tx:3'])
    gid = service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '组'})['created']['group_id']
    before = snapshot(conn)

    service.apply(conn, {'type': 'update', 'item': m, 'name': 'M2', 'note': '节点备注'})
    assert conn.execute('SELECT name, note FROM ledger2_node WHERE id=?', (m,)).fetchone()[:] == ('M2', '节点备注')

    service.apply(conn, {'type': 'update', 'item': f'g:{gid}', 'note': '组备注'})
    assert conn.execute('SELECT note FROM ledger2_net_group WHERE id=?', (gid,)).fetchone()[0] == '组备注'

    service.apply(conn, {'type': 'update', 'item': 'tx:1', 'name': '午饭改名', 'note': 'tx备注'})
    assert conn.execute("SELECT title, note FROM ledger2_tx WHERE tx_id=1 AND role='primary'").fetchone()[:] == (
        '午饭改名', 'tx备注')

    for _ in range(3):
        service.undo(conn)
    assert snapshot(conn) == before


def test_update_group_note_is_readable(conn):
    gid = service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '组'})['created']['group_id']
    service.apply(conn, {'type': 'update', 'item': f'g:{gid}', 'note': '报销'})
    tree = service.get_tree(conn)['tree']
    group = next(c for c in _flatten(tree) if c['type'] == 'group' and c['id'] == gid)
    assert group['note'] == '报销'


def test_update_mirror_has_no_manual(conn):
    service.apply(conn, {'type': 'split', 'tx_id': 1})
    with pytest.raises(LedgerError) as e:
        service.apply(conn, {'type': 'update', 'item': 'txm:1', 'name': 'x'})
    assert e.value.code == 'MIRROR_HAS_NO_MANUAL'


# ── 初始化（引擎层）───────────────────────────────────────────────────────

def test_init_style_platform(tmp_path):
    c = make_db(tmp_path / 'init.db', init=False)
    try:
        assert service.get_tree(c)['uninit'] == 7
        r = service.apply(c, {'type': 'init', 'tx_ids': [1, 2, 3], 'style': 'platform'})
        assert r['op'] == 'init'
        tree = service.get_tree(c)
        assert tree['uninit'] == 4 and tree['scope']['tx_total'] == 3
        assert ops.history(c)[-1]['name'] == 'init'
        service.undo(c)
        assert service.get_tree(c)['uninit'] == 7
    finally:
        c.close()


def test_init_custom_plan(tmp_path):
    c = make_db(tmp_path / 'init2.db', init=False)
    try:
        service.apply(c, {'type': 'init', 'tx_ids': [1, 2, 3],
                          'plan': {'nodes': [{'ref': 'a', 'name': '手工'}],
                                   'place': [{'node': 'a', 'txs': [1, 2, 3]}]}})
        tree = service.get_tree(c)['tree']
        node = next(ch for ch in tree['children'] if ch['type'] == 'node' and ch['name'] == '手工')
        assert node['count'] == 3
    finally:
        c.close()


def test_init_rejects_style_and_plan_together(conn):
    with pytest.raises(LedgerError) as e:
        service.apply(conn, {'type': 'init', 'tx_ids': [1], 'style': 'platform', 'plan': {'nodes': []}})
    assert e.value.code == 'BAD_OP'


# ── 工具 ───────────────────────────────────────────────────────────────────

def _flatten(node: dict):
    yield node
    for c in node.get('children') or []:
        yield from _flatten(c)
    for m in node.get('members') or []:
        yield m
