"""无数据库副作用的账本结构操作。"""
from __future__ import annotations
from copy import deepcopy
from uuid import uuid4
from .errors import Code, LedgerError
from .atoms import EXTERNAL_FUNDING, analyze_special_transactions


def _external_node_id(state: dict, book_id: str) -> str:
    found = [n for n in state['nodes'] if n.get('kind') == 'external_income']
    if found:
        return found[0]['id']
    node_id = f'{book_id}:external-income'
    if any(n['id'] == node_id for n in state['nodes']):
        node_id = f'{book_id}:external-income-{uuid4().hex[:8]}'
    state['nodes'].append({'id': node_id, 'name': '外部收入',
                           'parent_id': f'{book_id}:root', 'op': 'ADD',
                           'kind': 'external_income'})
    return node_id


def _prune_external_node(state: dict) -> None:
    if state.get('splits'):
        return
    state['nodes'] = [n for n in state['nodes'] if n.get('kind') != 'external_income']


def reconcile_special_splits(state: dict, facts: list[dict] | None = None) -> dict:
    """在交易成员变化后清理已不再适用的历史分裂。

    合法部分退款保留分裂，但金额会按剩余金额重算；
    全额退款、本人账户互转或退款证据冲突会自动解除分裂。
    """
    s = deepcopy(state)
    s.setdefault('splits', {})
    special = analyze_special_transactions(facts or [])
    blocked = (
        set(special['refund_zero_ids'])
        | set(special['transfer_zero_ids'])
        | set(special['refund_conflict_ids'])
    )
    for tx_key in list(s['splits']):
        if int(tx_key) in blocked:
            s['splits'].pop(tx_key, None)
    _prune_external_node(s)
    return s


def apply(state: dict, op: dict, book_id: str, tx_exists: bool = False,
          facts: list[dict] | None = None) -> dict:
    s = deepcopy(state)
    s.setdefault('splits', {})
    nodes = {n['id']: n for n in s['nodes']}
    placements = s['placements']
    facts_by_id = {int(tx['id']): tx for tx in (facts or [])}
    kind = op['op']

    def require_node(nid: str) -> dict:
        if nid not in nodes:
            raise LedgerError(Code.NODE_NOT_FOUND, '目标分组不存在，可能已经被删除。')
        return nodes[nid]

    def require_tx(tx_id: int) -> None:
        if str(tx_id) not in placements:
            raise LedgerError(Code.TX_NOT_IN_BOOK, '这笔交易不在当前账本中。')

    def reject_external_target(node: dict) -> None:
        if node.get('kind') == 'external_income':
            raise LedgerError(Code.EXTERNAL_NODE_FIXED, '外部收入节点只接收分裂产生的镜像腿，不能手动归入原始交易。')

    if kind == 'reassign':
        tx_id = int(op['tx_id']); require_tx(tx_id)
        target = require_node(op['to_node_id']); reject_external_target(target)
        placements[str(tx_id)] = target['id']
    elif kind == 'create_node':
        parent = require_node(op['parent_id']); reject_external_target(parent)
        name = op['name'].strip()
        if not name or len(name) > 50:
            raise LedgerError(Code.INVALID_NAME, '分组名必须为 1 至 50 个字符。')
        tx_ids = [int(x) for x in op.get('tx_ids', [])]
        if len(tx_ids) != len(set(tx_ids)):
            raise LedgerError(Code.DUPLICATE_TX, '同一笔交易不能在一次操作中重复选择。')
        for tx_id in tx_ids:
            require_tx(tx_id)
        requested_id = op.get('node_id')
        if requested_id is not None:
            if not isinstance(requested_id, str) or not requested_id or len(requested_id) > 160:
                raise LedgerError(Code.INVALID_NODE_ID, '指定的节点 ID 不合法。')
            if requested_id in nodes:
                raise LedgerError(Code.INVALID_NODE_ID, '指定的节点 ID 已经存在。')
            n_id = requested_id
        else:
            n_id = f'{book_id}:n{uuid4().hex[:12]}'
        s['nodes'].append({'id': n_id, 'name': name, 'parent_id': op['parent_id'],
                           'op': op.get('op_value', 'ADD'), 'kind': 'group'})
        for tx_id in tx_ids:
            placements[str(tx_id)] = n_id
    elif kind == 'set_op':
        n = require_node(op['node_id'])
        if n['parent_id'] is None:
            raise LedgerError(Code.ROOT_OP_FIXED, '总账根节点必须保持 ADD。')
        if n.get('kind') == 'external_income' and op['op_value'] != 'ADD':
            raise LedgerError(Code.EXTERNAL_NODE_FIXED, '外部收入节点必须保持 ADD。')
        n['op'] = op['op_value']
    elif kind == 'dissolve_node':
        n = require_node(op['node_id'])
        if n['parent_id'] is None:
            raise LedgerError(Code.CANNOT_DISSOLVE_ROOT, '不能解散总账。')
        if n.get('kind') == 'external_income' and s['splits']:
            raise LedgerError(Code.EXTERNAL_NODE_IN_USE, '仍有分裂交易使用外部收入节点，请先撤销分裂。')
        for child in s['nodes']:
            if child['parent_id'] == n['id']:
                child['parent_id'] = n['parent_id']
        for tx_id, nid in placements.items():
            if nid == n['id']:
                placements[tx_id] = n['parent_id']
        s['nodes'] = [node for node in s['nodes'] if node['id'] != n['id']]
    elif kind == 'attach_tx':
        tx_id = int(op['tx_id'])
        if str(tx_id) in placements:
            raise LedgerError(Code.ALREADY_ATTACHED, '这笔交易已经在当前账本里。')
        if not tx_exists:
            raise LedgerError(Code.TX_NOT_FOUND, '原始流水里不存在这笔交易。')
        target_id = op.get('node_id') or f'{book_id}:root'
        target = require_node(target_id); reject_external_target(target)
        s['members'].append(tx_id)
        placements[str(tx_id)] = target_id
    elif kind == 'detach_tx':
        tx_id = int(op['tx_id']); require_tx(tx_id)
        s['members'].remove(tx_id)
        del placements[str(tx_id)]
        s['splits'].pop(str(tx_id), None)
        _prune_external_node(s)
    elif kind == 'split':
        tx_id = int(op['tx_id']); require_tx(tx_id)
        tx = facts_by_id.get(tx_id)
        if tx is None:
            raise LedgerError(Code.TX_NOT_FOUND, '无法读取这笔交易的原始事实。')
        key = str(tx_id)
        if key in s['splits']:
            raise LedgerError(Code.ALREADY_SPLIT, '这笔交易已经分裂。')
        special = analyze_special_transactions(list(facts_by_id.values()))
        if (
            tx_id in special['refund_zero_ids']
            or tx_id in special['transfer_zero_ids']
            or tx_id in special['refund_conflict_ids']
        ):
            raise LedgerError(
                Code.SPLIT_NOT_APPLICABLE,
                '全额退款、内部转账或退款证据冲突的交易不能分裂。',
            )
        funding = str(tx.get('funding') or '自有').strip()
        if funding not in EXTERNAL_FUNDING:
            raise LedgerError(Code.SPLIT_NOT_APPLICABLE, '这笔交易没有配置为亲情卡、他人代付或外部额度，不能分裂。')
        _external_node_id(s, book_id)
        s['splits'][key] = {'funding': funding}
    elif kind == 'unsplit':
        tx_id = int(op['tx_id']); require_tx(tx_id)
        if str(tx_id) not in s['splits']:
            raise LedgerError(Code.NOT_SPLIT, '这笔交易当前没有分裂。')
        del s['splits'][str(tx_id)]
        _prune_external_node(s)
    else:
        raise LedgerError(Code.UNKNOWN_OP, '未知的账本操作。')
    return s
