"""门面：HTTP / MCP / 测试都只经过这里。一个函数 = 一次完整的读或写，自己不持有连接。

读：`get_tree` 全量读或按日期范围做最小覆盖投影，金额是整数分。
写：`apply` 把一个 op 字典分发给 ops.py；`undo` / `redo` 走原语日志。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from . import facts, invariants, ops, presets, reader
from .errors import LedgerError
from .model import GroupRef, TxRef
from .store import ensure_schema, root_id
from .tx import atomic


def ensure(conn) -> None:
    ensure_schema(conn)


def _scope(conn, date_from: str | None, date_to: str | None):
    """读时作用域：日期区间。"""
    if not date_from and not date_to:
        return None
    if bool(date_from) != bool(date_to):
        raise LedgerError('BAD_RANGE', '日期范围要同时给起止两个日期。')
    if date_from > date_to:
        raise LedgerError('BAD_RANGE', '起始日期不能晚于结束日期。')
    return facts.scope_by_date(conn, date_from, date_to)


def status(conn) -> dict:
    hist = ops.history(conn)
    applied = [h for h in hist if h['state'] == 'applied']
    undone = [h for h in hist if h['state'] == 'undone']
    return {
        'seq': applied[-1]['seq'] if applied else 0,
        'can_undo': bool(applied),
        'can_redo': bool(undone),
        'history': [h for h in hist if h['state'] != 'discarded'][-50:],
        'warnings': invariants.violations(conn),
    }


def get_tree(conn, date_from: str | None = None, date_to: str | None = None) -> dict:
    root = reader.read_ledger(conn)
    total = reader._count(root)
    scope = _scope(conn, date_from, date_to)
    if scope is not None:
        root = reader.read_scope(root, scope)
    return {
        'tree': reader.to_json(root),
        'scope': {'date_from': date_from, 'date_to': date_to,
                  'tx_in_scope': reader._count(root), 'tx_total': total},
        'uninit': len(facts.uninitialized_ids(conn)),
        'available': facts.date_bounds(conn),
        'status': status(conn),
    }


def list_uninitialized(conn, limit: int = 500) -> dict:
    """初始化弹层的候选：全部未初始化 id + 前 N 笔的展示字段。

    `ids` 完整返回，前端才能「全选/反选」后精确传 `tx_ids`；`items` 只用于列表展示。
    """
    ids = facts.uninitialized_ids(conn)
    items = facts.uninitialized_preview(conn, limit)
    return {'total': len(ids), 'ids': ids, 'items': items, 'truncated': len(ids) > len(items)}


def _need(op: dict, key: str):
    if key not in op or op[key] is None:
        raise LedgerError('BAD_OP', f'操作缺少字段 {key}。')
    return op[key]


def _split(conn, tx_id: int) -> list[int]:
    tx = facts.load_txs(conn).get(int(tx_id))
    if tx is None:
        raise LedgerError('TX_NOT_FOUND', f'交易 {tx_id} 不存在，或已被删除。')
    return ops.split_tx(conn, tx_id)


def _apply_init(conn, op: dict) -> dict:
    """初始化：`style` 走预设（预设只拼 op），`plan` 走自定义，`target` / `new_node`
    走「指定放置」；一次只能给一种。

    `tx_ids` **缺省**（键不存在）= 全部「存活但还没初始化」的交易；显式空数组是
    错误 —— 否则模型传一个空选择就会静默变成全量初始化。
    """
    raw_ids = op.get('tx_ids')
    if raw_ids is None:
        tx_ids = facts.uninitialized_ids(conn)
    else:
        tx_ids = [int(t) for t in raw_ids]
    if not tx_ids:
        raise LedgerError('EMPTY_SELECTION', '没有要初始化的交易。')
    style = op.get('style')
    plan = op.get('plan')
    target = op.get('target')
    new_node = op.get('new_node')
    if sum(bool(x) for x in (style, plan, target, new_node)) > 1:
        raise LedgerError('BAD_OP', 'style / plan / target / new_node 只能给一个。')
    if style == 'platform':
        plan = presets.platform_category_op(conn, tx_ids)
    elif style:
        raise LedgerError('BAD_OP', f'未知的初始化风格 {style}。')
    elif target or new_node:
        plan = presets.placement_op(conn, tx_ids, target=target, new_node=new_node)
    return ops.init_txs(conn, tx_ids, plan)


def _apply_update(conn, op: dict) -> None:
    """改名 / 改备注：按选择引用的类型分发（M / +2 / t；t′ 没有人工字段）。"""
    item = ops.parse_item(_need(op, 'item'))
    name = op.get('name')
    note = op.get('note')
    if isinstance(item, GroupRef):
        ops.update_group(conn, item.id, name, note)
    elif isinstance(item, str):
        ops.update_node(conn, item, name, note)
    elif item.role == 'primary':
        # 交易的人工字段叫 title（前端仍叫「命名」）
        ops.update_manual(conn, item.tx_id, name, note)
    else:
        raise LedgerError('MIRROR_HAS_NO_MANUAL', '镜像交易没有人工字段，只能改它所属的原始交易。')


def apply(conn, op: dict) -> dict:
    # 读取原语的旧位置和附加到历史必须处于同一写事务。
    with atomic(conn):
        return _apply(conn, op)


def _apply(conn, op: dict) -> dict:
    """执行一个用户操作。返回 {'created': {...}}（新建对象的 id），树由调用方再读。

    选择语义分三种（见 draft 10）：
    - 最简（move / join / create_*）：`ops.fold_*`，被覆盖的子项不再单独处理；
    - 最繁（split / unsplit）：`ops.expand_selection`，展开出全部可执行对象；
    - 解散（dissolve）：只解散明确选中的 M / +2，不把未选中的子节点一起拆掉。
    """
    kind = _need(op, 'type')
    created: dict = {}
    if kind == 'init':
        created.update(_apply_init(conn, op) or {})
    elif kind == 'uninstall':
        # 卸载：注销 V2 归属与人工字段，原始流水不动。空选择由 ops 拒绝。
        ops.uninst_txs(conn, _need(op, 'tx_ids'))
    elif kind == 'create_node':
        created['node_id'] = ops.create_node(conn, _need(op, 'name'), _need(op, 'items'), op.get('note') or '')
    elif kind == 'move':
        ops.move(conn, _need(op, 'items'), _need(op, 'target'))
    elif kind == 'join':
        txs = ops.fold_for_join(conn, _need(op, 'items'))
        ops.add_group_members(conn, _need(op, 'group_id'), txs, 'join')
    elif kind == 'create_group':
        name = (_need(op, 'name') or '').strip()
        if not name:
            raise LedgerError('EMPTY_NAME', '新建净额组时必须命名。')
        txs = ops.fold_for_join(conn, _need(op, 'items'))
        created['group_id'] = ops.create_net_group(conn, txs, name, op.get('note') or '')
    elif kind == 'dissolve':
        nodes, groups = ops.dissolve_selection(conn, _need(op, 'items'))
        nodes = [n for n in nodes if n != root_id(conn)]    # 根不能解散；选中根时忽略它
        ops.dissolve(conn, nodes, groups)
    elif kind == 'split':
        if 'items' in op:
            _nodes, _groups, txs = ops.expand_selection(conn, op['items'])
            created['split'] = ops.split_txs(conn, ops.split_candidates(conn, txs), 'split')
        else:
            created['split'] = _split(conn, int(_need(op, 'tx_id')))
    elif kind == 'unsplit':
        if 'items' in op:
            _nodes, _groups, txs = ops.expand_selection(conn, op['items'])
            created['unsplit'] = ops.unsplit_txs(conn, ops.split_sources(conn, txs), 'unsplit')
        else:
            created['unsplit'] = ops.unsplit_tx(conn, int(_need(op, 'tx_id')))
    elif kind == 'update':
        _apply_update(conn, op)
    elif kind == 'dissolve_node':
        ops.dissolve_node(conn, _need(op, 'node_id'))
    elif kind == 'rename_node':
        ops.rename_node(conn, _need(op, 'node_id'), _need(op, 'name'))
    elif kind == 'update_node':
        ops.update_node(conn, _need(op, 'node_id'), op.get('name'), op.get('note'))
    elif kind == 'update_manual':
        ops.update_manual(conn, int(_need(op, 'tx_id')), op.get('title'), op.get('note'))
    elif kind == 'add_member':
        ops.add_group_member(conn, _need(op, 'group_id'), _need(op, 'tx'))
    elif kind == 'remove_member':
        ops.remove_group_member(conn, _need(op, 'group_id'), _need(op, 'tx'))
    elif kind == 'rename_group':
        ops.rename_group(conn, _need(op, 'group_id'), _need(op, 'name'))
    elif kind == 'dissolve_group':
        ops.dissolve_group(conn, _need(op, 'group_id'))
    else:
        raise LedgerError('BAD_OP', f'未知操作 {kind}。')
    return {'op': kind, 'created': created}


def undo(conn) -> dict:
    return {'op': 'undo', 'undone': ops.undo(conn)}


def redo(conn) -> dict:
    return {'op': 'redo', 'redone': ops.redo(conn)}


# ── 回放 ───────────────────────────────────────────────────────────────

def replay_history(conn) -> list[dict]:
    titles = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='agentcore_run'").fetchone():
        titles = dict(conn.execute(
            "SELECT r.id, (SELECT substr(json_extract(m.payload_json, '$.text'), 1, 100) "
            "FROM agentcore_message m WHERE m.session_id=r.session_id AND m.kind='user_text' "
            "AND m.id>r.start_id AND (r.end_id IS NULL OR m.id<r.end_id) ORDER BY m.id LIMIT 1) "
            "FROM agentcore_run r WHERE r.id IN (SELECT run_id FROM ledger2_replay_event)"
        ))
    return [{**dict(row), 'request': titles.get(row['run_id'])} for row in conn.execute(
        "SELECT e.id, e.op_seq AS seq, e.kind, e.run_id, e.created_at AS at, "
        "json_extract(o.op_json, '$.name') AS name "
        "FROM ledger2_replay_event e JOIN ledger2_op_log o ON o.seq=e.op_seq ORDER BY e.id"
    )]


def _affected(primitives: list[dict]) -> list[str]:
    refs = []
    for primitive in primitives:
        value = primitive['row'] if primitive['op'] == 'tx_restore' else primitive
        if 'tx_id' in value:
            prefix = 'txm' if value.get('role') == 'mirror' else 'tx'
            refs.append(f"{prefix}:{value['tx_id']}")
        elif value.get('group_id'):
            refs.append(f"g:{value['group_id']}")
        elif value.get('node_id'):
            refs.append(value['node_id'])
    return list(dict.fromkeys(refs))


def _replay_items(conn, refs: list[str]) -> dict:
    if not refs:
        return {}
    # 按日志里的引用查名称和直接父级，跳过完整树与金额计算。
    rows = conn.execute(
        "WITH refs AS (SELECT value AS ref FROM json_each(?)) "
        "SELECT r.ref, 'node' AS kind, n.name, n.parent_id AS parent, n.note "
        "FROM refs r JOIN ledger2_node n ON n.id=r.ref "
        "UNION ALL SELECT r.ref, 'group', COALESCE(NULLIF(g.name,''), '未命名净额组'), g.node_id, g.note "
        "FROM refs r JOIN ledger2_net_group g ON g.id=substr(r.ref,3) WHERE r.ref LIKE 'g:%' "
        "UNION ALL SELECT r.ref, 'tx', COALESCE(NULLIF(meta.title,''), NULLIF(t.counterparty,''), "
        "NULLIF(t.item,''), '#' || t.id), COALESCE('g:' || m.net_group_id, m.node_id), COALESCE(meta.note,'') "
        "FROM refs r JOIN ledger2_tx m ON m.tx_id=CAST(substr(r.ref,instr(r.ref,':')+1) AS INTEGER) "
        "AND m.role=(CASE WHEN r.ref LIKE 'txm:%' THEN 'mirror' ELSE 'primary' END) "
        "JOIN transactions_raw t ON t.id=m.tx_id "
        "LEFT JOIN ledger2_tx meta ON meta.tx_id=m.tx_id AND meta.role='primary' "
        "WHERE (r.ref LIKE 'tx:%' OR r.ref LIKE 'txm:%') "
        "AND NOT EXISTS(SELECT 1 FROM tx_deletions d WHERE d.tx_id=t.id)",
        (json.dumps(refs, ensure_ascii=False),),
    )
    return {row['ref']: dict(row) for row in rows}


def _with_parents(conn, items: dict) -> dict:
    """每个展示项最多补两级，让目标组及其父级同时可见。"""
    scene = dict(items)
    frontier = items
    for _ in range(2):
        refs = list(dict.fromkeys(row['parent'] for row in frontier.values()
                                  if row['parent'] and row['parent'] not in scene))
        frontier = _replay_items(conn, refs)
        scene.update(frontier)
    return scene


def _replay_step(conn, row) -> dict:
    primitives = json.loads(row['primitives'])
    refs = _affected(primitives)
    before = _with_parents(conn, _replay_items(conn, refs))
    for primitive in primitives:
        ops._apply(conn, primitive)
    after = _with_parents(conn, _replay_items(conn, refs))
    changed = [ref for ref in refs if before.get(ref) != after.get(ref)]

    def scene(items: dict) -> list[dict]:
        keys = dict.fromkeys(ref for ref in changed if ref in items)
        for ref in list(keys):
            for _ in range(2):
                ref = items[ref]['parent']
                if ref not in items:
                    break
                keys[ref] = None
        return [items[ref] for ref in keys]

    return {'id': row['id'], 'changed': changed, 'before': scene(before), 'after': scene(after)}


def replay_frames(source, event_ids: list[int]) -> dict:
    if not event_ids or event_ids != sorted(set(event_ids)):
        raise LedgerError('REPLAY_RANGE', '请按时间顺序选择连续的操作步骤。')

    with closing(sqlite3.connect(':memory:')) as conn:
        source.backup(conn)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys = ON')
        chosen = conn.execute(
            "SELECT e.id, CASE WHEN e.kind='undo' THEN o.undo_json ELSE json_extract(o.op_json,'$.prims') END AS primitives "
            'FROM ledger2_replay_event e JOIN ledger2_op_log o ON o.seq=e.op_seq '
            'WHERE e.id BETWEEN ? AND ? ORDER BY e.id', (event_ids[0], event_ids[-1]),
        ).fetchall()
        if [row['id'] for row in chosen] != event_ids:
            raise LedgerError('REPLAY_RANGE', '所选步骤之间有空缺或历史已更新，请重新选择。')

        try:
            rewind = conn.execute(
                "SELECT CASE WHEN e.kind='undo' THEN json_extract(o.op_json,'$.prims') ELSE o.undo_json END "
                'FROM ledger2_replay_event e JOIN ledger2_op_log o ON o.seq=e.op_seq '
                'WHERE e.id>=? ORDER BY e.id DESC', (event_ids[0],),
            )
            for (primitives,) in rewind:
                for prim in json.loads(primitives):
                    ops._apply(conn, prim)

            return {'steps': [_replay_step(conn, row) for row in chosen]}
        except (sqlite3.IntegrityError, LedgerError) as exc:
            raise LedgerError('REPLAY_UNAVAILABLE', '相关历史数据已变化，无法还原这段星图。') from exc
