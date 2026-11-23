"""账本删除契约：登记册、账本文件和 ATLAS 生命周期均在隔离环境验证。"""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from backend import db, libraries
from backend.agentcore import runtime
from ledger2_support import DATA, make_db

BASE = '/api/libraries'


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    # 特意让启动库也像一个新建库，验证不能仅靠文件名就允许删除。
    original = tmp_path / 'library-000000000000.db'
    make_db(original, DATA[:1]).close()
    registry_path = tmp_path / 'legacy.registry.db'
    with closing(sqlite3.connect(registry_path)) as conn, conn:
        conn.executescript('''
            CREATE TABLE libraries (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE library_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        conn.execute('INSERT INTO libraries(id, name, path) VALUES (?, ?, ?)', ('initial', '我的账本', str(original)))
        conn.execute('INSERT INTO library_meta VALUES (?, ?)', ('active_library_id', 'initial'))
    monkeypatch.setattr(db, 'DB_DIR', tmp_path)
    monkeypatch.setattr(db, 'DB_PATH', original)
    monkeypatch.setattr(libraries, '_registry_path', None)
    monkeypatch.setattr(libraries, '_mutation_lock', asyncio.Lock())
    monkeypatch.setenv('LIBRARY_REGISTRY_DB', str(registry_path))
    monkeypatch.setenv('LIBRARY_REGISTRY_AUTHORITATIVE', '1')

    calls = []

    async def stop():
        calls.append(('stop', db.DB_PATH))

    def recover():
        calls.append(('recover', db.DB_PATH))

    monkeypatch.setattr(runtime, 'shutdown', stop)
    monkeypatch.setattr(runtime, 'recover_interrupted', recover)
    app = FastAPI()
    app.include_router(libraries.router)
    return app, original, registry_path, calls


def _create(client, name='临时账本'):
    response = client.post(BASE, json={'name': name})
    assert response.status_code == 200, response.text
    item = response.json()
    assert item['active'] and item['can_delete']
    return item['id'], db.DB_PATH


def _stored_state(registry_path, library_id):
    with closing(sqlite3.connect(registry_path)) as conn:
        return conn.execute('''
            SELECT deleted_at, (SELECT value FROM library_meta WHERE key = 'active_library_id')
            FROM libraries WHERE id = ?
        ''', (library_id,)).fetchone()


def test_registers_a_fresh_install_and_protects_its_initial_ledger(registry, monkeypatch):
    app, original, registry_path, calls = registry
    monkeypatch.setenv('LIBRARY_REGISTRY_DB', str(registry_path.with_name('new.registry.db')))
    with TestClient(app) as client:
        state = client.get(BASE).json()
        initial = state['active']
        assert initial and state['libraries'] == [initial]
        assert not initial['can_delete']
        assert db.DB_PATH == original
        assert client.delete(f"{BASE}/{initial['id']}").status_code == 409
    assert calls == []


def test_legacy_registry_migrates_and_keeps_original_protected(registry):
    app, original, registry_path, calls = registry
    earlier = original.with_name('library-0123456789ab.db')
    make_db(earlier, []).close()
    with closing(sqlite3.connect(registry_path)) as conn, conn:
        conn.execute('INSERT INTO libraries(id, name, path) VALUES (?, ?, ?)', ('earlier', '旧自建账本', str(earlier)))
    with TestClient(app) as client:
        data = client.get(BASE).json()
        assert data['active']['id'] == 'initial'
        assert {item['id']: item['can_delete'] for item in data['libraries']} == {'initial': False, 'earlier': True}
        assert client.delete(f'{BASE}/initial').status_code == 409
        assert client.delete(f'{BASE}/unknown').status_code == 404
        assert client.delete(f'{BASE}/earlier').status_code == 200
        assert client.delete(f'{BASE}/earlier').status_code == 404
        assert client.post(f'{BASE}/earlier/select').status_code == 404
    assert _stored_state(registry_path, 'initial') == (None, 'initial')
    assert _stored_state(registry_path, 'earlier')[0]
    assert earlier.is_file()
    assert db.DB_PATH == original
    assert calls == []


def test_delete_current_switches_to_original_and_preserves_both_databases(registry, monkeypatch):
    app, original, registry_path, calls = registry
    with TestClient(app) as client:
        other, _ = _create(client, '另一账本')
        library_id, archived = _create(client)
        make_db(archived, DATA[1:3], init=False).close()
        assert client.get(f'{BASE}/active').json()['stats']['transactions'] == 2
        calls.clear()

        response = client.delete(f'{BASE}/{library_id}')
        assert response.status_code == 200, response.text
        state = response.json()
        assert {item['id'] for item in state['libraries']} == {'initial', other}
        assert state['active']['id'] == 'initial'
        assert state['active']['stats']['transactions'] == 1
        assert calls == [('stop', archived), ('recover', original)]
        assert db.DB_PATH == original
        assert _stored_state(registry_path, library_id)[0]
        assert _stored_state(registry_path, library_id)[1] == 'initial'
        with closing(sqlite3.connect(f'{archived.as_uri()}?mode=ro', uri=True)) as conn:
            assert conn.execute('SELECT id, amount FROM transactions_raw ORDER BY id').fetchall() == [(2, 8000), (3, 20)]

        monkeypatch.setattr(libraries, '_registry_path', None)
        monkeypatch.setattr(db, 'DB_PATH', archived)
        assert libraries.bootstrap() == original
        assert client.post(f'{BASE}/{library_id}/select').status_code == 404
        recreated_id, new_path = _create(client)
        assert recreated_id != library_id and new_path != archived
        assert client.get(f'{BASE}/active').json()['stats']['transactions'] == 0


def test_unavailable_original_falls_back_to_remaining_created_ledger(registry):
    app, original, _, calls = registry
    with TestClient(app) as client:
        other, fallback = _create(client, '可用的另一本账本')
        current, current_path = _create(client, '当前账本')
        original.rename(original.with_suffix('.offline'))
        calls.clear()
        response = client.delete(f'{BASE}/{current}')
        assert response.status_code == 200, response.text
        assert response.json()['active']['id'] == other
        assert calls == [('stop', current_path), ('recover', fallback)]
        assert not original.exists()


@pytest.mark.parametrize('fallback_problem', ['missing', 'prepare', 'commit'])
def test_delete_failure_preserves_current_and_registration(registry, monkeypatch, fallback_problem):
    app, original, registry_path, calls = registry
    with TestClient(app, raise_server_exceptions=False) as client:
        current, current_path = _create(client)
        calls.clear()

        if fallback_problem == 'missing':
            original.rename(original.with_suffix('.offline'))
        elif fallback_problem == 'prepare':
            def fail_prepare(path):
                assert path == original and db.DB_PATH == current_path
                raise sqlite3.OperationalError('模拟账本无法准备')
            monkeypatch.setattr(libraries, '_prepare', fail_prepare)
        else:
            with closing(sqlite3.connect(registry_path)) as conn, conn:
                conn.execute('''CREATE TRIGGER block_delete BEFORE UPDATE OF deleted_at ON libraries
                    BEGIN SELECT RAISE(ABORT, '模拟登记册写入失败'); END''')

        expected_status = 500 if fallback_problem == 'commit' else 409
        assert client.delete(f'{BASE}/{current}').status_code == expected_status
        assert client.get(f'{BASE}/active').json()['id'] == current
        assert _stored_state(registry_path, current) == (None, current)
        assert calls == [('stop', current_path)]
        assert not runtime._switching_library


def test_delete_last_visible_ledger_is_rejected(registry):
    app, _, registry_path, calls = registry
    with TestClient(app) as client:
        current, current_path = _create(client)
        with closing(sqlite3.connect(registry_path)) as conn, conn:
            conn.execute("DELETE FROM libraries WHERE id = 'initial'")
        calls.clear()
        response = client.delete(f'{BASE}/{current}')
        assert response.status_code == 409
        assert response.json()['detail'] == '至少保留一本账本。'
        assert _stored_state(registry_path, current) == (None, current)
        assert db.DB_PATH == current_path
        assert calls == []


def test_delete_serializes_switches_and_blocks_new_atlas_turns(registry, monkeypatch):
    app, original, _, _ = registry

    async def exercise():
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            current = (await client.post(BASE, json={'name': '并行示例'})).json()['id']
            reached_stop = asyncio.Event()
            release_stop = asyncio.Event()

            async def stop():
                reached_stop.set()
                await release_stop.wait()

            monkeypatch.setattr(runtime, 'shutdown', stop)
            deleting = asyncio.create_task(client.delete(f'{BASE}/{current}'))
            await asyncio.wait_for(reached_stop.wait(), 2)
            selecting = asyncio.create_task(client.post(f'{BASE}/{current}/select'))
            await asyncio.sleep(0)
            try:
                assert not selecting.done()
                with pytest.raises(runtime.AgentcoreError) as caught:
                    await runtime.start_turn('test', '不会访问模型')
                assert caught.value.code == 'LIBRARY_SWITCHING'
            finally:
                release_stop.set()
            assert (await deleting).status_code == 200
            assert (await selecting).status_code == 404
            assert db.DB_PATH == original

    asyncio.run(exercise())
