"""AgentCore 的本地存储：会话日志、执行回执、旧卡、导入暂存、记忆目录。

这些表只由 Web / MCP 启动过程建，不修改 V1 的账单表。
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from backend import db

# 所有 AgentCore 的落盘物（会话表在业务库里，记忆与暂存在这里）都在这个目录下。
# 测试用 AGENTCORE_HOME 指到临时目录，绝不碰真实数据。
def home_dir() -> Path:
    override = os.environ.get('AGENTCORE_HOME')
    base = Path(override) if override else db.DB_DIR / 'agentcore'
    base.mkdir(parents=True, exist_ok=True)
    return base


def memory_dir() -> Path:
    path = home_dir() / 'memory'
    path.mkdir(parents=True, exist_ok=True)
    return path


def stage_dir() -> Path:
    path = home_dir() / 'stage'
    path.mkdir(parents=True, exist_ok=True)
    return path


SCHEMA = """
CREATE TABLE IF NOT EXISTS agentcore_session (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS agentcore_message (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES agentcore_session(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_agentcore_message_session
    ON agentcore_message(session_id, id);
CREATE TABLE IF NOT EXISTS agentcore_run (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES agentcore_session(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,
    model       TEXT NOT NULL,
    state       TEXT NOT NULL CHECK (state IN ('running','stopping','completed','failed','stopped','interrupted')),
    start_id    INTEGER NOT NULL,
    end_id      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agentcore_run_session ON agentcore_run(session_id, start_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agentcore_run_active
    ON agentcore_run(session_id) WHERE end_id IS NULL;
CREATE TABLE IF NOT EXISTS agentcore_action (
    id          TEXT PRIMARY KEY,
    session_id  TEXT,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    op_json     TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending','confirmed','rejected','expired')),
    result_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_agentcore_action_session
    ON agentcore_action(session_id, state, created_at);
CREATE TABLE IF NOT EXISTS agentcore_import_stage (
    id          TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    platform_name TEXT NOT NULL DEFAULT '',
    filename    TEXT NOT NULL DEFAULT '',
    total       INTEGER NOT NULL DEFAULT 0,
    date_from   TEXT,
    date_to     TEXT,
    summary_json TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'staged'
                CHECK (state IN ('staged','committed','cancelled')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS agentcore_ledger_receipt (
    call_key    TEXT PRIMARY KEY,
    args_json   TEXT NOT NULL,
    result_json TEXT NOT NULL
);
CREATE VIEW agentcore_tx AS
SELECT
    t.id                              AS tx_id,
    t.date                            AS date,
    t.month                           AS month,
    t.direction                       AS direction,
    CAST(ROUND(t.amount * 100) AS INTEGER) AS amount_cents,
    t.source                          AS source,
    t.platform_id                     AS platform_id,
    COALESCE(NULLIF(TRIM(o.manual_category), ''), NULLIF(TRIM(t.tx_type), ''), '其他') AS category,
    t.item                            AS item,
    t.counterparty                    AS counterparty,
    t.status                          AS status,
    CASE WHEN EXISTS (
        SELECT 1 FROM ledger2_tx m
        WHERE m.tx_id = t.id AND m.role = 'primary' AND m.initialized = 1
    ) THEN 1 ELSE 0 END               AS initialized
FROM transactions_raw t
LEFT JOIN tx_overrides o ON o.tx_id = t.id
WHERE NOT EXISTS (SELECT 1 FROM tx_deletions d WHERE d.tx_id = t.id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    # 原型库可能有旧分析口径。替换本视图时不让并发读者看到它暂时消失。
    conn.executescript('BEGIN IMMEDIATE;\nDROP VIEW IF EXISTS agentcore_tx;\n' + SCHEMA + '\nCOMMIT;')


def open_conn() -> sqlite3.Connection:
    return db.get_conn()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def health() -> dict:
    with closing(open_conn()) as conn:
        sessions = conn.execute('SELECT COUNT(*) FROM agentcore_session').fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM agentcore_action WHERE state='pending'"
        ).fetchone()[0]
    return {'sessions': sessions, 'pending_actions': pending}
