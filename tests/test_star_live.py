"""Auto-synchronized star graph: no user-created date-frozen book required."""
from __future__ import annotations
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from backend import db
from backend.main import app
from backend.ledger import service


@pytest.fixture
def local_db(tmp_path: Path, monkeypatch):
    path = tmp_path / 'live.db'
    monkeypatch.setattr(db, 'DB_PATH', path)
    db.init_db(path)
    conn = db.get_conn(path)
    conn.execute("INSERT INTO platforms(id,name,kind,adapter) VALUES('wechat','微信支付','wallet','wechat')")
    for i, time, category, amount in [
        (1, '2025-12-25 10:00:00', '餐饮', 12.50),
        (2, '2026-09-05 10:00:00', '交通', 18.25),
        (3, '2026-09-06 10:00:00', '餐饮', 20.00),
    ]:
        date = time[:10]
        conn.execute('INSERT INTO transactions_raw '
          '(id,source,platform_id,tx_time,direction,amount,date,month,year,weekday,hour,dedupe_key,raw_json,tx_type,item) '
          'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
          (i,'微信支付','wechat',time,'支出',amount,date,date[:7],date[:4],1,10,f'id-{i}','{}',category,'测试'))
    conn.commit(); conn.close()
    return path


def test_auto_init_and_period_filter_do_not_change_membership(local_db):
    full = service.get_live_tree()
    assert full['count'] == full['total_count'] == 3
    assert full['source_count'] == 3
    assert full['root_e'] == 50.75
    assert full['invariants_ok']
    subset = service.get_live_tree('2026-09-01', '2026-09-30')
    assert subset['count'] == 2
    assert subset['root_e'] == 38.25
    assert subset['total_count'] == 3
    assert [t['id'] for t in subset['transactions']] == [2, 3]
    assert subset['seq'] == full['seq']
    assert service.get_live_tree()['count'] == 3


def test_category_filter_uses_true_tree_membership(local_db):
    full = service.get_live_tree()
    cat = next(n for n in full['nodes'] if n['name'] == '餐饮')
    result = service.get_live_tree(category_node_id=cat['id'])
    assert result['count'] == 2 and result['root_e'] == 32.50
    assert {t['id'] for t in result['transactions']} == {1, 3}
    assert service.get_live_tree('2025-01-01','2025-12-31',cat['id'])['count'] == 1


def test_sync_keeps_user_changes_and_picks_up_later_import(local_db):
    before = service.get_live_tree()
    root_id = before['root_id']; book_id = before['book']['id']
    custom = service.apply_op(book_id, {'op': 'create_node', 'parent_id': root_id,
                     'name': '我自己的细分', 'op_value': 'ADD', 'tx_ids': [2]}, before['seq'])
    custom_id = next(n['id'] for n in custom['nodes'] if n['name'] == '我自己的细分')
    conn = db.get_conn()
    conn.execute('INSERT INTO transactions_raw '
      '(id,source,platform_id,tx_time,direction,amount,date,month,year,weekday,hour,dedupe_key,raw_json,tx_type,item) '
      'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
      (4,'微信支付','wechat','2026-10-09 13:00:00','收入',99,'2026-10-09','2026-10','2026',4,13,'id-4','{}','交通','测试'))
    conn.commit(); conn.close()
    after = service.get_live_tree()
    assert after['count'] == 4 and after['root_i'] == 99.0
    assert next(t['node_id'] for t in after['transactions'] if t['id'] == 2) == custom_id
    assert after['seq'] == custom['seq']
    assert service.get_live_tree()['seq'] == after['seq'], 'Repeated reads must not create revisions'
    undone = service.undo(book_id, after['seq'])
    assert undone['seq'] == before['seq']
    after_undo = service.get_live_tree()
    assert after_undo['count'] == 4 and after_undo['root_i'] == 99.0
    assert all(n['name'] != '我自己的细分' for n in after_undo['nodes'])
    redone = service.redo(book_id, after_undo['seq'])
    assert redone['count'] == 4
    assert next(t['node_id'] for t in redone['transactions'] if t['id'] == 2) == custom_id
    assert service.get_live_tree(date_from='2026-10-01')['count'] == 1


def test_exempt_still_visible_but_deleted_record_disappears(local_db):
    conn = db.get_conn()
    conn.execute('INSERT INTO tx_overrides(tx_id,is_exempt) VALUES(1,1)')
    conn.execute("INSERT INTO tx_deletions(tx_id) VALUES(2)")
    conn.commit(); conn.close()
    result = service.get_live_tree()
    assert {t['id'] for t in result['transactions']} == {1, 3}
    conn = db.get_conn()
    conn.execute('DELETE FROM tx_deletions WHERE tx_id=2')
    conn.commit(); conn.close()
    result = service.get_live_tree()
    assert result['count'] == 3


def test_http_live_and_errors(local_db):
    with TestClient(app) as c:
        full = c.get('/api/ledger/live')
        assert full.status_code == 200 and full.json()['count'] == 3
        part = c.get('/api/ledger/live', params={'date_from':'2026-09-05', 'date_to':'2026-09-05'})
        assert part.status_code == 200 and part.json()['count'] == 1
        assert part.json()['root_e'] == 18.25
        bad = c.get('/api/ledger/live', params={'date_from':'2026-10-01','date_to':'2026-09-01'})
        assert bad.status_code == 400
        assert bad.json()['code'] == 'INVALID_DATE'
