"""V2 账本工具：金额来自 get_tree，操作与页面共用 service。"""
from __future__ import annotations

import json
from contextlib import closing

from backend.ledger2 import facts, service
from backend.ledger2.errors import LedgerError
from backend.ledger2.tx import atomic

from . import actions
from .store import open_conn
from .truncate import page


class ToolInputError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ── 读 ──────────────────────────────────────────────────────────────────────

def _ref(item: dict) -> str:
    if item['type'] == 'group':
        return f"g:{item['id']}"
    return item.get('ref') or item['id']


def _items(item: dict) -> list[dict]:
    return item.get('children', item.get('members', []))


def _header(item: dict) -> dict:
    # 保留 service 返回的业务字段，只把集合拆出来分页。
    return {**{k: v for k, v in item.items() if k not in {'children', 'members'}},
            'ref': _ref(item)}


def _find(node: dict, ref: str, parent: str | None = None) -> tuple[dict, str | None] | None:
    if _ref(node) == ref:
        return node, parent
    for child in _items(node):
        found = _find(child, ref, _ref(node))
        if found is not None:
            return found
    return None


def read(args: dict) -> dict:
    mode = args.get('mode', 'summary')
    offset, limit = args.get('offset', 0), args.get('limit', 25)
    with closing(open_conn()) as conn:
        conn.execute('BEGIN')
        if mode == 'classification_examples':
            examples = actions.confirmed_classification_examples(conn)
            result = page(examples, offset, limit)
            return {'mode': mode, 'examples': result.pop('items'), 'count': result['total'], **result}

        full = service.get_tree(conn, args.get('date_from'), args.get('date_to'))
        tree = full.pop('tree')
        if mode == 'summary':
            top = page([_header(item) for item in _items(tree)], offset, limit)
            return {'mode': mode, 'root': _header(tree), 'top_level': top.pop('items'), **top, **full}

        if mode not in {'tree', 'node', 'tx'}:
            raise ToolInputError('BAD_MODE', f'未知的读取模式 {mode}。')
        ref = 'root' if mode == 'tree' else _need(args, 'ref')
        found = _find(tree, ref)
        if found is None or (mode == 'tx' and found[0]['type'] != 'tx'):
            raise ToolInputError('REF_NOT_FOUND', f'这个账本范围内没有 {ref}。')
        item, parent = found
        result = {'mode': mode, mode: _header(item), 'parent_ref': parent, 'scope': full['scope'],
                  'revision': full['status']['seq']}
        if item['type'] != 'tx':
            result.update(page([_header(child) for child in _items(item)], offset, limit))
        return result


# ── 写 ──────────────────────────────────────────────────────────────────────

_FIELDS = {
    'init': 'tx_ids all_uninitialized style plan target new_node',
    'uninstall': 'tx_ids',
    'create_node': 'items name note',
    'move': 'items target',
    'join': 'items group_id',
    'create_group': 'items name note',
    'dissolve': 'items',
    'split': 'items tx_id',
    'unsplit': 'items tx_id',
    'update': 'item name note',
    'dissolve_node': 'node_id',
    'rename_node': 'node_id name',
    'update_node': 'node_id name note',
    'update_manual': 'tx_id title note',
    'add_member': 'group_id tx',
    'remove_member': 'group_id tx',
    'rename_group': 'group_id name',
    'dissolve_group': 'group_id',
    'undo': 'expected_seq',
    'redo': 'expected_seq',
}
_OPS = set(_FIELDS)


def _need(args: dict, key: str):
    value = args.get(key)
    if value is None or (isinstance(value, (str, list)) and len(value) == 0):
        raise ToolInputError('MISSING_FIELD', f'这个操作缺少 {key}。')
    return value


def build_op(args: dict, conn) -> dict:
    """转换工具参数；全量选择在执行用的同一个事务中解析。"""
    kind = (args.get('op') or '').strip()
    if kind not in _OPS:
        raise ToolInputError('BAD_OP', f'不支持的操作 {kind!r}。')
    extra = args.keys() - {'op', *_FIELDS[kind].split()}
    if extra:
        raise ToolInputError('BAD_OP', f'{kind} 不接受字段 {", ".join(sorted(extra))}。')

    if kind in {'undo', 'redo'}:
        return {'type': kind}

    if kind == 'init':
        if args.get('all_uninitialized'):
            if 'tx_ids' in args:
                raise ToolInputError('BAD_OP', '全量初始化与 tx_ids 只能给一个。')
            ids = facts.uninitialized_ids(conn)
            if not ids:
                raise ToolInputError('NO_EFFECT', '没有待初始化的交易。')
            op: dict = {'type': 'init', 'tx_ids': ids}
        else:
            ids = _need(args, 'tx_ids')
            if not isinstance(ids, list):
                raise ToolInputError('BAD_TX_IDS', 'tx_ids 必须是交易 id 数组。')
            op = {'type': 'init', 'tx_ids': [int(i) for i in ids]}
        style = args.get('style')
        plan = args.get('plan')
        target = args.get('target')
        new_node = args.get('new_node')
        if sum(x is not None for x in (style, plan, target, new_node)) > 1:
            raise ToolInputError('BAD_OP', 'style / plan / target / new_node 只能给一个。')
        if style:
            op['style'] = style
        elif plan is not None:
            op['plan'] = plan
        elif target:
            op['target'] = str(target)
        elif new_node:
            if not isinstance(new_node, dict) or not str(new_node.get('name') or '').strip():
                raise ToolInputError('BAD_NEW_NODE', 'new_node 必须是带 name 的对象。')
            op['new_node'] = {
                'name': str(new_node['name']).strip(),
                **({'parent': str(new_node['parent'])} if new_node.get('parent') else {}),
            }
        else:
            op['style'] = 'platform'      # 默认走平台分类预设，而不是「全部未初始化」
        return op

    if kind == 'uninstall':
        ids = _need(args, 'tx_ids')
        if not isinstance(ids, list):
            raise ToolInputError('BAD_TX_IDS', 'tx_ids 必须是交易 id 数组。')
        return {'type': 'uninstall', 'tx_ids': [int(i) for i in ids]}

    if kind in {'move', 'join', 'create_group', 'dissolve', 'split', 'unsplit'}:
        op = {'type': kind}
        if 'items' in args and args['items'] is not None:
            op['items'] = _need(args, 'items')
        if kind == 'move':
            op['target'] = str(_need(args, 'target'))
        if kind == 'join':
            op['group_id'] = str(_need(args, 'group_id'))
        if kind == 'create_group':
            op['name'] = str(_need(args, 'name'))
            if args.get('note'):
                op['note'] = str(args['note'])
            op['items'] = _need(args, 'items')
        if kind in {'split', 'unsplit'} and 'tx_id' in args:
            if 'items' in op:
                raise ToolInputError('BAD_OP', 'items 和 tx_id 只能给一个。')
            op['tx_id'] = int(_need(args, 'tx_id'))
        if 'items' not in op and 'tx_id' not in op:
            raise ToolInputError('MISSING_FIELD', '这个操作需要 items 或 tx_id。')
        return op

    if kind == 'create_node':
        return {'type': kind, 'name': str(_need(args, 'name')),
                'items': _need(args, 'items'), 'note': str(args.get('note') or '')}

    if kind == 'update':
        op = {'type': kind, 'item': str(_need(args, 'item'))}
        if args.get('name') is not None:
            op['name'] = str(args['name'])
        if args.get('note') is not None:
            op['note'] = str(args['note'])
        if 'name' not in op and 'note' not in op:
            raise ToolInputError('MISSING_FIELD', 'update 至少要给 name 或 note。')
        return op

    if kind in {'dissolve_node', 'rename_node', 'update_node'}:
        op = {'type': kind, 'node_id': str(_need(args, 'node_id'))}
        if kind == 'rename_node':
            op['name'] = _need(args, 'name')
        if kind == 'update_node':
            op.update({key: args[key] for key in ('name', 'note') if args.get(key) is not None})
        return op

    if kind == 'update_manual':
        op = {'type': kind, 'tx_id': int(_need(args, 'tx_id'))}
        if args.get('title') is not None:
            op['title'] = str(args['title'])
        if args.get('note') is not None:
            op['note'] = str(args['note'])
        return op

    if kind in {'add_member', 'remove_member'}:
        tx = _need(args, 'tx')
        # service 的单成员操作接受 tx 引用；整数短写指主交易。
        if isinstance(tx, int):
            tx = f'tx:{int(tx)}'
        return {'type': kind, 'group_id': str(_need(args, 'group_id')), 'tx': str(tx)}

    if kind == 'rename_group':
        return {'type': kind, 'group_id': str(_need(args, 'group_id')),
                'name': str(_need(args, 'name'))}

    if kind == 'dissolve_group':
        return {'type': kind, 'group_id': str(_need(args, 'group_id'))}

    raise ToolInputError('BAD_OP', f'不支持的操作 {kind!r}。')


_HISTORY_TOP = {
    'undo': "SELECT seq FROM ledger2_op_log WHERE state='applied' ORDER BY seq DESC LIMIT 1",
    'redo': "SELECT seq FROM ledger2_op_log WHERE state='undone' ORDER BY seq ASC LIMIT 1",
}


def apply(args: dict, *, call_key: str | None = None) -> dict:
    """真实执行一次。HTTP 工具批次的调用键只用于恢复/重试，不由模型提供。"""
    with closing(open_conn()) as conn, atomic(conn):
        raw = json.dumps(args, ensure_ascii=False, sort_keys=True)
        if call_key:
            previous = conn.execute(
                'SELECT args_json,result_json FROM agentcore_ledger_receipt WHERE call_key=?', (call_key,),
            ).fetchone()
            if previous:
                if previous['args_json'] != raw:
                    raise ToolInputError('CALL_MISMATCH', '这个调用已按另一份参数执行。')
                return json.loads(previous['result_json'])

        op = build_op(args, conn)
        kind = op['type']
        seq = None
        if kind in _HISTORY_TOP:
            row = conn.execute(_HISTORY_TOP[kind]).fetchone()
            seq = row['seq'] if row else None
            if args.get('expected_seq') is not None and seq != args['expected_seq']:
                raise ToolInputError('HISTORY_CHANGED', '撤销/重做栈顶不再是指定操作，请重新读账本。')
        result = actions.run_op(conn, op)
        if kind not in _HISTORY_TOP:
            seq = conn.execute(_HISTORY_TOP['undo']).fetchone()['seq']
        receipt = {'state': 'executed', **result, 'seq': seq}
        if kind in {'init', 'uninstall'}:
            receipt['tx_count'] = len(op['tx_ids'])
        if call_key:
            conn.execute(
                'INSERT INTO agentcore_ledger_receipt VALUES(?,?,?)',
                (call_key, raw, json.dumps(receipt, ensure_ascii=False)),
            )
    return receipt


def committed_receipt(call_key: str) -> dict | None:
    with closing(open_conn()) as conn:
        row = conn.execute('SELECT result_json FROM agentcore_ledger_receipt WHERE call_key=?', (call_key,)).fetchone()
    return json.loads(row['result_json']) if row else None


def propose(args: dict, *, session_id: str | None) -> dict:
    with closing(open_conn()) as conn, atomic(conn):
        op = build_op(args, conn)
    try:
        result = actions.propose(session_id, op, title=_title(op))
    except (actions.ActionError, LedgerError) as exc:
        raise ToolInputError(getattr(exc, 'code', 'OP_FAILED'), str(exc)) from exc
    return {
        'action_id': result['action_id'],
        'state': 'pending',
        'op': op,
        'preview': result['preview'],
        'note': '这只是一份待确认预览，账本没有变化。用户在页面确认后才会真正执行。',
    }


def _title(op: dict) -> str:
    names = {
        'init': '初始化账本', 'uninstall': '卸载账本', 'move': '移动交易',
        'join': '加入净额组', 'create_group': '新建净额组', 'dissolve': '解散',
        'split': '拆分交易', 'unsplit': '取消拆分', 'update': '修改名称/备注',
        'create_node': '新建节点', 'undo': '撤销上一步', 'redo': '重做',
    }
    return names.get(op.get('type'), op.get('type') or '操作')
