"""账本引擎不变量：结构覆盖、腿唯一归属、分裂对称、递归聚合与独立净额审计。"""
from __future__ import annotations
from .errors import Code, LedgerError
from .amount import cents, net
from .atoms import atom_value, effective_atom_value, generate_legs, EXTERNAL_FUNDING


def _check_net_idempotence() -> bool:
    """运行时对固定边界样本做轻量回归；完整随机性质测试放在 pytest 中。"""
    probes = [(e, i) for e in range(4) for i in range(4)]
    for e1, i1 in probes:
        for e2, i2 in probes:
            left = net(*[a + b for a, b in zip(net(e1, i1), net(e2, i2))])
            right = net(e1 + e2, i1 + i2)
            if left != right:
                return False
    return True


def validate(state: dict, facts: list[dict], book_id: str) -> dict:
    nodes = state['nodes']; members = state['members']; placements = state['placements']
    splits = state.get('splits') or {}
    ids = [n['id'] for n in nodes]
    if len(ids) != len(set(ids)) or not ids:
        raise LedgerError(Code.INVALID_TREE, '账本节点重复或根节点缺失。')
    node_map = {n['id']: n for n in nodes}
    roots = [n for n in nodes if n['parent_id'] is None]
    if len(roots) != 1 or roots[0]['id'] != f'{book_id}:root':
        raise LedgerError(Code.INVALID_ROOT, '账本必须有且只有一个根节点。')
    if roots[0]['op'] != 'ADD' or roots[0].get('kind') != 'root':
        raise LedgerError(Code.INVALID_ROOT, '总账根节点必须是 ADD。')
    allowed_kinds = {'root', 'group', 'external_income'}
    children: dict[str, list[str]] = {nid: [] for nid in ids}
    for n in nodes:
        if n.get('kind') not in allowed_kinds:
            raise LedgerError(Code.INVALID_NODE_KIND, '账本节点类型无效。')
        if n['op'] not in ('ADD', 'NET'):
            raise LedgerError(Code.INVALID_OP, '聚合方式只能是 ADD 或 NET。')
        if n.get('kind') == 'external_income' and n['op'] != 'ADD':
            raise LedgerError(Code.INVALID_EXTERNAL_NODE, '外部收入节点必须保持 ADD。')
        if n['parent_id'] is not None:
            if n['parent_id'] not in node_map:
                raise LedgerError(Code.ORPHAN_NODE, '存在没有父节点的分组。')
            if n.get('kind') == 'external_income' and n['parent_id'] != roots[0]['id']:
                raise LedgerError(Code.INVALID_EXTERNAL_NODE, '外部收入节点必须直接挂在总账下。')
            children[n['parent_id']].append(n['id'])

    visited: set[str] = set(); visiting: set[str] = set()
    def walk(nid: str) -> None:
        if nid in visiting:
            raise LedgerError(Code.TREE_CYCLE, '分组不能形成循环引用。')
        if nid in visited:
            return
        visiting.add(nid)
        for child in children[nid]:
            walk(child)
        visiting.remove(nid); visited.add(nid)
    walk(roots[0]['id'])
    if len(visited) != len(ids):
        raise LedgerError(Code.DISCONNECTED_TREE, '账本存在没有连到根节点的分组。')
    if len(members) != len(set(members)) or set(map(str, members)) != set(placements):
        raise LedgerError(Code.MISSING_ASSIGNMENT, '存在漏记或重复归位的交易。')
    if not set(placements.values()).issubset(node_map):
        raise LedgerError(Code.INVALID_ASSIGNMENT, '有交易落入不存在的分组。')
    if any(node_map[nid].get('kind') == 'external_income' for nid in placements.values()):
        raise LedgerError(Code.INVALID_ASSIGNMENT, '原始交易不能直接归入外部收入节点。')

    facts_by_id = {int(tx['id']): tx for tx in facts}
    if set(members) != set(facts_by_id):
        raise LedgerError(Code.MISSING_FACTS, '账本成员与原始流水不一致。')
    if not set(splits).issubset(set(map(str, members))):
        raise LedgerError(Code.INVALID_SPLIT_STATE, '分裂状态引用了不在账本中的交易。')
    external_nodes = [n for n in nodes if n.get('kind') == 'external_income']
    if splits and len(external_nodes) != 1:
        raise LedgerError(Code.INVALID_EXTERNAL_NODE, '存在分裂交易时必须且只能有一个外部收入节点。')
    if not splits and external_nodes:
        raise LedgerError(Code.INVALID_EXTERNAL_NODE, '没有分裂交易时不应保留空的外部收入节点。')
    for tx_key, split_meta in splits.items():
        funding = split_meta.get('funding') if isinstance(split_meta, dict) else str(split_meta)
        if funding not in EXTERNAL_FUNDING:
            raise LedgerError(Code.INVALID_SPLIT_STATE, '分裂记录缺少有效的外部额度来源。')

    try:
        generated = generate_legs(state, facts, book_id)
    except (ValueError, KeyError, TypeError) as exc:
        raise LedgerError(Code.LEG_GENERATION_FAILED, f'原子腿生成失败：{exc}') from exc
    legs = generated['legs']
    leg_ids = [leg['leg_id'] for leg in legs]
    if len(leg_ids) != len(set(leg_ids)):
        raise LedgerError(Code.LEG_DUPLICATE, '发现重复的记账腿。')
    primary_ids = {int(leg['tx_id']) for leg in legs if leg['role'] == 'primary'}
    if primary_ids != set(members):
        raise LedgerError(Code.LEG_INCOMPLETE, '原始交易与主腿集合不一致。')
    expected_mirrors = set(splits)
    mirror_ids = {str(leg['tx_id']) for leg in legs if leg['role'] == 'mirror'}
    if mirror_ids != expected_mirrors:
        raise LedgerError(Code.LEG_INCOMPLETE, '分裂交易与镜像腿集合不一致。')

    leg_by_tx_role = {(str(leg['tx_id']), leg['role']): leg for leg in legs}
    for tx_key in splits:
        primary = leg_by_tx_role.get((tx_key, 'primary'))
        mirror = leg_by_tx_role.get((tx_key, 'mirror'))
        if not primary or not mirror:
            raise LedgerError(Code.SPLIT_ASYMMETRIC, '分裂交易必须同时具有主腿和镜像腿。')
        if primary['e'] != mirror['i'] or primary['i'] != mirror['e'] or primary['e'] + primary['i'] != mirror['e'] + mirror['i']:
            raise LedgerError(Code.SPLIT_ASYMMETRIC, '分裂主腿与镜像腿金额不对称。')
        if mirror['node_id'] != external_nodes[0]['id']:
            raise LedgerError(Code.MIRROR_DESTINATION, '镜像腿只能进入外部收入节点。')

    by_node: dict[str, list[dict]] = {nid: [] for nid in ids}
    for leg in legs:
        if leg['node_id'] not in node_map:
            raise LedgerError(Code.LEG_ORPHAN, '存在归属到不存在节点的腿。')
        by_node[leg['node_id']].append(leg)

    totals: dict[str, dict] = {}
    def aggregate(nid: str) -> tuple[int, int, set[int]]:
        e = sum(int(leg['e']) for leg in by_node[nid])
        i = sum(int(leg['i']) for leg in by_node[nid])
        tx_ids = {int(leg['tx_id']) for leg in by_node[nid]}
        for child in children[nid]:
            ce, ci, child_tx_ids = aggregate(child)
            e += ce; i += ci; tx_ids.update(child_tx_ids)
        if node_map[nid]['op'] == 'NET':
            e, i = net(e, i)
        totals[nid] = {'e': e, 'i': i, 'net': i - e, 'count': len(tx_ids)}
        return e, i, tx_ids

    root_e, root_i, root_tx_ids = aggregate(roots[0]['id'])

    # I5：独立地从“直接腿 + 直接子节点”重算每个节点，校验递归聚合结果。
    for nid in ids:
        direct_e = sum(int(leg['e']) for leg in by_node[nid])
        direct_i = sum(int(leg['i']) for leg in by_node[nid])
        for child_id in children[nid]:
            direct_e += int(totals[child_id]['e'])
            direct_i += int(totals[child_id]['i'])
        if node_map[nid]['op'] == 'NET':
            direct_e, direct_i = net(direct_e, direct_i)
        if (direct_e, direct_i) != (totals[nid]['e'], totals[nid]['i']):
            raise LedgerError(Code.AUDIT_FAILED, f'节点 {nid} 未通过 I5 递归聚合校验。')

    all_leg_net = sum(int(leg['i']) - int(leg['e']) for leg in legs)
    raw_net = 0
    product_net = 0
    post_refund_net = 0
    after_transfer_net = 0
    refund_zero_ids = set(generated['refund_zero_ids'])
    transfer_zero_ids = set(generated['transfer_zero_ids'])
    refund_amounts = {
        int(key): int(value)
        for key, value in generated['refund_amounts_by_original'].items()
    }

    for tx in facts:
        tx_id = int(tx['id'])
        raw_e, raw_i = atom_value(tx)
        product_e, product_i = effective_atom_value(tx)
        raw_net += raw_i - raw_e
        product_net += product_i - product_e

        if tx_id in refund_zero_ids:
            continue

        original_cents = cents(tx['amount'])
        refunded_cents = refund_amounts.get(tx_id, 0)
        remaining_cents = original_cents - refunded_cents
        if remaining_cents < 0:
            raise LedgerError(Code.AUDIT_FAILED, f'交易 {tx_id} 的退款金额超过原交易金额。')

        if product_i > product_e:
            remaining_net = remaining_cents
        elif product_e > product_i:
            remaining_net = -remaining_cents
        else:
            remaining_net = 0
        post_refund_net += remaining_net
        if tx_id not in transfer_zero_ids:
            after_transfer_net += remaining_net

    neutral_direction_delta = product_net - raw_net
    refund_delta = post_refund_net - product_net
    transfer_delta = after_transfer_net - post_refund_net
    split_delta = all_leg_net - after_transfer_net
    transform_delta = (
        neutral_direction_delta + refund_delta + transfer_delta + split_delta
    )
    expected_net = raw_net + transform_delta

    if len(root_tx_ids) != len(members) or root_tx_ids != set(members):
        raise LedgerError(Code.AUDIT_FAILED, '根节点覆盖的交易集合与账本成员不一致。')
    if root_i - root_e != all_leg_net or root_i - root_e != expected_net:
        raise LedgerError(Code.AUDIT_FAILED, '根净额与原子腿分阶段审计链不一致。')
    if not _check_net_idempotence():
        raise LedgerError(Code.NET_IDEMPOTENCE_FAILED, 'NET 算子未通过幂等性校验。')

    invariant_names = [
        'I1 腿的唯一归属', 'I2 腿的完备性', 'I3 分裂对称',
        'I4 镜像腿去向唯一', 'I5 节点递归可加性', 'I6 NET 幂等性',
    ]
    return {
        'totals': totals, 'legs': legs, 'root_e': root_e, 'root_i': root_i,
        'root_net': root_i - root_e, 'count': len(root_tx_ids),
        'invariants': invariant_names, 'audit_ok': True,
        'warnings': generated['warnings'],
        'refund_zero_ids': generated['refund_zero_ids'],
        'transfer_zero_ids': generated['transfer_zero_ids'],
        'partial_refund_ids': generated['partial_refund_ids'],
        'refund_conflict_ids': generated['refund_conflict_ids'],
        'refund_amounts_by_original': generated['refund_amounts_by_original'],
        'refund_pairs': generated['refund_pairs'],
        'transfer_pairs': generated['transfer_pairs'],
        'audit': {
            'raw_net': raw_net,
            'neutral_direction_delta': neutral_direction_delta,
            'product_net': product_net,
            'refund_delta': refund_delta,
            'post_refund_net': post_refund_net,
            'transfer_delta': transfer_delta,
            'after_transfer_net': after_transfer_net,
            'split_delta': split_delta,
            'transformation_delta': transform_delta,
            'expected_net': expected_net,
            'root_net': root_i - root_e,
        },
    }
