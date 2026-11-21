"""账本 V2 · 门面（service）与 HTTP（/api/ledger2/*）契约。

API 测试用 `backend.main.app` + 一个 monkeypatch 到 tmp 路径的 `db.DB_PATH`：
绝不碰 db/bills.db（不进 `with TestClient`，不跑 startup）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.ledger2 import service
from backend.ledger2.errors import LedgerError
from ledger2_support import ROOT_VALUE, add_tx, init_all, make_db, table_count


@pytest.fixture()
def conn(tmp_path):
    c = make_db(tmp_path / 'svc.db')
    yield c
    c.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / 'api.db'
    make_db(path).close()
    monkeypatch.setattr(db, 'DB_PATH', path)
    from backend.main import app
    return TestClient(app)


def _cat(tree: dict, name: str) -> dict:
    return next(c for c in tree['children'] if c['id'] == f'cat:{name}')


# ── service.get_tree ────────────────────────────────────────────────────────

def test_get_tree_full_shape(conn):
    out = service.get_tree(conn)
    assert set(out) == {'tree', 'scope', 'status', 'uninit', 'available'}
    assert out['uninit'] == 0 and out['available']['first'] is not None
    assert (out['tree']['e'], out['tree']['i']) == ROOT_VALUE
    assert out['scope'] == {'date_from': None, 'date_to': None, 'tx_in_scope': 7, 'tx_total': 7}
    assert out['status']['can_undo'] is False and out['status']['can_redo'] is False
    assert out['status']['seq'] == 0 and out['status']['warnings'] == []


def test_get_tree_date_range_projection(conn):
    out = service.get_tree(conn, '2026-01-01', '2026-01-31')
    t = out['tree']
    assert out['scope'] == {'date_from': '2026-01-01', 'date_to': '2026-01-31', 'tx_in_scope': 5, 'tx_total': 7}
    assert (t['e'], t['i']) == (10000, 805000)               # 一月：支出 30+20+50，收入 8000+50
    ids = [c['id'] for c in t['children']]
    assert 'cat:交通' not in ids and 'cat:餐饮' in ids       # 二月才有的分类整个不出现
    assert _cat(t, '餐饮')['e'] == 5000


def test_get_tree_range_with_no_hits_keeps_root(conn):
    out = service.get_tree(conn, '2030-01-01', '2030-12-31')
    assert out['tree']['children'] == [] and (out['tree']['e'], out['tree']['i']) == (0, 0)
    assert out['scope']['tx_in_scope'] == 0 and out['scope']['tx_total'] == 7


@pytest.mark.parametrize('a,b', [('2026-01-01', None), (None, '2026-01-31'), ('2026-02-01', '2026-01-01')])
def test_get_tree_bad_range(conn, a, b):
    with pytest.raises(LedgerError) as e:
        service.get_tree(conn, a, b)
    assert e.value.code == 'BAD_RANGE' and e.value.status == 400


def test_get_tree_respects_deleted_transactions(conn):
    conn.execute('INSERT INTO tx_deletions(tx_id) VALUES(2)')
    conn.commit()
    out = service.get_tree(conn)
    assert out['scope']['tx_total'] == 6 and (out['tree']['e'], out['tree']['i']) == (22500, 5000)


def test_get_tree_projection_does_not_change_the_stored_state(conn):
    service.apply(conn, {'type': 'create_node', 'name': 'X', 'items': ['tx:1']})
    before = [tuple(r) for r in conn.execute('SELECT * FROM ledger2_node ORDER BY id')]
    service.get_tree(conn, '2026-02-01', '2026-02-28')       # 这个范围里 X 是空的，但只是投影
    assert [tuple(r) for r in conn.execute('SELECT * FROM ledger2_node ORDER BY id')] == before


# ── service.apply 分发 ──────────────────────────────────────────────────────

def test_apply_returns_created_ids_and_dispatches_every_op(conn):
    r = service.apply(conn, {'type': 'create_node', 'name': '餐饮M', 'items': ['tx:1', 'tx:3']})
    node_id = r['created']['node_id']
    assert r['op'] == 'create_node' and node_id
    r = service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '组'})
    gid = r['created']['group_id']
    service.apply(conn, {'type': 'rename_group', 'group_id': gid, 'name': '组2'})
    service.apply(conn, {'type': 'rename_node', 'node_id': node_id, 'name': '餐饮M2'})
    service.apply(conn, {'type': 'remove_member', 'group_id': gid, 'tx': 'tx:1'})      # 剩 1 个 → 自动解散
    assert table_count(conn, 'ledger2_net_group') == 0
    r = service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '组3'})
    gid = r['created']['group_id']
    service.apply(conn, {'type': 'dissolve_group', 'group_id': gid})
    service.apply(conn, {'type': 'move', 'items': ['tx:5'], 'target': node_id})
    service.apply(conn, {'type': 'split', 'tx_id': 5})
    assert table_count(conn, 'ledger2_split_tx') == 1
    service.apply(conn, {'type': 'unsplit', 'tx_id': 5})
    service.apply(conn, {'type': 'dissolve_node', 'node_id': node_id})
    assert service.status(conn)['can_undo'] is True


def test_apply_add_member(conn):
    node_id = service.apply(conn, {'type': 'create_node', 'name': 'M', 'items': ['tx:1', 'tx:3', 'tx:4']})['created']['node_id']
    gid = service.apply(conn, {'type': 'create_group', 'items': ['tx:1', 'tx:3'], 'name': '组'})['created']['group_id']
    service.apply(conn, {'type': 'add_member', 'group_id': gid, 'tx': 'tx:4'})
    row = conn.execute('SELECT COUNT(*) FROM ledger2_tx WHERE net_group_id=?', (gid,)).fetchone()
    assert row[0] == 3 and node_id


def test_apply_bad_ops(conn):
    with pytest.raises(LedgerError) as e:
        service.apply(conn, {'type': 'explode'})
    assert e.value.code == 'BAD_OP'
    with pytest.raises(LedgerError) as e:
        service.apply(conn, {})
    assert e.value.code == 'BAD_OP'
    with pytest.raises(LedgerError) as e:
        service.apply(conn, {'type': 'move', 'items': ['tx:1']})          # 缺 target
    assert e.value.code == 'BAD_OP' and 'target' in e.value.message
    assert table_count(conn, 'ledger2_op_log') == 0


def test_refund_pair_are_ordinary_atoms_and_can_be_split(tmp_path):
    """事实层不再按退款配对归零：退款和原单都是普通原子，分裂照常可用。"""
    c = make_db(tmp_path / 'z.db', rows=[])
    a = add_tx(c, tx_time='2026-03-01 10:00:00', direction='支出', amount=40, tx_type='购物',
               counterparty='店', item='鞋', tx_no='ORD-1')
    b = add_tx(c, tx_time='2026-03-02 10:00:00', direction='收入', amount=40, tx_type='退款',
               counterparty='店', item='退款-鞋', tx_no='ORD-1')
    init_all(c)
    from backend.ledger2 import facts
    txs = facts.load_txs(c)
    assert (txs[a].amount, txs[b].amount) == (4000, 4000)
    service.apply(c, {'type': 'split', 'tx_id': a})
    service.apply(c, {'type': 'split', 'tx_id': b})
    assert table_count(c, 'ledger2_split_tx') == 2
    c.close()


def test_split_missing_or_deleted_tx(conn):
    conn.execute('INSERT INTO tx_deletions(tx_id) VALUES(1)')
    conn.commit()
    for tx_id in (1, 999):
        with pytest.raises(LedgerError) as e:
            service.apply(conn, {'type': 'split', 'tx_id': tx_id})
        assert e.value.code == 'TX_NOT_FOUND' and e.value.status == 404


def test_split_positive_tx_through_service(conn):
    service.apply(conn, {'type': 'split', 'tx_id': 3})
    tree = service.get_tree(conn)['tree']
    mirrors = [x for x in _cat(tree, '餐饮')['children'] if x['type'] == 'tx' and x['role'] == 'mirror']
    assert len(mirrors) == 1 and (mirrors[0]['e'], mirrors[0]['i']) == (0, 2000)


# ── service.undo / redo / status ────────────────────────────────────────────

def test_undo_redo_status_cycle(conn):
    service.apply(conn, {'type': 'create_node', 'name': 'A', 'items': ['tx:1']})
    service.apply(conn, {'type': 'create_node', 'name': 'B', 'items': ['tx:3']})
    s = service.status(conn)
    assert s['seq'] == 2 and s['can_undo'] and not s['can_redo']
    r = service.undo(conn)
    assert r['op'] == 'undo' and r['undone'] == 'create_node'
    s = service.status(conn)
    assert s['seq'] == 1 and s['can_undo'] and s['can_redo']
    assert service.redo(conn)['redone'] == 'create_node'
    assert service.status(conn)['seq'] == 2 and not service.status(conn)['can_redo']


def test_undo_redo_with_empty_stacks(conn):
    with pytest.raises(LedgerError) as e:
        service.undo(conn)
    assert e.value.code == 'NOTHING_TO_UNDO' and e.value.status == 409
    with pytest.raises(LedgerError) as e:
        service.redo(conn)
    assert e.value.code == 'NOTHING_TO_REDO' and e.value.status == 409


def test_status_history_hides_discarded_and_is_capped(conn):
    service.apply(conn, {'type': 'create_node', 'name': 'A', 'items': ['tx:1']})
    service.undo(conn)
    service.apply(conn, {'type': 'create_node', 'name': 'B', 'items': ['tx:3']})     # A 被作废
    states = [h['state'] for h in service.status(conn)['history']]
    assert 'discarded' not in states and states == ['applied']
    for i in range(55):
        service.apply(conn, {'type': 'rename_node', 'node_id': _first_m(conn), 'name': f'n{i}'})
    assert len(service.status(conn)['history']) == 50


def _first_m(conn) -> str:
    return conn.execute("SELECT id FROM ledger2_node WHERE id != 'root' AND id NOT LIKE 'cat:%' LIMIT 1").fetchone()['id']


def test_status_warnings_surface_invariant_violations(conn):
    service.apply(conn, {'type': 'create_node', 'name': 'A', 'items': ['tx:1']})
    conn.execute("UPDATE ledger2_tx SET initialized=0 WHERE tx_id=8 AND role='primary'")  # 人为造坏
    conn.commit()
    assert any('未初始化' in w for w in service.status(conn)['warnings'])


# ── HTTP ────────────────────────────────────────────────────────────────────

def test_api_tree_full_and_ranged(client):
    r = client.get('/api/ledger2/tree')
    assert r.status_code == 200
    body = r.json()
    assert (body['tree']['e'], body['tree']['i']) == ROOT_VALUE and body['scope']['tx_total'] == 7
    r = client.get('/api/ledger2/tree', params={'date_from': '2026-02-01', 'date_to': '2026-02-28'})
    assert r.status_code == 200
    assert r.json()['scope']['tx_in_scope'] == 2
    assert sorted(c['id'] for c in r.json()['tree']['children']) == ['cat:交通', 'cat:餐饮']


def test_api_tree_bad_range_is_400_with_code(client):
    r = client.get('/api/ledger2/tree', params={'date_from': '2026-02-01'})
    assert r.status_code == 400
    assert r.json()['code'] == 'BAD_RANGE' and r.json()['message']


def test_api_op_undo_redo_roundtrip_returns_fresh_tree(client):
    r = client.post('/api/ledger2/op', json={'op': {'type': 'create_node', 'name': 'X', 'items': ['tx:1', 'tx:3']}})
    assert r.status_code == 200
    body = r.json()
    node_id = body['result']['created']['node_id']
    food = _cat(body['tree'], '餐饮')
    assert [c['name'] for c in food['children'] if c['type'] == 'node'] == ['X']
    assert body['status']['can_undo'] is True

    r = client.post('/api/ledger2/undo', json={})
    assert r.status_code == 200 and r.json()['result']['op'] == 'undo'
    food = _cat(r.json()['tree'], '餐饮')
    assert [c for c in food['children'] if c['type'] == 'node'] == []
    assert r.json()['status']['can_redo'] is True

    r = client.post('/api/ledger2/redo', json={})
    assert r.status_code == 200
    ids = [c['id'] for c in _cat(r.json()['tree'], '餐饮')['children'] if c['type'] == 'node']
    assert ids == [node_id]


def test_api_op_respects_date_range_in_response(client):
    r = client.post('/api/ledger2/op', json={
        'op': {'type': 'create_node', 'name': 'X', 'items': ['tx:1']},
        'date_from': '2026-02-01', 'date_to': '2026-02-28'})
    assert r.status_code == 200
    body = r.json()
    assert body['scope']['tx_in_scope'] == 2
    assert 'cat:工资' not in [c['id'] for c in body['tree']['children']]
    food = _cat(body['tree'], '餐饮')
    assert [c for c in food['children'] if c['type'] == 'node'] == []        # X 里只有一月的 t1 → 空 → 不出现


def test_api_error_mapping(client):
    r = client.post('/api/ledger2/op', json={'op': {'type': 'create_group', 'items': ['tx:1', 'tx:2'], 'name': '组'}})
    assert r.status_code == 409 and r.json()['code'] == 'SAME_PARENT_REQUIRED'
    r = client.post('/api/ledger2/op', json={'op': {'type': 'split', 'tx_id': 999}})
    assert r.status_code == 404 and r.json()['code'] == 'TX_NOT_FOUND'
    r = client.post('/api/ledger2/op', json={'op': {'type': 'move', 'items': ['tx:1']}})
    assert r.status_code == 400 and r.json()['code'] == 'BAD_OP'
    r = client.post('/api/ledger2/undo', json={})
    assert r.status_code == 409 and r.json()['code'] == 'NOTHING_TO_UNDO'
    r = client.post('/api/ledger2/redo', json={})
    assert r.status_code == 409 and r.json()['code'] == 'NOTHING_TO_REDO'


def test_api_rejects_unknown_fields_and_ops(client):
    # main.py 的全局校验处理器把 Pydantic 拒绝统一翻成 400；碰原始字段名则是项目招牌的 403
    r = client.post('/api/ledger2/op', json={'op': {'type': 'nope'}})
    assert r.status_code == 400
    r = client.post('/api/ledger2/op', json={'op': {'type': 'move', 'items': ['tx:1'], 'target': 'root', 'amount': 1}})
    assert r.status_code == 403
    r = client.post('/api/ledger2/op', json={'op': {'type': 'rename_node', 'node_id': 'root', 'name': 'x'}, 'extra': 1})
    assert r.status_code == 400


def test_api_failed_op_leaves_state_untouched(client):
    client.post('/api/ledger2/op', json={'op': {'type': 'create_node', 'name': 'X', 'items': ['tx:1']}})
    before = client.get('/api/ledger2/tree').json()
    r = client.post('/api/ledger2/op', json={'op': {'type': 'dissolve_node', 'node_id': 'root'}})
    assert r.status_code == 400 and r.json()['code'] == 'CANNOT_DISSOLVE_ROOT'
    assert client.get('/api/ledger2/tree').json() == before


def test_api_never_touches_the_real_database(client, tmp_path):
    """DB_PATH 被 monkeypatch 到 tmp：写请求只会落在 tmp 库里。"""
    client.post('/api/ledger2/op', json={'op': {'type': 'create_node', 'name': 'X', 'items': ['tx:1']}})
    assert db.DB_PATH.parent == tmp_path
    assert (tmp_path / 'api.db').exists()
