"""账本 V2 测试的公共件：在临时路径上建一个**真 schema** 的库，灌 7 笔虚构交易。

绝不碰 db/bills.db：路径永远由调用方（tmp_path）给出。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from backend import db
from backend.ledger2 import facts, presets, store
from backend.ledger2.model import NetGroup, Node

PLATFORM_ID = 'testpay'
PLATFORM_NAME = '测试支付'

# 金额是元；tx_type 就是分类。原型里的「退款」分类改成「其他收入」，
# 否则 V1 的退款分析会把它当退款流水归零。
DATA = [
    dict(id=1, tx_time='2026-01-05 10:00:00', direction='支出', amount=30, tx_type='餐饮', counterparty='甲', item='午饭'),
    dict(id=2, tx_time='2026-01-08 09:00:00', direction='收入', amount=8000, tx_type='工资', counterparty='乙', item='月薪'),
    dict(id=3, tx_time='2026-01-09 19:00:00', direction='支出', amount=20, tx_type='餐饮', counterparty='甲', item='晚饭'),
    dict(id=4, tx_time='2026-02-02 12:00:00', direction='支出', amount=25, tx_type='餐饮', counterparty='甲', item='外卖'),
    dict(id=5, tx_time='2026-02-03 08:00:00', direction='支出', amount=100, tx_type='交通', counterparty='丙', item='月票'),
    dict(id=6, tx_time='2026-01-20 21:00:00', direction='收入', amount=50, tx_type='其他收入', counterparty='丁', item='红包'),
    dict(id=8, tx_time='2026-01-21 11:00:00', direction='支出', amount=50, tx_type='购物', counterparty='戊', item='日用品'),
]

ROOT_VALUE = (22500, 805000)     # 整数分：支出 225.00 / 收入 8050.00


def make_db(path, rows=DATA, init=True):
    """建库（真 schema）→ 插交易 → 建 ledger2 表 →（默认）按平台分类初始化 → 清空操作日志。返回 ledger2 连接。

    init=False：交易全部保持「未初始化」，用来测初始化本身 / 未初始化被过滤。
    初始化这一步不算「用户的操作」，所以日志清空：撤销栈从干净状态开始。
    """
    path = Path(path)
    db.init_db(path)
    conn = store.connect(path)
    conn.execute(
        'INSERT OR IGNORE INTO platforms(id, name, kind, adapter) VALUES(?,?,?,?)',
        (PLATFORM_ID, PLATFORM_NAME, 'wallet', 'generic_csv'),
    )
    for row in rows:
        add_tx(conn, **row)
    conn.commit()
    store.ensure_schema(conn)
    if init and rows:
        presets.init_by_platform_category(conn)
        reset_log(conn)
    return conn


def add_tx(conn, *, id=None, tx_time, direction, amount, tx_type=None, counterparty=None, item=None,
           status=None, tx_no=None, merchant_no=None, remark=None, source=PLATFORM_NAME, raw=None):
    """插一笔 transactions_raw。amount 是元；返回 id。"""
    dt = datetime.strptime(tx_time, '%Y-%m-%d %H:%M:%S')
    key = f'ledger2-test-{id if id is not None else tx_time}-{counterparty}-{item}-{amount}-{tx_no}'
    cur = conn.execute(
        'INSERT INTO transactions_raw(id, source, platform_id, tx_time, direction, amount, counterparty, item, '
        'tx_type, status, tx_no, merchant_no, remark, date, month, year, weekday, hour, dedupe_key, raw_json) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (id, source, PLATFORM_ID, tx_time, direction, amount, counterparty, item, tx_type, status, tx_no,
         merchant_no, remark, dt.strftime('%Y-%m-%d'), dt.strftime('%Y-%m'), dt.strftime('%Y'),
         dt.weekday(), dt.hour, key, json.dumps(raw or {}, ensure_ascii=False)),
    )
    return cur.lastrowid


def snapshot(conn) -> dict:
    """全部 ledger2 业务表的逐行快照 —— 撤销必须逐行还原，不是「看起来差不多」。"""
    out = {}
    for table in ('ledger2_node', 'ledger2_tx', 'ledger2_net_group', 'ledger2_split_tx'):
        rows = conn.execute(f'SELECT * FROM {table}').fetchall()
        out[table] = sorted(tuple(dict(r).values()) for r in rows)
    return out


def table_count(conn, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def cat(node: Node, name: str) -> Node:
    """某个分类节点（按 id `cat:<name>` 找，不看名字——名字可能和用户建的 M 重名）。"""
    for c in node.children:
        if isinstance(c, Node) and c.id == f'cat:{name}':
            return c
    raise AssertionError(f'没有分类节点 {name}；实际：{[getattr(x, "id", x) for x in node.children]}')


def child(node: Node, name: str) -> Node:
    """按名字找用户建的 M（不含分类节点）。"""
    for c in node.children:
        if isinstance(c, Node) and c.name == name and c.kind != 'category':
            return c
    raise AssertionError(f'没有找到节点 {name}；实际子项：{[getattr(x, "name", x) for x in node.children]}')


def group(node: Node, name: str) -> NetGroup:
    for c in node.children:
        if isinstance(c, NetGroup) and c.name == name:
            return c
    raise AssertionError(f'没有找到组 {name}')


def tx_ids(node: Node, role: str | None = None) -> list[int]:
    from backend.ledger2.model import Tx
    return sorted(c.id for c in node.children if isinstance(c, Tx) and (role is None or c.role == role))


def reset_log(conn):
    """清空操作日志并让 seq 从头开始（AUTOINCREMENT 的计数器也要清）。"""
    conn.execute('DELETE FROM ledger2_op_log')
    conn.execute("DELETE FROM sqlite_sequence WHERE name='ledger2_op_log'")
    conn.commit()


def init_all(conn):
    """把现在还没初始化的交易按平台分类全部初始化，并清空日志（测试里 add_tx 之后用）。"""
    conn.commit()
    if facts.uninitialized_ids(conn):
        presets.init_by_platform_category(conn)
    reset_log(conn)
    conn.commit()
