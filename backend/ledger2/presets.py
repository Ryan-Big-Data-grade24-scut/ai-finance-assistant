"""初始化预设（数据层版）。引擎层以后会把它们包成「初始化风格」；这里先用最直接的方式实现，供测试和证据包使用。

预设只做一件事：**把一份 `op` 拼出来，交给 `ops.init_txs`**。所有校验、原子性、撤销都在 ops 里，这里不碰表。

平台分类预设：
* 每个平台分类建一个节点，id 固定为 `cat:<分类名>`，名字 = 分类名，挂在根下；节点已经存在就复用（不重建）；
* 每笔交易放进它自己的分类节点；
* 标题 = 交易的商品（没有则交易对方），备注 = 平台备注 —— 直接抄交易的不可变字段。
"""
from __future__ import annotations

from . import facts, ops
from .errors import LedgerError


def platform_category_op(conn, tx_ids: list[int]) -> dict:
    """拼出「按平台分类」的 init op（不执行）。"""
    ids = [int(t) for t in tx_ids]
    fields = facts.immutable_fields(conn, ids)
    missing = [t for t in ids if t not in fields]
    if missing:
        raise LedgerError('TX_NOT_FOUND', f'交易 {missing[0]} 不存在，或已被删除。', {'tx_ids': missing})

    by_cat: dict[str, list[int]] = {}
    for t in ids:
        by_cat.setdefault(fields[t]['category'], []).append(t)

    nodes, place = [], []
    for cat, txs in sorted(by_cat.items()):
        nid = facts.category_node_id(cat)
        exists = conn.execute('SELECT 1 FROM ledger2_node WHERE id=?', (nid,)).fetchone() is not None
        if exists:
            place.append({'node': nid, 'txs': txs})
        else:
            nodes.append({'ref': nid, 'id': nid, 'name': cat, 'parent': 'root'})
            place.append({'node': nid, 'txs': txs})
    meta = {t: {'title': fields[t]['title'], 'note': fields[t]['note']} for t in ids}
    return {'nodes': nodes, 'place': place, 'meta': meta}


def init_by_platform_category(conn, tx_ids: list[int] | None = None) -> dict:
    """按平台分类初始化。tx_ids 不给 = 全部「存活但还没初始化」的交易。"""
    ids = facts.uninitialized_ids(conn) if tx_ids is None else [int(t) for t in tx_ids]
    if not ids:
        raise LedgerError('NO_EFFECT', '没有需要初始化的交易。')
    return ops.init_txs(conn, ids, platform_category_op(conn, ids))


def placement_op(conn, tx_ids: list[int], *, target: str | None = None,
                 new_node: dict | None = None) -> dict:
    """拼出「指定放置」的 init op：所有交易放进一个已有节点，或一个新建节点。

    这是自动分类落地的最小形状：模型已经建议了「这几笔归到餐饮」，就不该先全量
    按平台预设初始化、再逐笔移动 —— 那样会先制造一批用户没看过的中间状态，而且要
    撤销两次。一次 init + 明确放置，预览里看到的就是最终状态。
    """
    ids = [int(t) for t in tx_ids]
    fields = facts.immutable_fields(conn, ids)
    missing = [t for t in ids if t not in fields]
    if missing:
        raise LedgerError('TX_NOT_FOUND', f'交易 {missing[0]} 不存在，或已被删除。',
                          {'tx_ids': missing})
    meta = {t: {'title': fields[t]['title'], 'note': fields[t]['note']} for t in ids}

    if new_node:
        name = str(new_node.get('name') or '').strip()
        if not name:
            raise LedgerError('EMPTY_NAME', '新节点的名字不能为空。')
        spec: dict = {'ref': 'new', 'name': name}
        parent = new_node.get('parent')
        if parent:
            if conn.execute('SELECT 1 FROM ledger2_node WHERE id=?', (str(parent),)).fetchone() is None:
                raise LedgerError('NODE_NOT_FOUND', f'父节点 {parent} 不存在。')
            spec['parent'] = str(parent)
        return {'nodes': [spec], 'place': [{'node': 'new', 'txs': ids}], 'meta': meta}

    if target:
        if conn.execute('SELECT 1 FROM ledger2_node WHERE id=?', (str(target),)).fetchone() is None:
            raise LedgerError('NODE_NOT_FOUND', f'目标节点 {target} 不存在。')
        return {'place': [{'node': str(target), 'txs': ids}], 'meta': meta}

    raise LedgerError('BAD_OP', '指定放置需要 target 或 new_node。')
