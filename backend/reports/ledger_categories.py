"""账本树分类口径。

报告默认按「账本代数 V2」的节点路径归类，而不是平台分类：

    根 → 大类 → 小类（更深层用 " / " 串起来）

只有已初始化（``ledger2_tx.role='primary' AND initialized=1``）的流水才在账本树里；
其余流水退回平台分类，保证金额不会因为尚未初始化而消失。
"""
from __future__ import annotations

import sqlite3

from ..schemas import CategoryResp, MajorGroup, MinorGroup
from ._common import pct, round2

_TX_ID_CAP = 200
_ROOT = '(SELECT id FROM ledger2_node WHERE parent_id IS NULL LIMIT 1)'


def ledger_ready(conn: sqlite3.Connection) -> bool:
    for table in ('ledger2_node', 'ledger2_tx'):
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone() is None:
            return False
    return True


def _path_resolver(conn: sqlite3.Connection):
    rows = conn.execute('SELECT id, parent_id, name FROM ledger2_node').fetchall()
    nodes = {r['id']: (r['parent_id'], r['name']) for r in rows}
    roots = {r['id'] for r in rows if r['parent_id'] is None}
    cache: dict[str, list[str]] = {}

    def path(node_id) -> list[str]:
        if node_id in cache:
            return cache[node_id]
        names: list[str] = []
        seen: set[str] = set()
        current = node_id
        while current and current not in seen:
            seen.add(current)
            if current in roots:
                break
            node = nodes.get(current)
            if node is None:
                names = []
                break
            parent, name = node
            names.append(name)
            current = parent
        names.reverse()
        cache[node_id] = names
        return names

    return path


def _placement(row, path_of) -> tuple[str, str, bool]:
    names = path_of(row['node_id']) if row['node_id'] else []
    if names:
        return names[0], ' / '.join(names[1:]), True
    return (row['category'] or '未分类'), (row['subcat'] or ''), False


def category_breakdown(conn: sqlite3.Connection, clause: str, params: dict, total: float) -> CategoryResp:
    """按账本树汇总支出分类；未初始化的流水按平台分类兜底。"""
    path_of = _path_resolver(conn)
    rows = conn.execute(
        'SELECT c.id, c.amount, c.category, c.subcat, m.node_id '
        'FROM v_tx_counted c '
        "LEFT JOIN ledger2_tx m ON m.tx_id = c.id AND m.role = 'primary' AND m.initialized = 1 "
        f'WHERE c.direction = :direction AND {clause}',
        params,
    ).fetchall()

    majors: dict[str, dict] = {}
    for row in rows:
        major_name, minor_name, from_ledger = _placement(row, path_of)
        major = majors.setdefault(
            major_name, {'amount': 0.0, 'count': 0, 'minors': {}, 'ledger': from_ledger}
        )
        major['amount'] += float(row['amount'] or 0)
        major['count'] += 1
        if not minor_name:
            continue
        minor = major['minors'].setdefault(minor_name, {'amount': 0.0, 'count': 0, 'tx_ids': []})
        minor['amount'] += float(row['amount'] or 0)
        minor['count'] += 1
        if len(minor['tx_ids']) < _TX_ID_CAP:
            minor['tx_ids'].append(int(row['id']))

    major_items = sorted(majors.items(), key=lambda item: item[1]['amount'], reverse=True)
    result: list[MajorGroup] = []
    for name, data in major_items:
        minor_items = sorted(data['minors'].items(), key=lambda item: item[1]['amount'], reverse=True)
        result.append(MajorGroup(
            major=name,
            amount=round2(data['amount']),
            pct=pct(data['amount'], total),
            count=data['count'],
            ledger=data['ledger'],
            minors=[
                MinorGroup(
                    minor=minor_name,
                    amount=round2(minor['amount']),
                    pct=pct(minor['amount'], total),
                    count=minor['count'],
                    tx_ids=minor['tx_ids'],
                )
                for minor_name, minor in minor_items
            ],
        ))
    return CategoryResp(total=total, majors=result)


def placement_clause(category: str, subcat: str | None) -> tuple[str, dict]:
    """给明细列表用的账本树筛选。

    ``category`` 是大类节点名；``subcat`` 是分类报告里的 ``minor`` 文本，
    也就是**相对大类**的路径（不含大类名），与 ``category_breakdown`` 的
    ``' / '.join(names[1:])`` 对齐。
    """
    sql = f"""
        EXISTS (
            WITH RECURSIVE under(id, path) AS (
                SELECT n.id, ''
                  FROM ledger2_node n
                 WHERE n.parent_id = {_ROOT}
                   AND n.name = :ledger_category
                UNION ALL
                SELECT n.id,
                       CASE WHEN under.path = '' THEN n.name
                            ELSE under.path || ' / ' || n.name END
                  FROM ledger2_node n JOIN under ON n.parent_id = under.id
            )
            SELECT 1
              FROM ledger2_tx m
             WHERE m.tx_id = v_tx.id
               AND m.role = 'primary'
               AND m.initialized = 1
               AND m.node_id IN (
                   SELECT id FROM under
                    WHERE :ledger_subcat IS NULL OR path = :ledger_subcat
               )
        )
    """
    return sql, {'ledger_category': category, 'ledger_subcat': subcat or None}
