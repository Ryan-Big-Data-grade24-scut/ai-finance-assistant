"""账本代数 V2 原型。

对照文档：docs/设计/第二轮/账本需求原文.md（N1–N21）
数据结构：docs/设计/第二轮/05_账本数据结构.md

这是一个**独立原型**：自己的 schema、自己的连接、自己的测试。
不 import 也不改动 backend/ledger 与 db/schema.sql。
"""
from .errors import LedgerError
from .algebra import atom, mirror, plus1, net
from .model import Tx, NetGroup, Node, Scope, TxRef
from .store import connect, init_db, seed_transactions
from .reader import read_ledger, read_scope, scope_by_date, render, to_json
from .ops import (
    create_node, move, dissolve_node,
    create_net_group, add_group_member, remove_group_member,
    split_tx, unsplit_tx,
    undo, redo, history,
)

__all__ = [
    'LedgerError', 'atom', 'mirror', 'plus1', 'net',
    'Tx', 'NetGroup', 'Node', 'Scope', 'TxRef',
    'connect', 'init_db', 'seed_transactions',
    'read_ledger', 'read_scope', 'scope_by_date', 'render', 'to_json',
    'create_node', 'move', 'dissolve_node',
    'create_net_group', 'add_group_member', 'remove_group_member',
    'split_tx', 'unsplit_tx', 'undo', 'redo', 'history',
]
