"""读层：扁平行 → 树 → 作用域投影。

装配规则（错一次就重复计数）：

> 被某个 +2 组标记的交易，**只出现在组里面**，不再作为节点的直接子项出现。

于是 Node.value() 可以无条件对所有子项 +1，正好对应
`[t1, [t2,t3,t4]+2, t5, ..., tn]+1`（N5 / N8）。
"""
from __future__ import annotations

from .algebra import Vector, plus1, yuan
from .errors import LedgerError
from .model import NetGroup, Node, Scope, Tx, group_value


# ── 装配 ────────────────────────────────────────────────────────────────────

def read_ledger(conn) -> Node:
    """读扁平行 → 装配成根 Node。顺带校验组的硬约束（位置漂移 / 成员数）。"""
    node_rows = {r['id']: dict(r) for r in conn.execute('SELECT * FROM ledger_node')}
    root_id = next((nid for nid, r in node_rows.items() if r['parent_id'] is None), None)
    if root_id is None:
        raise LedgerError('NO_ROOT', '这个账本没有根节点 M_r。')

    raw = {r['id']: dict(r) for r in conn.execute('SELECT * FROM transactions_raw')}
    splits = {r['src_tx_id'] for r in conn.execute('SELECT src_tx_id FROM ledger_split_tx')}
    placements = [dict(r) for r in conn.execute('SELECT * FROM ledger_placement')]
    groups = {r['id']: dict(r) for r in conn.execute('SELECT * FROM ledger_net_group')}

    def make_tx(tx_id: int, role: str) -> Tx:
        row = raw.get(tx_id)
        if row is None:
            raise LedgerError('TX_NOT_FOUND', f'交易 {tx_id} 在原始表里不存在。')
        return Tx(
            id=tx_id, role=role, amount=row['amount'], direction=row['direction'],
            tx_time=row['tx_time'], date=row['date'], category=row['category'],
            counterparty=row['counterparty'], item=row['item'],
        )

    placement_index: dict[tuple[int, str], dict] = {}
    group_members: dict[str, list[Tx]] = {gid: [] for gid in groups}
    node_children: dict[str, list] = {nid: [] for nid in node_rows}

    for p in placements:
        key = (p['tx_id'], p['role'])
        if key in placement_index:
            raise LedgerError('DUPLICATE_PLACEMENT', f'交易 {p["tx_id"]}（{p["role"]}）有重复的归属行。')
        placement_index[key] = p
        if p['role'] == 'mirror' and p['tx_id'] not in splits:
            raise LedgerError('MIRROR_WITHOUT_SPLIT', f'交易 {p["tx_id"]} 有镜像归属行，但没有分裂记录。')
        tx = make_tx(p['tx_id'], p['role'])
        if p['net_group_id']:
            if p['net_group_id'] not in groups:
                raise LedgerError('GROUP_NOT_FOUND', f'归属行指向了不存在的组 {p["net_group_id"]}。')
            group_members[p['net_group_id']].append(tx)
        else:
            node_children[p['node_id']].append(tx)

    # 无归属行的原始交易 = 在根
    for tx_id in raw:
        if (tx_id, 'primary') not in placement_index:
            node_children[root_id].append(make_tx(tx_id, 'primary'))
    # 无归属行的分裂副本 = 在根
    for src in splits:
        if (src, 'mirror') not in placement_index:
            node_children[root_id].append(make_tx(src, 'mirror'))

    # 组 → 挂在 group.node_id 下，并做硬校验
    for gid, g in groups.items():
        members = group_members[gid]
        if len(members) < 2:
            raise LedgerError(
                'GROUP_TOO_SMALL',
                f'净额组「{g["name"] or gid}」只剩 {len(members)} 个成员，组至少要有 2 个成员。',
                {'group_id': gid, 'members': len(members)},
            )
        for m in members:
            row = placement_index[m.key]
            if row['node_id'] != g['node_id']:
                raise LedgerError(
                    'GROUP_POSITION_DRIFT',
                    f'净额组「{g["name"] or gid}」的位置与成员不一致：组在 {g["node_id"]}，'
                    f'成员 {m.label} 在 {row["node_id"]}。',
                    {'group_id': gid, 'node_id': g['node_id'], 'member': m.label},
                )
        node_children[g['node_id']].append(NetGroup(id=gid, name=g['name'] or '', node_id=g['node_id'], members=members))

    nodes: dict[str, Node] = {}
    for nid, row in node_rows.items():
        nodes[nid] = Node(id=nid, parent_id=row['parent_id'], name=row['name'])

    # 节点挂到父节点下（父子链）
    for nid, node in nodes.items():
        if node.parent_id is None:
            continue
        if node.parent_id not in nodes:
            raise LedgerError('DANGLING_NODE', f'节点 {node.id} 的父亲 {node.parent_id} 不存在。')
        node_children[node.parent_id].append(node)

    for nid, node in nodes.items():
        node.children = _sort_children(node_children[nid])
    for node in nodes.values():
        if node.parent_id is not None and node.parent_id not in nodes:
            raise LedgerError('DANGLING_NODE', f'节点 {node.id} 的父亲 {node.parent_id} 不存在。')

    root = nodes[root_id]
    _fill_values(root)
    return root


def _sort_children(children: list) -> list:
    def key(c):
        if isinstance(c, Node):
            return (0, c.name, c.id)
        if isinstance(c, NetGroup):
            return (1, c.name, c.id)
        return (2, c.tx_time, c.id, c.role)
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

def scope_by_date(conn, date_from: str, date_to: str) -> Scope:
    """时间范围 → tx_ids（N14）。时间范围是读时参数，不落表。"""
    rows = conn.execute(
        'SELECT id FROM transactions_raw WHERE date BETWEEN ? AND ?',
        (date_from, date_to),
    ).fetchall()
    return Scope(frozenset(int(r['id']) for r in rows))


def scope_of_ids(tx_ids) -> Scope:
    return Scope(frozenset(int(t) for t in tx_ids))


def read_scope(root: Node, scope: Scope) -> Node:
    """最小覆盖树投影（N11 / N14）。

    - 范围内没有交易的分类**整个不出现**（不是显示成 0）；
    - 根永远保留；空范围时只有根，值 (0, 0)；
    - 组的值按「成员 ∩ 范围」算（N9）。
    """
    projected = _project_node(root, scope, is_root=True)
    return projected


def _project_item(item, scope: Scope):
    """返回 (可见, 值, 投影后的对象)。"""
    if isinstance(item, Node):
        return _project_node(item, scope)
    if isinstance(item, NetGroup):
        members = [m for m in item.members if scope.contains(m.id)]
        v = group_value(members)
        item.value = v
        return bool(members), v, NetGroup(id=item.id, name=item.name, node_id=item.node_id, members=members, value=v)
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
    out = Node(id=node.id, parent_id=node.parent_id, name=node.name, children=kids, value=total)
    if is_root:
        # 根永远保留（N14）
        return out
    return visible, total, out


# ── 读出结构（JSON）──────────────────────────────────────────────────────────

def to_json(node: Node) -> dict:
    """读出树 → JSON。这是将来 HTTP / MCP 返回值的样子（前端只认这个形状）。

    三种 type：node（M）/ group（+2）/ tx（叶子）。
    每个 M 都带 e, i（N12）；group 的 e,i = ν(⊕ 范围内成员)（N8 + D4）。
    """
    def item(c):
        if isinstance(c, Node):
            return {
                'type': 'node', 'id': c.id, 'name': c.name,
                'e': (c.value or (0, 0))[0], 'i': (c.value or (0, 0))[1],
                'children': [item(x) for x in c.children],
            }
        if isinstance(c, NetGroup):
            e, i = c.value or (0, 0)
            return {
                'type': 'group', 'id': c.id, 'name': c.name, 'node_id': c.node_id,
                'e': e, 'i': i,
                'members': [
                    {'tx_id': m.id, 'role': m.role, 'e': m.value()[0], 'i': m.value()[1]}
                    for m in c.members
                ],
            }
        e, i = c.value()
        return {'type': 'tx', 'tx_id': c.id, 'role': c.role, 'e': e, 'i': i}

    e, i = node.value or (0, 0)
    return {
        'type': 'node', 'id': node.id, 'name': node.name,
        'e': e, 'i': i,
        'children': [item(c) for c in node.children],
    }


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
    # 镜像副本的向量是反过来的，收入/支出也跟着反
    income = (c.direction > 0) != (c.role == 'mirror')
    kind = '收入' if income else '支出'
    return f'{pad}├─ {c.label} {c.category or ""} {kind} e={yuan(e)} i={yuan(i)}'
