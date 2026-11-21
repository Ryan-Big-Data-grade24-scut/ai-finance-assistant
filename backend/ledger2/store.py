"""连接与建表。V2 的表不进 db/schema.sql，由 ensure_schema 幂等创建。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .errors import LedgerError

SCHEMA_PATH = Path(__file__).with_name('schema.sql')
SCHEMA_VERSION = '1'
ROOT_ID = 'root'
ROOT_NAME = '总账'


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
    conn.commit()


def root_id(conn) -> str:
    row = conn.execute('SELECT id FROM ledger2_node WHERE parent_id IS NULL').fetchone()
    if row is None:
        raise LedgerError('NO_ROOT', '这个账本没有根节点。')
    return row['id']
