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

import os
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend import db

router = APIRouter(prefix='/api/libraries', tags=['libraries'])

_ACTIVE_KEY = 'active_library_id'
_REGISTRY_ENV = 'LIBRARY_REGISTRY_DB'
_lock = threading.RLock()
_registry_path: Path | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    path       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
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
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _same_path(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _default_name() -> str:
    return '我的账本'


def _unique_name(conn: sqlite3.Connection, wanted: str) -> str:
    base = wanted.strip() or _default_name()
    names = {str(row['name']).casefold() for row in conn.execute('SELECT name FROM libraries')}
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
    row = conn.execute(
        'INSERT INTO libraries(id, name, path) VALUES(?, ?, ?) '
        'RETURNING id, name, path, created_at',
        (f'lib_{uuid.uuid4().hex[:12]}', _unique_name(conn, _default_name()), current),
    ).fetchone()
    conn.commit()
    return row


def _active_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        'SELECT l.* FROM library_meta m JOIN libraries l ON l.id = m.value '
        'WHERE m.key = ?', (_ACTIVE_KEY,)
    ).fetchone()


def _set_active(conn: sqlite3.Connection, library_id: str) -> None:
    conn.execute(
        'INSERT INTO library_meta(key, value) VALUES(?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (_ACTIVE_KEY, library_id),
    )
    conn.commit()


def bootstrap() -> Path:
    """登记当前库，并按规则恢复上次打开的账本。返回最终生效的数据库路径。"""
    with _lock:
        conn = _connect()
        try:
            current = _register_current(conn)
            saved = _active_row(conn)
            if _authoritative() and saved is not None:
                db.DB_PATH = Path(saved['path'])
            elif saved is None or not _same_path(saved['path'], db.DB_PATH):
                _set_active(conn, current['id'])
        finally:
            conn.close()
    return Path(db.DB_PATH)


def _open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise _Unreadable
    conn = sqlite3.connect(path, timeout=1)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA busy_timeout=1000')
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


def _summary(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    path = Path(row['path'])
    saved_active = _active_row(conn)
    return {
        'id': row['id'],
        'name': row['name'],
        'active': _same_path(row['path'], db.DB_PATH),
        'saved_active': saved_active is not None and saved_active['id'] == row['id'],
        'created_at': row['created_at'],
        'stats': stats_for(path),
    }


def list_libraries() -> dict:
    bootstrap()
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute('SELECT * FROM libraries ORDER BY created_at, id').fetchall()
            items = [_summary(conn, row) for row in rows]
        finally:
            conn.close()
    active = next((item for item in items if item['active']), None)
    return {'libraries': items, 'active': active}


def _prepare(path: Path) -> None:
    """把一本库准备到可服务状态（建表、V1 投影、V2/ATLAS 表）。"""
    db.init_db(path)
    db.DB_PATH = path
    from backend.ledger import service as ledger_service
    from backend.ledger2 import store as ledger2_store
    from backend.agentcore import store as agentcore_store

    ledger_service.sync_live_book()
    with closing(db.get_conn()) as conn:
        ledger2_store.ensure_schema(conn)
        agentcore_store.ensure_schema(conn)


def _activate_row(row: sqlite3.Row) -> dict:
    target = Path(row['path']).expanduser().resolve()
    old = Path(db.DB_PATH).expanduser().resolve()
    if not _same_path(target, old):
        try:
            _prepare(target)
        except Exception:
            db.DB_PATH = old
            raise
    conn = _connect()
    try:
        _set_active(conn, row['id'])
        fresh = conn.execute('SELECT * FROM libraries WHERE id = ?', (row['id'],)).fetchone()
        return _summary(conn, fresh)
    finally:
        conn.close()


def create_library(name: str) -> dict:
    wanted = name.strip()
    if not wanted:
        raise HTTPException(400, '账本名称不能为空。')
    if len(wanted) > 40:
        raise HTTPException(400, '账本名称最多 40 个字符。')
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute('SELECT name FROM libraries').fetchall()
            if any(str(row['name']).casefold() == wanted.casefold() for row in existing):
                raise HTTPException(409, '已存在同名账本，请换一个名字。')
            path = _registry_file().parent / f'library-{uuid.uuid4().hex[:12]}.db'
            row = conn.execute(
                'INSERT INTO libraries(id, name, path) VALUES(?, ?, ?) '
                'RETURNING id, name, path, created_at',
                (f'lib_{uuid.uuid4().hex[:12]}', wanted, str(path)),
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
        try:
            return _activate_row(row)
        except HTTPException:
            raise
        except Exception as exc:
            with _lock:
                conn = _connect()
                try:
                    conn.execute('DELETE FROM libraries WHERE id = ?', (row['id'],))
                    conn.commit()
                finally:
                    conn.close()
            Path(row['path']).unlink(missing_ok=True)
            raise HTTPException(500, f'新建账本失败：{exc}') from exc


def select_library(library_id: str) -> dict:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute('SELECT * FROM libraries WHERE id = ?', (library_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise HTTPException(404, '账本不存在。')
        try:
            return _activate_row(row)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, f'切换账本失败：{exc}') from exc


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
def post_library(body: LibraryCreate):
    return create_library(body.name)


@router.post('/{library_id}/select')
async def post_select(library_id: str):
    # 运行中的 ATLAS 属于旧流水库，切换前先停稳，避免它把后续写入落到新库。
    from backend.agentcore import runtime as agentcore_runtime
    await agentcore_runtime.shutdown()
    result = select_library(library_id)
    try:
        agentcore_runtime.recover_interrupted()
    except Exception:
        pass
    return result
