"""模型的只读 SQL 在脱敏后的流水快照上运行。"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from backend import db
from backend.ai import privacy

from .truncate import json_text

VIEW = 'agentcore_tx'
TABLES = ('transactions_raw', VIEW)
MAX_ROWS = 500
MAX_BYTES = 60_000
# 每次查询先把业务表复制进内存快照，再执行模型 SQL；真实账本几千行时 3 秒会把
# 正常的聚合查询也判成超时。上限只防跑飞，不限制查询复杂度；可用环境变量调整。
MAX_SECONDS = max(1.0, float(os.getenv('AGENTCORE_SQL_SECONDS', '30')))


class SqlRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _authorizer(action, arg1, arg2, _dbname, _trigger):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and str(arg1).lower() in TABLES:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() != 'load_extension':
        return sqlite3.SQLITE_OK   # 标准 SQLite 函数；连接不注册扩展或自定义函数
    return sqlite3.SQLITE_DENY


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _copy(source, dest, name: str, policy: privacy.Policy) -> dict:
    # 固定业务表名，列名和类型来自 schema：新增原始字段不需要维护第二份 DDL。
    cols = source.execute(f'PRAGMA table_info({name})').fetchall()
    if not cols:
        raise SqlRejected('NOT_READY', f'{name} 尚未初始化。')
    int_columns = {'amount_cents', 'initialized'}
    declarations = []
    for _, column, declared, *_ in cols:
        kind = 'INTEGER' if column in int_columns else declared
        declarations.append(f'{_quoted(column)} {_quoted(kind)}' if kind else _quoted(column))
    dest.execute(f'CREATE TABLE {name} ({", ".join(declarations)})')
    marks = ','.join('?' for _ in cols)

    def private_rows():
        for row in source.execute(f'SELECT * FROM {name}'):
            yield tuple(policy.redact(value, data_keys=column == 'raw_json') if isinstance(value, str) else value
                        for value, (_, column, *_) in zip(row, cols))

    dest.executemany(f'INSERT INTO {name} VALUES ({marks})', private_rows())
    start, end, count = dest.execute(f'SELECT MIN(date), MAX(date), COUNT(*) FROM {name}').fetchone()
    return {'date_from': start, 'date_to': end, 'total': count}


def _first_keyword(sql: str) -> str:
    match = re.match(r'(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*([a-z]+)\b', sql, re.I | re.S)
    return match[1].lower() if match else ''


def _json_value(value):
    if isinstance(value, bytes):
        return {'blob_hex': value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def query(sql: str, *, db_path: Path | None = None) -> dict:
    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejected('EMPTY_SQL', 'SQL 不能为空。')
    if _first_keyword(sql) not in {'select', 'with'}:
        raise SqlRejected('ONLY_SELECT', '只允许 SELECT / WITH 查询。')

    path = Path(db_path) if db_path else db.DB_PATH
    started = time.monotonic()
    deadline = started + MAX_SECONDS

    def expired():
        return int(time.monotonic() > deadline)

    rows = []
    truncated = False
    try:
        with closing(sqlite3.connect(':memory:')) as mem:
            mem.set_progress_handler(expired, 1000)
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                         timeout=MAX_SECONDS)) as source:
                source.set_progress_handler(expired, 1000)
                source.execute('BEGIN')  # 原始表和分析表来自同一次读事务
                policy = privacy.current(source)
                coverage = {name: _copy(source, mem, name, policy) for name in TABLES}

            mem.set_authorizer(_authorizer)
            # 防止单个表达式分配远超一页的内存；这不是表/列/分析函数权限。
            mem.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4_000_000)
            cur = mem.execute(sql)
            columns = [d[0] for d in cur.description]
            size = len(json_text(columns).encode())
            if size > MAX_BYTES - 1_024:
                raise SqlRejected('RESULT_TOO_LARGE', '列名过长，请用较短的列名。')
            for row in cur:
                if expired():
                    raise SqlRejected('TIMEOUT', f'查询超过 {MAX_SECONDS:g} 秒被中止。')
                values = [_json_value(v) for v in row]
                size += len(json_text(values).encode()) + 1
                if len(rows) >= MAX_ROWS or size > MAX_BYTES - 1_024:
                    if not rows:
                        raise SqlRejected('ROW_TOO_LARGE', '单行超出返回预算；请选择所需列，或用 substr 分段读。')
                    truncated = True
                    break
                rows.append(values)
    except sqlite3.DatabaseError as exc:
        message = str(exc)
        if 'not authorized' in message or 'prohibited' in message or 'table-valued' in message:
            raise SqlRejected('NOT_AUTHORIZED', f'只允许读取 {TABLES[0]} 和 {VIEW}。') from exc
        if 'interrupted' in message or expired():
            raise SqlRejected('TIMEOUT', f'查询超过 {MAX_SECONDS:g} 秒被中止。') from exc
        if 'unable to open database file' in message or any(f'no such table: main.{t}' in message for t in TABLES):
            raise SqlRejected('NOT_READY', '流水数据库尚未初始化。') from exc
        raise SqlRejected('SQL_ERROR', f'SQL 无法执行：{message}') from exc

    result = {
        'columns': columns, 'rows': rows, 'row_count': len(rows), 'truncated': truncated,
        'coverage': coverage, 'elapsed_ms': int((time.monotonic() - started) * 1000),
    }
    if truncated:
        result['truncated_note'] = '只返回前面的完整结果行；用聚合或 ORDER BY 配合 LIMIT/OFFSET 继续。'
    return result
