"""星图账本端到端合同：交易归位、节点金额、撤销、非法操作拒绝。"""
from __future__ import annotations
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from backend import db
from backend.main import app
from backend.ledger import service
from backend.ledger.errors import LedgerError


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch):
    path = tmp_path / 'ledger-test.db'
    monkeypatch.setattr(db, 'DB_PATH', path)
    db.init_db(path)
    conn = db.get_conn(path)
    conn.execute("INSERT INTO platforms(id,name,kind,adapter) VALUES('wechat','微信支付','wallet','wechat')")
    rows = [
        (1, '2026-09-05 09:00:00', '支出', 125.0, '餐饮'),
        (2, '2026-09-05 10:00:00', '收入', 112.40, '转账'),
        (3, '2026-10-04 11:00:00', '支出', 10.0, '交通'),
        (4, '2026-10-04 11:30:00', '中性', 2.25, '其他'),
    ]
    for tid, time, direction, amount, category in rows:
        date = time[:10]
        conn.execute('INSERT INTO transactions_raw (id,source,platform_id,tx_time,direction,amount,'
            'date,month,year,weekday,hour,dedupe_key,raw_json,tx_type,item) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (tid, '微信支付', 'wechat', time, direction, amount, date, date[:7], date[:4],
             5, 10, f'unique-{tid}', '{}', category, '测试交易'))
    conn.commit(); conn.close()
    yield path


def test_book_is_frozen_and_initial_values(seeded):
    result = service.create_book('测试星图', '2026-09-01', '2026-10-31')
    assert result['count'] == 4
    assert result['root_e'] == 137.25
    assert result['root_i'] == 112.40
    assert result['root_net'] == -24.85
    assert result['invariants_ok'] and len(result['invariants']) == 6
    assert len(result['nodes']) >= 4
    conn = db.get_conn()
    assert conn.execute('SELECT count(*) FROM tx_overrides').fetchone()[0] == 0
    assert conn.execute('SELECT count(*) FROM transactions_raw').fetchone()[0] == 4
    conn.close()


def test_move_group_net_undo_redo_and_reset(seeded):
    t = service.create_book('test', '2026-09-01', '2026-10-31')
    book = t['book']['id']
    group = service.apply_op(book, {'op': 'create_node', 'parent_id': t['root_id'],
        'name': 'AA 聚餐', 'op_value': 'NET', 'tx_ids': [1, 2]}, t['seq'])
    assert group['root_e'] == 24.85  # 125 - 112.40 + 10 + 2.25
    assert group['root_i'] == 0
    assert group['root_net'] == t['root_net']
    aa = next(n for n in group['nodes'] if n['name'] == 'AA 聚餐')
    assert aa['e'] == 12.6 and aa['i'] == 0 and aa['op'] == 'NET'
    moved = service.apply_op(book, {'op': 'reassign', 'tx_id': 2, 'to_node_id': t['root_id']}, group['seq'])
    assert moved['root_e'] == t['root_e'] and moved['root_i'] == t['root_i']
    assert moved['root_net'] == t['root_net']
    reverted = service.undo(book, moved['seq'])
    assert reverted['root_e'] == group['root_e']
    redone = service.redo(book, reverted['seq'])
    assert redone['root_e'] == moved['root_e']
    reset = service.reset_to_m0(book, redone['seq'])
    assert len(reset['nodes']) == 1 and reset['count'] == 4
    assert reset['root_e'] == t['root_e']
    undo_reset = service.undo(book, reset['seq'])
    assert undo_reset['seq'] == redone['seq']


def test_wrong_revision_and_cross_book_rejected(seeded):
    a = service.create_book('A', '2026-09-01', '2026-10-31')
    b = service.create_book('B', '2026-09-01', '2026-10-31')
    with pytest.raises(LedgerError) as e:
        service.apply_op(a['book']['id'], {'op': 'reassign', 'tx_id': 1, 'to_node_id': b['root_id']})
    assert e.value.code == 'NODE_NOT_FOUND'
    with pytest.raises(LedgerError) as e:
        service.apply_op(a['book']['id'], {'op': 'reassign', 'tx_id': 1,
            'to_node_id': a['root_id']}, expected_seq=13)
    assert e.value.status == 409
    with pytest.raises(LedgerError) as e:
        service.apply_op(a['book']['id'], {'op': 'split', 'tx_id': 1})
    assert e.value.code == 'SPLIT_NOT_APPLICABLE'


def test_http_endpoints_and_invalid_payload(seeded):
    with TestClient(app) as client:
        response = client.post('/api/ledger/books', json={
            'name': 'HTTP 测试', 'date_from': '2026-09-01', 'date_to': '2026-10-31',
        })
        assert response.status_code == 200
        t = response.json(); book = t['book']['id']
        assert client.get('/api/ledger/tree', params={'book_id': book}).json()['count'] == 4
        assert client.get('/api/ledger/status', params={'book_id': book}).json()['invariants_ok']
        moved = client.post('/api/ledger/op', json={'book_id': book, 'expected_seq': 0,
            'op': {'op': 'reassign', 'tx_id': 1, 'to_node_id': t['root_id']}})
        assert moved.status_code == 200 and moved.json()['seq'] == 1
        assert client.post('/api/ledger/undo', json={'book_id': book, 'expected_seq': 1}).json()['seq'] == 0
        assert client.post('/api/ledger/redo', json={'book_id': book, 'expected_seq': 0}).json()['seq'] == 1
        assert client.post('/api/ledger/reset', json={'book_id': book, 'expected_seq': 1}).json()['seq'] == 2
        assert client.get('/api/ledger/books').json()['books']
        bad = client.post('/api/ledger/op', json={'book_id': book,
            'op': {'op': 'reassign', 'tx_id': 1, 'to_node_id': t['root_id'], 'amount': 10000}})
        assert bad.status_code == 403 and bad.json()['detail']
        rejected = client.post('/api/ledger/op', json={'book_id': book,
            'op': {'op': 'split', 'tx_id': 1}})
        assert rejected.status_code == 400 and rejected.json()['code'] == 'SPLIT_NOT_APPLICABLE'
