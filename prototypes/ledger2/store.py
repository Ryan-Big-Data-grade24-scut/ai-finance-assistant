"""连接与建库。

原型自带一份最小版 transactions_raw，正式环境沿用既有表。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .errors import LedgerError

SCHEMA_PATH = Path(__file__).with_name('schema.sql')


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA busy_timeout = 5000')
    return conn


def init_db(path) -> sqlite3.Connection:
    """建库 + 建表 + 建默认根节点 M_r。"""
    conn = connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding='utf-8'))
    conn.execute("INSERT OR IGNORE INTO ledger_node(id, parent_id, name) VALUES('root', NULL, '总账')")
    conn.commit()
    return conn


def seed_transactions(conn, rows: list[dict]) -> None:
    """灌入虚构交易。rows 里 amount 单位是分。"""
    for row in rows:
        conn.execute(
            'INSERT INTO transactions_raw(id, tx_time, date, direction, amount, category, counterparty, item, source) '
            'VALUES(:id,:tx_time,:date,:direction,:amount,:category,:counterparty,:item,:source)',
            dict(row),
        )
    conn.commit()


def root_id(conn) -> str:
    row = conn.execute('SELECT id FROM ledger_node WHERE parent_id IS NULL').fetchone()
    if row is None:
        raise LedgerError('NO_ROOT', '这个账本没有根节点 M_r。')
    return row['id']
