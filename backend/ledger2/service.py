"""门面：HTTP / MCP / 测试都只经过这里。一个函数 = 一次完整的读或写，自己不持有连接。

读：`get_tree` 全量读或按日期范围做最小覆盖投影，金额是整数分。
写：`apply` 把一个 op 字典分发给 ops.py；`undo` / `redo` 走原语日志。
"""
from __future__ import annotations

from . import facts, invariants, ops, presets, reader
from .errors import LedgerError
from .model import GroupRef, TxRef
from .store import ensure_schema, root_id


def ensure(conn) -> None:
    ensure_schema(conn)


def _scope(conn, date_from: str | None, date_to: str | None):
    if not date_from and not date_to:
        return None
    if not (date_from and date_to):
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
        'scope': {'date_from': date_from, 'date_to': date_to, 'tx_in_scope': reader._count(root), 'tx_total': total},
        'uninit': len(facts.uninitialized_ids(conn)),
        'available': facts.date_bounds(conn),
        'status': status(conn),
    }


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
        raise LedgerError('MIRROR_HAS_NO_MANUAL', 't′ 没有人工字段，只能改它所属的 t。')


def apply(conn, op: dict) -> dict:
    """执行一个用户操作。返回 {'created': {...}}（新建对象的 id），树由调用方再读。

    选择语义分两种（见 draft 10）：
    - 最简（move / join / create_*）：`ops.fold_*`，被覆盖的子项不再单独处理；
    - 最繁（dissolve / split / unsplit）：`ops.expand_selection`，展开出全部可执行对象。
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
            raise LedgerError('EMPTY_NAME', '新建 +2 时必须命名。')
        txs = ops.fold_for_join(conn, _need(op, 'items'))
        created['group_id'] = ops.create_net_group(conn, txs, name, op.get('note') or '')
    elif kind == 'dissolve':
        nodes, groups, _ = ops.expand_selection(conn, _need(op, 'items'))
        nodes.discard(root_id(conn))            # 根不能解散；选中根时忽略它
        ops.dissolve(conn, sorted(nodes), sorted(groups))
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
