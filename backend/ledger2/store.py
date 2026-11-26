"""连接与建表。V2 的表不进 db/schema.sql，由 ensure_schema 幂等创建。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .errors import LedgerError
from .tx import atomic

SCHEMA_PATH = Path(__file__).with_name('schema.sql')
SCHEMA_VERSION = '1'
ROOT_ID = 'root'
ROOT_NAME = '总账'


def _seed_replay(conn) -> None:
    """旧库按仍可重建的撤销分支补历史。AI 来源按已提交的工具回执恢复。"""
    marker = "SELECT 1 FROM ledger2_meta WHERE key='replay_seeded'"
    if conn.execute(marker).fetchone():
        return
    with atomic(conn):
        if conn.execute(marker).fetchone():
            return
        origins = {}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='agentcore_ledger_receipt'").fetchone():
            for row in conn.execute(
                "SELECT call_key, json_extract(result_json, '$.seq') AS seq FROM agentcore_ledger_receipt "
                "WHERE json_extract(args_json, '$.op') NOT IN ('undo', 'redo')"
            ):
                run_id, separator, _ = row['call_key'].partition(':')
                if separator and run_id.startswith('run_'):
                    origins[row['seq']] = run_id
        rows = conn.execute(
            "SELECT seq, state, created_at FROM ledger2_op_log WHERE state!='discarded' ORDER BY seq"
        ).fetchall()
        insert = 'INSERT INTO ledger2_replay_event(op_seq, kind, run_id, created_at) VALUES(?,?,?,?)'
        conn.executemany(insert, ((r['seq'], 'apply', origins.get(r['seq']), r['created_at']) for r in rows))
        conn.executemany(insert, ((r['seq'], 'undo', None, r['created_at']) for r in reversed(rows) if r['state'] == 'undone'))
        conn.execute("INSERT INTO ledger2_meta(key, value) VALUES('replay_seeded', '1')")


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA busy_timeout = 5000')
    return conn


def ensure_schema(conn) -> None:
    """建表 + 建根节点 + 补列。幂等。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding='utf-8'))
    # 旧库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加列。
    for table in ('ledger2_node', 'ledger2_net_group'):
        cols = {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
        if 'note' not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "INSERT OR IGNORE INTO ledger2_node(id, parent_id, name) VALUES(?, NULL, ?)",
        (ROOT_ID, ROOT_NAME),
    )
    conn.execute("INSERT OR IGNORE INTO ledger2_meta(key, value) VALUES('schema_version', ?)", (SCHEMA_VERSION,))
    _seed_replay(conn)
    conn.commit()


def root_id(conn) -> str:
    row = conn.execute('SELECT id FROM ledger2_node WHERE parent_id IS NULL').fetchone()
    if row is None:
        raise LedgerError('NO_ROOT', '这个账本没有根节点。')
    return row['id']
