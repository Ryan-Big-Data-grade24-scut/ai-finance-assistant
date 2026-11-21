"""读层：扁平行 → 树 → 作用域投影。

装配规则（错一次就重复计数）：

> 被某个 +2 组标记的交易，**只出现在组里面**，不再作为节点的直接子项出现。

于是 Node.value() 可以无条件对所有子项 +1，正好对应
`[t1, [t2,t3,t4]+2, t5, ..., tn]+1`（N5 / N8）。

只读已初始化的交易（facts 已过滤）；每个已初始化的交易必有归属行 —— 没有「默认父节点」。
`cat:<名>` 开头的节点是平台分类预设建出来的普通节点，读出时只标 kind=category。
"""
from __future__ import annotations

from dataclasses import replace

from . import facts
from .algebra import Vector, plus1, yuan
from .errors import LedgerError
from .model import NetGroup, Node, Scope, Tx, group_value


# ── 装配 ────────────────────────────────────────────────────────────────────

def read_ledger(conn) -> Node:
    """全量读：保留结构性的空节点（用户建了但还没放东西的 M）。"""
    node_rows = {r['id']: dict(r) for r in conn.execute('SELECT * FROM ledger2_node')}
    root_id = next((nid for nid, r in node_rows.items() if r['parent_id'] is None), None)
    if root_id is None:
        raise LedgerError('NO_ROOT', '这个账本没有根节点（总账）。')

    primaries = facts.load_txs(conn)
    splits = {int(r['src_tx_id']) for r in conn.execute('SELECT src_tx_id FROM ledger2_split_tx')}
    live_txs: dict[tuple[int, str], Tx] = {(t.id, 'primary'): t for t in primaries.values()}
    for src in splits:
        if src in primaries:
            live_txs[(src, 'mirror')] = replace(primaries[src], role='mirror')

    nodes: dict[str, Node] = {}
    for nid, row in node_rows.items():
        kind = 'root' if row['parent_id'] is None else ('category' if facts.is_category_node(nid) else 'node')
        nodes[nid] = Node(id=nid, parent_id=row['parent_id'], name=row['name'],
                          note=row['note'], kind=kind)

    node_children: dict[str, list] = {nid: [] for nid in nodes}
    placements = {(int(r['tx_id']), r['role']): dict(r) for r in conn.execute('SELECT * FROM ledger2_tx')}
    groups = {r['id']: dict(r) for r in conn.execute('SELECT * FROM ledger2_net_group')}
    group_rows: dict[str, list[dict]] = {gid: [] for gid in groups}
    group_members: dict[str, list[Tx]] = {gid: [] for gid in groups}

    for key, p in placements.items():
        if p['role'] == 'mirror' and key[0] not in splits:
            raise LedgerError('MIRROR_WITHOUT_SPLIT', f'交易 {key[0]} 有镜像归属行，但没有生成镜像的记录。')
        if p['net_group_id']:
            if p['net_group_id'] not in groups:
                raise LedgerError('GROUP_NOT_FOUND', f'归属行指向了不存在的组 {p["net_group_id"]}。')
            group_rows[p['net_group_id']].append(p)
        tx = live_txs.get(key)
        if tx is None:                         # 交易已被删除：归属行留着（可撤销），只是不显示
            continue
        if p['net_group_id']:
            group_members[p['net_group_id']].append(tx)
        else:
            node_children[p['node_id']].append(tx)

    for key, tx in live_txs.items():
        if key not in placements:
            raise LedgerError('PLACEMENT_MISSING', f'已初始化的交易 {tx.label} 没有归属行。', {'tx_id': tx.id})

    for gid, g in groups.items():
        rows = group_rows[gid]
        if len(rows) < 2:
            raise LedgerError(
                'GROUP_TOO_SMALL',
                f'净额组「{g["name"] or gid}」只剩 {len(rows)} 个成员，组至少要有 2 个成员。',
                {'group_id': gid, 'members': len(rows)},
            )
        for p in rows:
            if p['node_id'] != g['node_id']:
                raise LedgerError(
                    'GROUP_POSITION_DRIFT',
                    f'净额组「{g["name"] or gid}」的位置与成员不一致：组在 {g["node_id"]}，'
                    f'成员 {p["tx_id"]} 在 {p["node_id"]}。',
                    {'group_id': gid, 'node_id': g['node_id'], 'member': p['tx_id']},
                )
        members = sorted(group_members[gid], key=lambda t: (t.tx_time or '', t.id, t.role))
        if members:                            # 成员全被删了就不显示这个组
            node_children[g['node_id']].append(
                NetGroup(id=gid, name=g['name'] or '', node_id=g['node_id'],
                         note=(g.get('note') or ''), members=members))

    for nid, node in nodes.items():
        if node.parent_id is None:
            continue
        if node.parent_id not in nodes:
            raise LedgerError('DANGLING_NODE', f'节点 {node.id} 的父亲 {node.parent_id} 不存在。')
        node_children[node.parent_id].append(node)

    for nid, node in nodes.items():
        node.children = _sort_children(node_children[nid])

    root = nodes[root_id]
    _fill_values(root)
    return root


def _sort_children(children: list) -> list:
    def key(c):
        if isinstance(c, Node):
            return (0, c.name, c.id)
        if isinstance(c, NetGroup):
            return (1, c.name, c.id)
        return (2, c.tx_time or '', c.id, c.role)
    return sorted(children, key=key)


def _fill_values(node: Node) -> Vector:
    """全量口径（无作用域）：每个 M 算 +1，每个组算 ν。"""
    total = (0, 0)
    for c in node.children:
        if isinstance(c, Node):
            v = _fill_values(c)
        elif isinstance(c, NetGroup):
            v = group_value(c.members)
            c.value = v
        else:
            v = c.value()
        total = plus1([total, v])
    node.value = total
    return total


# ── 作用域投影 ──────────────────────────────────────────────────────────────

def read_scope(root: Node, scope: Scope) -> Node:
    """最小覆盖树投影（N11 / N14）。

    - 范围内没有交易的分类 / M **整个不出现**（不是显示成 0）；
    - 根永远保留；空范围时只有根，值 (0, 0)；
    - 组的值按「成员 ∩ 范围」算（N9）。
    """
    return _project_node(root, scope, is_root=True)


def _project_item(item, scope: Scope):
    """返回 (可见, 值, 投影后的对象)。"""
    if isinstance(item, Node):
        return _project_node(item, scope)
    if isinstance(item, NetGroup):
        members = [m for m in item.members if scope.contains(m.id)]
        v = group_value(members)
        return bool(members), v, NetGroup(id=item.id, name=item.name, node_id=item.node_id,
                                          note=item.note, members=members, value=v)
    visible = scope.contains(item.id)
    v = item.value() if visible else (0, 0)
    return visible, v, item


def _project_node(node: Node, scope: Scope, is_root: bool = False):
    kids = []
    total = (0, 0)
    visible = False
    for c in node.children:
        vis, v, projected = _project_item(c, scope)
        if vis:
            kids.append(projected)
            visible = True
        total = plus1([total, v])
    out = Node(id=node.id, parent_id=node.parent_id, name=node.name, note=node.note,
               children=kids, value=total, kind=node.kind)
    if is_root:
        return out
    return visible, total, out


# ── 读出结构（JSON）──────────────────────────────────────────────────────────

def _tx_json(t: Tx) -> dict:
    e, i = t.value()
    income = (t.direction > 0) != (t.role == 'mirror')
    return {
        'type': 'tx', 'tx_id': t.id, 'role': t.role, 'ref': f'tx:{t.id}' if t.role == 'primary' else f'txm:{t.id}',
        'e': e, 'i': i, 'kind': 'income' if income else 'expense',
        'date': t.date, 'tx_time': t.tx_time, 'category': t.category,
        'counterparty': t.counterparty, 'item': t.item, 'source': t.source, 'note': t.note,
        'title': t.title, 'memo': t.memo, 'category_path': t.category_path,
    }


def _count(c) -> int:
    if isinstance(c, Node):
        return sum(_count(x) for x in c.children)
    if isinstance(c, NetGroup):
        return len(c.members)
    return 1


def to_json(node: Node) -> dict:
    """读出树 → JSON（金额是整数分）。这是 HTTP / MCP 返回值的样子，前端只认这个形状。

    三种 type：node（M / 分类 / 根）/ group（+2）/ tx（叶子）。
    每个 M 都带 e, i（N12）；group 的 e,i = ν(⊕ 范围内成员)（N8 + D4）。
    """
    def item(c):
        if isinstance(c, Node):
            e, i = c.value or (0, 0)
            return {
                'type': 'node', 'id': c.id, 'name': c.name, 'note': c.note, 'kind': c.kind, 'e': e, 'i': i,
                'count': _count(c), 'children': [item(x) for x in c.children],
            }
        if isinstance(c, NetGroup):
            e, i = c.value or (0, 0)
            return {
                'type': 'group', 'id': c.id, 'name': c.name, 'node_id': c.node_id, 'note': c.note,
                'e': e, 'i': i, 'count': len(c.members),
                'members': [_tx_json(m) for m in c.members],
            }
        return _tx_json(c)

    return item(node)


# ── 打印 ────────────────────────────────────────────────────────────────────

def render(node: Node, indent: int = 0) -> str:
    """人看的树。"""
    e, i = node.value or (0, 0)
    lines = [f'{"  " * indent}{node.name}  e={yuan(e)} i={yuan(i)}']
    for c in node.children:
        lines.append(_render_child(c, indent + 1))
    return '\n'.join(lines)


def _render_child(c, indent: int) -> str:
    pad = '  ' * indent
    if isinstance(c, Node):
        e, i = c.value or (0, 0)
        head = f'{pad}├─ [M] {c.name}  e={yuan(e)} i={yuan(i)}'
        return '\n'.join([head] + [_render_child(g, indent + 1) for g in c.children])
    if isinstance(c, NetGroup):
        e, i = c.value or (0, 0)
        out = [f'{pad}├─ [+2] {c.name or c.id}  e={yuan(e)} i={yuan(i)}']
        for m in c.members:
            me, mi = m.value()
            out.append(f'{pad}│   · {m.label} {m.category or ""} e={yuan(me)} i={yuan(mi)}')
        return '\n'.join(out)
    e, i = c.value()
    income = (c.direction > 0) != (c.role == 'mirror')
    kind = '收入' if income else '支出'
    return f'{pad}├─ {c.label} {c.category or ""} {kind} e={yuan(e)} i={yuan(i)}'
