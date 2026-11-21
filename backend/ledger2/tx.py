"""事务边界：同一段写逻辑既能在「自己提交」下跑，也能参加外层的预览事务。

为什么需要它：AgentCore 的「预览 → 用户确认」要求先在可回滚的事务里真的跑一遍
V2 的业务操作，读出它造成的影响，然后整体回滚。但 ops 里的 `with conn:` 会当场
提交，把预览变成真写。

规则：
- 连接还没有事务时 → 自己 BEGIN IMMEDIATE / COMMIT，保持原来的原子行为；
- 连接已有外层事务时 → 用 SAVEPOINT，外层回滚就能把这一段一起撤掉。

只换事务归属，不改任何业务语义；`/v2` 的普通调用走第一条分支，行为不变。
"""
from __future__ import annotations

import itertools
from contextlib import contextmanager

_SAVEPOINT_SEQ = itertools.count(1)


@contextmanager
def atomic(conn):
    if conn.in_transaction:
        name = f'agentcore_sp_{next(_SAVEPOINT_SEQ)}'
        conn.execute(f'SAVEPOINT {name}')
        try:
            yield
        except BaseException:
            conn.execute(f'ROLLBACK TO {name}')
            conn.execute(f'RELEASE {name}')
            raise
        else:
            conn.execute(f'RELEASE {name}')
        return

    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def preview(conn):
    """在可回滚事务里执行一段真实业务操作，退出时一定回滚。

    外面用 `with preview(conn):` 包住 service.apply，就是「真的跑过一遍」，
    不是另写一套估算逻辑 —— 预览和提交走的是同一段代数。
    """
    if conn.in_transaction:
        name = f'agentcore_sp_{next(_SAVEPOINT_SEQ)}'
        conn.execute(f'SAVEPOINT {name}')
        try:
            yield
        finally:
            conn.execute(f'ROLLBACK TO {name}')
            conn.execute(f'RELEASE {name}')
        return

    conn.execute('BEGIN IMMEDIATE')
    try:
        yield
    finally:
        conn.rollback()
