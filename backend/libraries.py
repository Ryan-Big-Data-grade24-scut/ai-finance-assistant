"""账本库（多个流水库并存）。

一个流水库 = 一个独立 SQLite 文件，文件里就是一本账。
本模块只维护「有哪些流水库、当前打开哪一本」的登记册，真正的流水仍在各自的库里。

登记册默认位于 ``db/libraries.registry.db``；它的路径只取决于启动时的数据库：
- 正式库 ``db/bills.db`` → ``db/libraries.registry.db``，重启后保留当前账本；
- 测试库 / 临时库 → 与测试库同目录的 ``<stem>.libraries.registry.db``，
  避免测试把正式账本切换走。

切换账本会改变进程级的 ``db.DB_PATH``。这是本地单用户应用的刻意取舍：
所有既有接口都通过 ``db.get_conn()`` 取连接，无需逐个改签名。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend import db

router = APIRouter(prefix='/api/libraries', tags=['libraries'])
log = logging.getLogger(__name__)

_ACTIVE_KEY = 'active_library_id'
_STARTUP_PATH_KEY = 'startup_library_path'
_REGISTRY_ENV = 'LIBRARY_REGISTRY_DB'
_MANAGED_FILENAME = re.compile(r'library-[0-9a-f]{12}\.db')
_lock = threading.RLock()
_mutation_lock = asyncio.Lock()
_registry_path: Path | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    path       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_at TEXT
);
CREATE TABLE IF NOT EXISTS library_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class LibraryCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1, max_length=40)


class _Unreadable(Exception):
    pass


def _is_default_install() -> bool:
    try:
        return (Path(db.DB_PATH).resolve() == (Path(db.DB_DIR) / 'bills.db').resolve())
    except OSError:
        return False


def _registry_file() -> Path:
    """登记册位置在第一次使用时钉死，之后切换账本不会把它带走。"""
    global _registry_path
    if _registry_path is not None:
        return _registry_path
    env = os.environ.get(_REGISTRY_ENV)
    if env:
        _registry_path = Path(env).expanduser().resolve()
    elif _is_default_install():
        _registry_path = Path(db.DB_DIR).resolve() / 'libraries.registry.db'
    else:
        current = Path(db.DB_PATH).expanduser().resolve()
        _registry_path = current.with_name(f'{current.stem}.libraries.registry.db')
    return _registry_path


def _authoritative() -> bool:
    """正式安装或显式声明时，重启后沿用登记册里的当前账本；测试库永远以进程库为准。"""
    return _is_default_install() or os.environ.get('LIBRARY_REGISTRY_AUTHORITATIVE') == '1'


def _connect() -> sqlite3.Connection:
    path = _registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript(_SCHEMA)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if not any(row['name'] == 'deleted_at' for row in conn.execute('PRAGMA table_info(libraries)')):
                conn.execute('ALTER TABLE libraries ADD COLUMN deleted_at TEXT')
    except Exception:
        conn.close()
        raise
    return conn


def _same_path(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _startup_path(conn: sqlite3.Connection) -> str:
    row = conn.execute('SELECT value FROM library_meta WHERE key = ?', (_STARTUP_PATH_KEY,)).fetchone()
    if row is not None:
        return row['value']
    path = str(Path(db.DB_PATH).expanduser().resolve())
    conn.execute('INSERT INTO library_meta(key, value) VALUES(?, ?)', (_STARTUP_PATH_KEY, path))
    return path


def _is_managed(row: sqlite3.Row, startup_path: str) -> bool:
    path = Path(row['path'])
    registry = _registry_file()
    return (
        _MANAGED_FILENAME.fullmatch(path.name) is not None
        and _same_path(path.parent, registry.parent)
        and not _same_path(path, registry)
        and not _same_path(path, startup_path)
        and not _same_path(path, Path(db.DB_DIR) / 'bills.db')
    )


def _default_name() -> str:
    return '我的账本'


def _unique_name(conn: sqlite3.Connection, wanted: str) -> str:
    base = wanted.strip() or _default_name()
    names = {str(row['name']).casefold() for row in conn.execute(
        'SELECT name FROM libraries WHERE deleted_at IS NULL'
    )}
    if base.casefold() not in names:
        return base
    index = 2
    while f'{base} {index}'.casefold() in names:
        index += 1
    return f'{base} {index}'


def _register_current(conn: sqlite3.Connection) -> sqlite3.Row:
    current = str(Path(db.DB_PATH).expanduser().resolve())
    row = conn.execute('SELECT * FROM libraries WHERE path = ?', (current,)).fetchone()
    if row is not None:
        return row
    return conn.execute(
        'INSERT INTO libraries(id, name, path) VALUES(?, ?, ?) RETURNING *',
        (f'lib_{uuid.uuid4().hex[:12]}', _unique_name(conn, _default_name()), current),
    ).fetchone()


def _active_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        'SELECT l.* FROM library_meta m JOIN libraries l ON l.id = m.value '
        'WHERE m.key = ? AND l.deleted_at IS NULL', (_ACTIVE_KEY,)
    ).fetchone()


def _visible_row(conn: sqlite3.Connection, library_id: str) -> sqlite3.Row:
    row = conn.execute(
        'SELECT * FROM libraries WHERE id = ? AND deleted_at IS NULL', (library_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, '账本不存在。')
    return row


def _visible_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        'SELECT * FROM libraries WHERE deleted_at IS NULL ORDER BY created_at, id'
    ).fetchall()


def _set_active(conn: sqlite3.Connection, library_id: str) -> None:
    conn.execute(
        'INSERT INTO library_meta(key, value) VALUES(?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (_ACTIVE_KEY, library_id),
    )


def bootstrap() -> Path:
    """登记当前库，并按规则恢复上次打开的账本。返回最终生效的数据库路径。"""
    with _lock, closing(_connect()) as conn:
        with conn:
            _startup_path(conn)
            current = _register_current(conn)
            saved = _active_row(conn)
            if current['deleted_at'] is not None:
                # 即使启动路径指向归档文件，也不能把它重新登记为当前账本。
                target = saved if saved is not None and _is_readable(saved) else next(
                    (row for row in _visible_rows(conn) if _is_readable(row)), None,
                )
                if target is None:
                    raise RuntimeError('找不到可用的账本，无法恢复当前账本。')
            elif _authoritative() and saved is not None:
                target = saved
            else:
                target = current
            if saved is None or target['id'] != saved['id']:
                _set_active(conn, target['id'])
        db.DB_PATH = Path(target['path'])
        return db.DB_PATH


def _open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise _Unreadable
    conn = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone() is not None


def stats_for(path: Path) -> dict:
    """不切换当前库，只读统计一个流水库。读不到时返回可识别的空状态。"""
    empty = {
        'transactions': 0, 'initialized': 0, 'uninitialized': 0,
        'batches': 0, 'date_from': None, 'date_to': None,
        'last_import': None, 'readable': False,
    }
    try:
        conn = _open_readonly(path)
    except (OSError, sqlite3.Error, _Unreadable):
        return empty
    try:
        if not _table_exists(conn, 'transactions_raw'):
            empty['readable'] = True
            return empty
        live = (
            'NOT EXISTS (SELECT 1 FROM tx_deletions d WHERE d.tx_id = t.id)'
            if _table_exists(conn, 'tx_deletions') else '1=1'
        )
        transactions = int(conn.execute(
            f'SELECT COUNT(*) AS n FROM transactions_raw t WHERE {live}'
        ).fetchone()['n'])
        bounds = conn.execute(
            f'SELECT MIN(t.date) AS a, MAX(t.date) AS b FROM transactions_raw t WHERE {live}'
        ).fetchone()
        initialized = 0
        if _table_exists(conn, 'ledger2_tx'):
            initialized = int(conn.execute(
                'SELECT COUNT(*) AS n FROM ledger2_tx m '
                'JOIN transactions_raw t ON t.id = m.tx_id '
                f"WHERE m.role = 'primary' AND m.initialized = 1 AND {live}"
            ).fetchone()['n'])
        batches = 0
        last_import = None
        if _table_exists(conn, 'import_batches'):
            batch = conn.execute(
                'SELECT COUNT(*) AS n, MAX(created_at) AS last FROM import_batches'
            ).fetchone()
            batches = int(batch['n'])
            last_import = batch['last']
        return {
            'transactions': transactions,
            'initialized': initialized,
            'uninitialized': max(0, transactions - initialized),
            'batches': batches,
            'date_from': bounds['a'],
            'date_to': bounds['b'],
            'last_import': last_import,
            'readable': True,
        }
    except sqlite3.Error:
        return empty
    finally:
        conn.close()


def _is_readable(row: sqlite3.Row) -> bool:
    try:
        with closing(_open_readonly(Path(row['path']))) as conn:
            return _table_exists(conn, 'transactions_raw')
    except (OSError, sqlite3.Error, _Unreadable):
        return False


def _summary(row: sqlite3.Row, saved: sqlite3.Row | None, startup_path: str) -> dict:
    path = Path(row['path'])
    return {
        'id': row['id'],
        'name': row['name'],
        'active': _same_path(path, db.DB_PATH),
        'saved_active': saved is not None and saved['id'] == row['id'],
        'can_delete': _is_managed(row, startup_path),
        'created_at': row['created_at'],
        'stats': stats_for(path),
    }


def _snapshot(conn: sqlite3.Connection) -> dict:
    saved = _active_row(conn)
    startup_path = _startup_path(conn)
    items = [_summary(row, saved, startup_path) for row in _visible_rows(conn)]
    active = next((item for item in items if item['active']), None)
    return {'libraries': items, 'active': active}


def list_libraries() -> dict:
    with _lock:
        bootstrap()
        with closing(_connect()) as conn:
            return _snapshot(conn)


def _prepare(path: Path) -> None:
    """把一本库准备到可服务状态，不提前暴露给其他正在处理的请求。"""
    db.init_db(path)
    from backend.ledger import service as ledger_service
    from backend.ledger2 import store as ledger2_store
    from backend.agentcore import store as agentcore_store

    ledger_service.sync_live_book(path)
    with closing(db.get_conn(path)) as conn:
        ledger2_store.ensure_schema(conn)
        agentcore_store.ensure_schema(conn)


def _activate_row(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    target = Path(row['path']).expanduser().resolve()
    if not _same_path(target, db.DB_PATH):
        _prepare(target)
    with conn:
        _set_active(conn, row['id'])
    db.DB_PATH = target
    return _summary(row, row, _startup_path(conn))


def create_library(name: str) -> dict:
    wanted = name.strip()
    if not wanted:
        raise HTTPException(400, '账本名称不能为空。')
    if len(wanted) > 40:
        raise HTTPException(400, '账本名称最多 40 个字符。')
    with _lock:
        bootstrap()
        with closing(_connect()) as conn:
            existing = conn.execute('SELECT name FROM libraries WHERE deleted_at IS NULL').fetchall()
            if any(str(row['name']).casefold() == wanted.casefold() for row in existing):
                raise HTTPException(409, '已存在同名账本，请换一个名字。')
            path = _registry_file().parent / f'library-{uuid.uuid4().hex[:12]}.db'
            with conn:
                row = conn.execute(
                    'INSERT INTO libraries(id, name, path) VALUES(?, ?, ?) '
                    'RETURNING id, name, path, created_at',
                    (f'lib_{uuid.uuid4().hex[:12]}', wanted, str(path)),
                ).fetchone()
            try:
                return _activate_row(conn, row)
            except Exception as exc:
                with conn:
                    conn.execute('DELETE FROM libraries WHERE id = ?', (row['id'],))
                path.unlink(missing_ok=True)
                raise HTTPException(500, f'新建账本失败：{exc}') from exc


def select_library(library_id: str) -> dict:
    with _lock, closing(_connect()) as conn:
        row = _visible_row(conn, library_id)
        try:
            return _activate_row(conn, row)
        except Exception as exc:
            raise HTTPException(500, f'切换账本失败：{exc}') from exc


def _validate_delete(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    if not _is_managed(row, _startup_path(conn)):
        raise HTTPException(409, '初始账本不能删除。')
    if conn.execute(
        'SELECT 1 FROM libraries WHERE deleted_at IS NULL AND id != ? LIMIT 1', (row['id'],)
    ).fetchone() is None:
        raise HTTPException(409, '至少保留一本账本。')


def _delete_library(library_id: str) -> dict:
    with _lock, closing(_connect()) as conn:
        row = _visible_row(conn, library_id)
        _validate_delete(conn, row)
        remaining = [item for item in _visible_rows(conn) if item['id'] != library_id]
        target_path = None
        with conn:
            if _same_path(row['path'], db.DB_PATH):
                startup_path = _startup_path(conn)
                remaining.sort(key=lambda item: not _same_path(item['path'], startup_path))
                for candidate in remaining:
                    if not _is_readable(candidate):
                        continue
                    path = Path(candidate['path'])
                    try:
                        _prepare(path)
                    except Exception:
                        log.warning('Could not prepare fallback library %s', candidate['id'], exc_info=True)
                        continue
                    _set_active(conn, candidate['id'])
                    target_path = path
                    break
                if target_path is None:
                    raise HTTPException(409, '没有其他可打开的账本，不能删除当前账本。')
            else:
                # 修正可能与进程库不一致的上次选择，不改变当前库。
                saved = _active_row(conn)
                if saved is not None and saved['id'] == library_id:
                    current = next(item for item in remaining if _same_path(item['path'], db.DB_PATH))
                    _set_active(conn, current['id'])
            conn.execute("UPDATE libraries SET deleted_at = datetime('now') WHERE id = ?", (library_id,))
        if target_path is not None:
            db.DB_PATH = target_path
        return _snapshot(conn)


def _will_switch(library_id: str, *, deleting: bool = False) -> bool:
    with _lock, closing(_connect()) as conn:
        row = _visible_row(conn, library_id)
        if deleting:
            _validate_delete(conn, row)
        is_current = _same_path(row['path'], db.DB_PATH)
        return is_current if deleting else not is_current


def _recover_atlas() -> None:
    from backend.agentcore import runtime
    try:
        runtime.recover_interrupted()
    except Exception:
        log.warning('Could not recover interrupted ATLAS runs after switching libraries', exc_info=True)


@router.get('')
def get_libraries():
    return list_libraries()


@router.get('/active')
def get_active_library():
    data = list_libraries()
    active = data['active']
    if active is None:
        raise HTTPException(404, '还没有可用的账本。')
    return active


@router.post('')
async def post_library(body: LibraryCreate):
    from backend.agentcore import runtime
    async with _mutation_lock, runtime.pause_for_library_switch():
        result = create_library(body.name)
        _recover_atlas()
        return result


@router.post('/{library_id}/select')
async def post_select(library_id: str):
    from backend.agentcore import runtime
    async with _mutation_lock:
        if not _will_switch(library_id):
            return select_library(library_id)
        async with runtime.pause_for_library_switch():
            result = select_library(library_id)
            _recover_atlas()
            return result


@router.delete('/{library_id}')
async def delete_library(library_id: str):
    from backend.agentcore import runtime
    async with _mutation_lock:
        if not _will_switch(library_id, deleting=True):
            return _delete_library(library_id)
        async with runtime.pause_for_library_switch():
            result = _delete_library(library_id)
            _recover_atlas()
            return result
