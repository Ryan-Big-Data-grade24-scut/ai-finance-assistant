"""旧版待确认记录：保留手工确认入口；新版直写不在这里生成待确认卡。"""
from __future__ import annotations

import json
from contextlib import closing

from backend.ledger2 import facts, reader, service
from backend.ledger2 import store as ledger_store
from backend.ledger2.errors import LedgerError
from backend.ledger2.tx import preview

from .store import new_id, open_conn

TX_REF_PREFIXES = ('tx:', 'txm:')


class ActionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _tx_refs(op: dict) -> list[tuple[int, str]]:
    raw: list = []
    for key in ('tx_ids', 'tx_id', 'tx', 'item'):
        value = op.get(key)
        if value is None:
            continue
        raw.extend(value if isinstance(value, list) else [value])
    for item in op.get('items') or []:
        raw.append(item)
    refs: set[tuple[int, str]] = set()
    for value in raw:
        if isinstance(value, int):
            refs.add((value, 'primary'))
            continue
        text = str(value)
        if text.startswith('tx:'):
            refs.add((int(text[3:]), 'primary'))
        elif text.startswith('txm:'):
            refs.add((int(text[4:]), 'mirror'))
    return sorted(refs)


def _node_ids(op: dict) -> list[str]:
    found: set[str] = set()
    for value in [op.get('target'), op.get('node_id')]:
        if isinstance(value, str) and value:
            found.add(value)
    for value in op.get('items') or []:
        text = str(value)
        if not text.startswith(TX_REF_PREFIXES) and not text.startswith('g:'):
            found.add(text)
    return sorted(found)


def _group_ids(op: dict) -> list[str]:
    found: set[str] = set()
    value = op.get('group_id')
    if isinstance(value, str) and value:
        found.add(value)
    for item in op.get('items') or []:
        text = str(item)
        if text.startswith('g:'):
            found.add(text[2:])
    return sorted(found)


def fingerprint(conn, op: dict) -> dict:
    """这个操作触及到的对象的当前状态。预览时定格，确认时重算。"""
    fp: dict = {'op': op.get('type')}
    tx_state = {}
    for tx_id, role in _tx_refs(op):
        row = conn.execute(
            'SELECT node_id, net_group_id, initialized FROM ledger2_tx WHERE tx_id=? AND role=?',
            (tx_id, role),
        ).fetchone()
        tx_state[f'{tx_id}:{role}'] = dict(row) if row else None
    fp['tx'] = tx_state

    node_state = {}
    for node_id in _node_ids(op):
        row = conn.execute(
            'SELECT id, parent_id, name FROM ledger2_node WHERE id=?', (node_id,)
        ).fetchone()
        node_state[node_id] = dict(row) if row else None
    fp['node'] = node_state

    group_state = {}
    for group_id in _group_ids(op):
        row = conn.execute(
            'SELECT id, node_id, name FROM ledger2_net_group WHERE id=?', (group_id,)
        ).fetchone()
        group_state[group_id] = dict(row) if row else None
    fp['group'] = group_state

    if op.get('type') in {'undo', 'redo'}:
        last_applied = conn.execute(
            "SELECT MAX(seq) FROM ledger2_op_log WHERE state='applied'"
        ).fetchone()[0]
        last_undone = conn.execute(
            "SELECT MIN(seq) FROM ledger2_op_log WHERE state='undone'"
        ).fetchone()[0]
        fp['log'] = {'applied': last_applied, 'undone': last_undone}
    return fp


def _flatten(tree: dict) -> dict:
    """树 JSON → {'node:id': …, 'group:id': …, 'tx:id:role': …}，用于算差异。"""
    out: dict = {}

    def walk(item, parent_name: str, parent_kind: str):
        kind = item.get('type')
        if kind == 'node':
            out[f"node:{item['id']}"] = {
                'name': item.get('name'), 'kind': item.get('kind'),
                'parent': parent_name, 'parent_kind': parent_kind,
                'e': item.get('e'), 'i': item.get('i'),
            }
            for child in item.get('children') or []:
                walk(child, item.get('name') or item['id'], 'node')
        elif kind == 'group':
            out[f"group:{item['id']}"] = {
                'name': item.get('name'), 'node_id': item.get('node_id'),
                'e': item.get('e'), 'i': item.get('i'),
                'members': [f"tx:{m['tx_id']}:{m['role']}" for m in item.get('members') or []],
            }
            for member in item.get('members') or []:
                walk(member, item.get('name') or item['id'], 'group')
        elif kind == 'tx':
            key = f"tx:{item['tx_id']}:{item['role']}"
            out[key] = {
                'parent': parent_name, 'parent_kind': parent_kind,
                'e': item.get('e'), 'i': item.get('i'),
                'direction': item.get('kind'),
            }

    walk(tree, '', 'root')
    return out


# 一次初始化可能新建几千个节点。影响清单按条数封顶：**计数永远是全量**，
# 明细只给前若干条。既不撑爆上下文，也不让模型把"只看得到 40 条"当成"只影响 40 个"。
DIFF_ITEM_LIMIT = 40


def _diff(before: dict, after: dict) -> dict:
    b, a = _flatten(before), _flatten(after)
    created = sorted(k for k in a if k not in b)
    removed = sorted(k for k in b if k not in a)
    moved = sorted(
        k for k in a if k in b and (
            a[k].get('parent') != b[k].get('parent')
            or a[k].get('node_id') != b[k].get('node_id')
        )
    )
    renamed = sorted(
        k for k in a if k in b and a[k].get('name') != b[k].get('name')
    )
    counts = {'created': len(created), 'removed': len(removed),
              'moved': len(moved), 'renamed': len(renamed)}
    out = {
        'created': [{'ref': k, **{x: a[k].get(x) for x in ('name', 'kind', 'parent')}}
                    for k in created[:DIFF_ITEM_LIMIT]],
        'removed': [{'ref': k, **{x: b[k].get(x) for x in ('name', 'kind', 'parent')}}
                    for k in removed[:DIFF_ITEM_LIMIT]],
        'moved': [{'ref': k, 'from': b[k].get('parent'), 'to': a[k].get('parent')}
                  for k in moved[:DIFF_ITEM_LIMIT]],
        'renamed': [{'ref': k, 'from': b[k].get('name'), 'to': a[k].get('name')}
                    for k in renamed[:DIFF_ITEM_LIMIT]],
        'counts': counts,
    }
    if any(value > DIFF_ITEM_LIMIT for value in counts.values()):
        out['note'] = (f'明细最多列 {DIFF_ITEM_LIMIT} 条；counts 里是全量计数，'
                       '不要拿明细条数当影响总数。')
    return out


def run_op(conn, op: dict) -> dict:
    kind = op.get('type')
    if kind == 'undo':
        return service.undo(conn)
    if kind == 'redo':
        return service.redo(conn)
    return service.apply(conn, op)


def propose(session_id: str | None, op: dict, *, title: str = '') -> dict:
    """在可回滚事务里真的执行一次，把影响定格，然后回滚。"""
    with closing(open_conn()) as conn:
        ledger_store.ensure_schema(conn)
        try:
            with preview(conn):
                before = reader.to_json(reader.read_ledger(conn))
                fp = fingerprint(conn, op)
                result = run_op(conn, op)
                after = reader.to_json(reader.read_ledger(conn))
        except LedgerError as exc:
            raise ActionError(exc.code, exc.message) from exc

        diff = _diff(before, after)
        action_id = new_id('act')
        conn.execute(
            'INSERT INTO agentcore_action(id, session_id, kind, title, op_json, preview_json, state) '
            "VALUES(?,?,?,?,?,?,'pending')",
            (action_id, session_id, op.get('type') or 'op', title or op.get('type') or '操作',
             json.dumps(op, ensure_ascii=False), json.dumps(
                 {'result': result, 'diff': diff, 'fingerprint': fp}, ensure_ascii=False)),
        )
        conn.commit()
        return {'action_id': action_id, 'state': 'pending', 'preview': diff, 'result': result}


def confirm(action_id: str, session_id: str | None = None) -> dict:
    """用户在页面上确认。单次使用；前置状态变了就拒绝，不猜测目标。"""
    with closing(open_conn()) as conn:
        ledger_store.ensure_schema(conn)
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute(
                'SELECT * FROM agentcore_action WHERE id=?', (action_id,)
            ).fetchone()
            if row is None:
                raise ActionError('ACTION_NOT_FOUND', '这个待确认操作不存在。')
            if row['state'] != 'pending':
                raise ActionError('ACTION_NOT_PENDING', f'这个操作已经处理过（{row["state"]}）。')
            if session_id and row['session_id'] and row['session_id'] != session_id:
                raise ActionError('ACTION_SESSION_MISMATCH', '这个操作不属于当前会话。')
            op = json.loads(row['op_json'])
            stored_fp = json.loads(row['preview_json']).get('fingerprint')
            current_fp = fingerprint(conn, op)
            # 兼容旧记录：没有指纹时只做状态检查，不声称更强的一致性。
            if stored_fp is not None and stored_fp != current_fp:
                raise ActionError(
                    'STALE_ACTION',
                    '账本在你确认之前变过，这个预览已经不准确。请重新生成一次操作。',
                )
            result = run_op(conn, op)
            conn.execute(
                "UPDATE agentcore_action SET state='confirmed', result_json=?, "
                "decided_at=datetime('now') WHERE id=?",
                (json.dumps(result, ensure_ascii=False), action_id),
            )
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        return {'action_id': action_id, 'state': 'confirmed', 'result': result}


def reject(action_id: str, session_id: str | None = None) -> dict:
    with closing(open_conn()) as conn:
        ledger_store.ensure_schema(conn)
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute(
                'SELECT * FROM agentcore_action WHERE id=?', (action_id,)
            ).fetchone()
            if row is None:
                raise ActionError('ACTION_NOT_FOUND', '这个待确认操作不存在。')
            if row['state'] != 'pending':
                raise ActionError('ACTION_NOT_PENDING', f'这个操作已经处理过（{row["state"]}）。')
            if session_id and row['session_id'] and row['session_id'] != session_id:
                raise ActionError('ACTION_SESSION_MISMATCH', '这个操作不属于当前会话。')
            conn.execute(
                "UPDATE agentcore_action SET state='rejected', decided_at=datetime('now') WHERE id=?",
                (action_id,),
            )
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        return {'action_id': action_id, 'state': 'rejected'}


def confirmed_classification_examples(conn) -> list[dict]:
    """历史手工确认的目标仍与当前归属相同，才算确认范例。"""
    rows = conn.execute(
        "SELECT id, kind, op_json, result_json, decided_at FROM agentcore_action "
        "WHERE state='confirmed' AND kind IN ('init','move','create_node') "
        "ORDER BY decided_at DESC, id"
    ).fetchall()
    examples = []
    for row in rows:
        op = json.loads(row['op_json'])
        created = json.loads(row['result_json'] or '{}').get('created', {})
        for tx_id, role in _tx_refs(op):
            if role != 'primary':
                continue
            target = _confirmed_target(op, created, tx_id)
            if target is None:
                continue
            current = conn.execute(
                'SELECT m.node_id, n.name FROM ledger2_tx m JOIN ledger2_node n ON n.id=m.node_id '
                "WHERE m.tx_id=? AND m.role='primary' AND m.initialized=1", (tx_id,),
            ).fetchone()
            if current is None or current['node_id'] != target:
                continue
            try:
                category = facts.category_of(conn, tx_id)
            except LedgerError:
                continue
            examples.append({
                'action_id': row['id'], 'kind': row['kind'], 'at': row['decided_at'],
                'source': 'user_confirmed', 'tx_id': tx_id, 'category': category,
                'node_id': target, 'node_name': current['name'],
            })
    return examples


def _confirmed_target(op: dict, created: dict, tx_id: int) -> str | None:
    if op.get('target'):
        return op['target']
    if op['type'] == 'create_node':
        return created.get('node_id')
    if op['type'] != 'init':
        return None
    nodes = created.get('nodes', {})
    if op.get('new_node'):
        return nodes.get('new')
    if op.get('style') == 'platform':
        # 旧预设回执没有逐笔目标，不能用现在的分类倒推当时的放置。
        return None
    for placement in (op.get('plan') or {}).get('place') or []:
        if tx_id in [int(t) for t in placement['txs']]:
            node = placement['node']
            return nodes.get(node, node)
    return 'root'

