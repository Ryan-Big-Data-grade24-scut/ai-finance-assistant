from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.agentcore import ledger_tools, session, store as agent_store
from backend.ledger2 import service
from backend.main import app
from ledger2_support import make_db


@pytest.fixture
def client(agentcore_paths):
    with closing(make_db(db.DB_PATH)) as conn:
        agent_store.ensure_schema(conn)
    return TestClient(app)


def _run(text):
    chat = session.create()['id']
    return session.begin_run(chat, provider='test', model='mock', system='', tools=[],
                             digest='', snapshot='', text=text)


def _assert_scene(items, tree):
    refs = {}

    def visit(item, parent=None):
        kind = item['type']
        if kind == 'tx':
            ref = item['ref']
            name = item['title'] or item['counterparty'] or item['item'] or f"#{item['tx_id']}"
            note = item['memo']
        else:
            ref = f"g:{item['id']}" if kind == 'group' else item['id']
            name = item['name'] or '未命名净额组'
            note = item['note']
        refs[ref] = {'ref': ref, 'kind': kind, 'name': name, 'parent': parent, 'note': note}
        for child in item.get('children', item.get('members', [])):
            visit(child, ref)

    visit(tree)
    assert all(item == refs[item['ref']] for item in items)


def test_replay_restores_exact_frames_and_stays_read_only(client):
    run_id = _run('把午饭和晚饭收进餐费')['id']

    def run(index, **op):
        ledger_tools.apply(op, call_key=f'{run_id}:10:{index}', run_id=run_id)
        return client.get('/api/ledger2/tree').json()

    initial = client.get('/api/ledger2/tree').json()['tree']
    receipt = ledger_tools.apply({'op': 'create_node', 'name': '餐费', 'items': ['tx:1']},
                                 call_key=f'{run_id}:10:0', run_id=run_id)
    new_id = receipt['created']['node_id']
    created = client.get('/api/ledger2/tree').json()
    moved = run(1, op='move', items=['tx:3'], target=new_id)
    undone = run(2, op='undo')
    redone = run(3, op='redo')
    reply = client.post('/api/ledger2/op', json={'op': {'type': 'move', 'items': ['tx:1'], 'target': 'cat:购物'}})
    assert reply.status_code == 200, reply.text
    live = client.get('/api/ledger2/tree').json()
    selected = live['timeline'][:-1]
    assert [step['kind'] for step in selected] == ['apply', 'apply', 'undo', 'redo']
    assert {step['run_id'] for step in selected} == {run_id}
    assert {step['request'] for step in selected} == {'把午饭和晚饭收进餐费'}
    assert live['timeline'][-1]['run_id'] is None

    ids = [step['id'] for step in selected]
    assert client.post('/api/ledger2/replay', json={'event_ids': [ids[0], ids[2]]}).json()['code'] == 'REPLAY_RANGE'
    whole = client.post('/api/ledger2/replay', json={'event_ids': ids})
    assert whole.status_code == 200, whole.text
    assert set(whole.json()) == {'steps'}
    frames = whole.json()['steps']
    states = [initial, created['tree'], moved['tree'], undone['tree'], redone['tree']]
    for index, frame in enumerate(frames):
        _assert_scene(frame['before'], states[index])
        _assert_scene(frame['after'], states[index + 1])
        assert all(item['ref'] not in {'tx:2', 'tx:4', 'cat:工资'} for item in frame['before'] + frame['after'])
    assert set(frames[0]['changed']) == {new_id, 'tx:1'}
    assert frames[1]['changed'] == ['tx:3']
    assert {item['ref'] for item in frames[1]['after']} == {'tx:3', new_id, 'cat:餐饮'}
    portion = client.post('/api/ledger2/replay', json={'event_ids': ids[1:3]}).json()
    assert portion['steps'] == frames[1:3]
    assert client.get('/api/ledger2/tree').json() == live


def test_existing_ledger_history_recovers_ai_request_and_undone_branch(client):
    run_id = _run('把第二笔餐饮放到购物')['id']
    initial = client.get('/api/ledger2/tree').json()['tree']
    ledger_tools.apply({'op': 'move', 'items': ['tx:3'], 'target': 'cat:购物'}, call_key=f'{run_id}:10:0')
    moved = client.get('/api/ledger2/tree').json()['tree']
    with closing(db.get_conn()) as conn:
        service.undo(conn)
        conn.execute('DROP TABLE ledger2_replay_event')
        conn.execute("DELETE FROM ledger2_meta WHERE key='replay_seeded'")
        conn.commit()

    migrated = client.get('/api/ledger2/tree').json()['timeline']
    assert [step['kind'] for step in migrated] == ['apply', 'undo']
    assert migrated[0]['run_id'] == run_id and migrated[0]['request'] == '把第二笔餐饮放到购物'
    ids = [step['id'] for step in migrated]
    result = client.post('/api/ledger2/replay', json={'event_ids': ids})
    assert result.status_code == 200, result.text
    move, undo = result.json()['steps']
    _assert_scene(move['before'], initial)
    _assert_scene(move['after'], moved)
    assert (undo['before'], undo['after']) == (move['after'], move['before'])


def test_replay_rename_and_dissolve_include_only_relevant_nodes(client):
    with closing(db.get_conn()) as conn:
        new_id = service.apply(conn, {'type': 'create_node', 'name': '临时组', 'items': ['tx:1']})['created']['node_id']
        service.apply(conn, {'type': 'rename_node', 'node_id': new_id, 'name': '小食'})
        service.apply(conn, {'type': 'dissolve_node', 'node_id': new_id})
    recent = client.get('/api/ledger2/tree').json()['timeline'][-2:]
    result = client.post('/api/ledger2/replay', json={'event_ids': [event['id'] for event in recent]})
    assert result.status_code == 200, result.text
    renamed, dissolved = result.json()['steps']
    assert renamed['changed'] == [new_id]
    assert renamed['before'][0]['name'] == '临时组' and renamed['after'][0]['name'] == '小食'
    assert set(dissolved['changed']) == {new_id, 'tx:1'}
    assert {item['ref'] for item in dissolved['after']} == {'tx:1', 'cat:餐饮', 'root'}


def test_replay_moving_an_entire_net_group_keeps_its_members_collapsed(client):
    with closing(db.get_conn()) as conn:
        group_id = service.apply(conn, {'type': 'create_group', 'name': '净额组', 'items': ['tx:1', 'tx:3']})['created']['group_id']
        service.apply(conn, {'type': 'move', 'items': [f'g:{group_id}'], 'target': 'cat:购物'})
    recent = client.get('/api/ledger2/tree').json()['timeline'][-1]
    result = client.post('/api/ledger2/replay', json={'event_ids': [recent['id']]})
    assert result.status_code == 200, result.text
    step, = result.json()['steps']
    assert step['changed'] == [f'g:{group_id}']
    assert {item['ref'] for item in step['after']} == {f'g:{group_id}', 'cat:购物', 'root'}
