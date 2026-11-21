"""写操作层。

两层结构：

* **原语层**：直接改表的最小动作。每个原语都能算出自己的逆
  （`node_create`↔`node_delete`、`tx_place`↔`tx_unplace`、…）。
* **用户层**：校验 + 编排原语。一次用户操作 = 一串原语；
  撤销 = 把这串原语反着执行（Q4：所有操作都可逆，撤销用的是同一操作集里的其它操作）。

日志表只存两样东西：这次的正向原语序列，和它的逆序列。
不存快照 —— 快照恢复不了「净额组里有几笔、分别是谁」这种结构性信息，
而原语逆序可以逐字段还原。
"""
from __future__ import annotations

import json
from uuid import uuid4

from .errors import LedgerError
from .model import TxRef
from .store import root_id

# ── 读表小工具 ──────────────────────────────────────────────────────────────

def _node(conn, nid: str) -> dict:
    row = conn.execute('SELECT * FROM ledger_node WHERE id=?', (nid,)).fetchone()
    if row is None:
        raise LedgerError('NODE_NOT_FOUND', f'分组 {nid} 不存在。')
    return dict(row)


def _parent_of(conn, nid: str) -> str:
    return _node(conn, nid)['parent_id']


def _node_name(conn, nid: str) -> str:
    """给人看的名字。根节点叫「总账」，其它叫自己的 name。"""
    row = conn.execute('SELECT name, parent_id FROM ledger_node WHERE id=?', (nid,)).fetchone()
    if row is None:
        return nid
    return '总账' if row['parent_id'] is None else (row['name'] or nid)


def _children_nodes(conn, nid: str) -> list[str]:
    return [r['id'] for r in conn.execute('SELECT id FROM ledger_node WHERE parent_id=?', (nid,))]


def _placement(conn, tx_id: int, role: str) -> dict | None:
    row = conn.execute(
        'SELECT * FROM ledger_placement WHERE tx_id=? AND role=?', (tx_id, role)
    ).fetchone()
    return dict(row) if row else None


def _resolve_node(conn, tx: TxRef) -> str:
    """归属行里的 node_id；没有行 = 在根。"""
    row = _placement(conn, tx.tx_id, tx.role)
    return row['node_id'] if row else root_id(conn)


def _group_of(conn, tx: TxRef) -> dict | None:
    row = _placement(conn, tx.tx_id, tx.role)
    if not row or not row['net_group_id']:
        return None
    g = conn.execute('SELECT * FROM ledger_net_group WHERE id=?', (row['net_group_id'],)).fetchone()
    if g is None:
        raise LedgerError('GROUP_NOT_FOUND', f'归属行指向了不存在的组 {row["net_group_id"]}。')
    return dict(g)


def _members(conn, gid: str) -> list[TxRef]:
    rows = conn.execute(
        'SELECT tx_id, role FROM ledger_placement WHERE net_group_id=? ORDER BY tx_id, role', (gid,)
    ).fetchall()
    return [TxRef(int(r['tx_id']), r['role']) for r in rows]


def _ancestor_chain(conn, nid: str) -> list[str]:
    """[自己, 父亲, …, 根]。"""
    chain: list[str] = []
    seen: set[str] = set()
    cur: str | None = nid
    while cur is not None and cur not in seen:
        chain.append(cur)
        seen.add(cur)
        cur = _parent_of(conn, cur)
    return chain


def _is_ancestor(conn, anc: str, node: str) -> bool:
    """anc 是 node 的**严格**祖先。"""
    return anc != node and anc in _ancestor_chain(conn, node)


def _lca(conn, node_ids: list[str]) -> str:
    ids = list(dict.fromkeys(node_ids))
    if not ids:
        raise LedgerError('NO_ITEMS', '没有可用的父节点。')
    chains = [_ancestor_chain(conn, i) for i in ids]
    common = set(chains[0])
    for c in chains[1:]:
        common &= set(c)
    for n in chains[0]:
        if n in common:
            return n
    raise LedgerError('NO_COMMON_ANCESTOR', '这些项没有公共祖先。')


def parse_item(raw) -> str | TxRef:
    """'tx:12' / 'txm:12' → 交易引用；其它字符串 → 节点 id。"""
    if isinstance(raw, TxRef):
        return raw
    s = str(raw)
    if s.startswith('tx:'):
        return TxRef(int(s[3:]), 'primary')
    if s.startswith('txm:'):
        return TxRef(int(s[4:]), 'mirror')
    return s


# ── 折叠与展开（N16 / N17 / R1）─────────────────────────────────────────────

def _fold(conn, items: list):
    """top()：去掉被集合里其它元素覆盖的项。

    覆盖 = 某项在某个被选中的节点子树里（节点项本身若是被选中节点的**后代**也被去掉）。
    """
    node_items = [i for i in items if isinstance(i, str)]
    tx_items = [i for i in items if isinstance(i, TxRef)]

    kept_nodes = []
    for n in node_items:
        if any(_is_ancestor(conn, other, n) for other in node_items if other != n):
            continue
        kept_nodes.append(n)

    kept_txs = []
    for t in tx_items:
        cur = _resolve_node(conn, t)
        if any(kn == cur or _is_ancestor(conn, kn, cur) for kn in kept_nodes):
            continue
        kept_txs.append(t)
    return kept_nodes, kept_txs


def _expand_groups(conn, tx_items: list[TxRef]) -> list[TxRef]:
    """R1：组成员不可分割。碰到组里的任何一笔，就带上整组。"""
    out: list[TxRef] = []
    seen: set[tuple[int, str]] = set()
    for t in tx_items:
        g = _group_of(conn, t)
        if g is None:
            if t.key not in seen:
                seen.add(t.key)
                out.append(t)
            continue
        for m in _members(conn, g['id']):
            if m.key not in seen:
                seen.add(m.key)
                out.append(m)
    return out


# ── 原语 ────────────────────────────────────────────────────────────────────

def _apply(conn, prim: dict) -> None:
    op = prim['op']
    if op == 'node_create':
        conn.execute('INSERT INTO ledger_node(id,parent_id,name) VALUES(?,?,?)',
                     (prim['node_id'], prim['parent_id'], prim['name']))
    elif op == 'node_delete':
        conn.execute('DELETE FROM ledger_node WHERE id=?', (prim['node_id'],))
    elif op == 'node_parent':
        conn.execute('UPDATE ledger_node SET parent_id=? WHERE id=?', (prim['parent_id'], prim['node_id']))
    elif op == 'tx_place':
        conn.execute(
            'INSERT INTO ledger_placement(tx_id,role,node_id,net_group_id) VALUES(?,?,?,?) '
            'ON CONFLICT(tx_id,role) DO UPDATE SET node_id=excluded.node_id, net_group_id=excluded.net_group_id',
            (prim['tx_id'], prim['role'], prim['node_id'], prim['group_id']))
    elif op == 'tx_unplace':
        conn.execute('DELETE FROM ledger_placement WHERE tx_id=? AND role=?', (prim['tx_id'], prim['role']))
    elif op == 'group_create':
        conn.execute('INSERT INTO ledger_net_group(id,name,node_id) VALUES(?,?,?)',
                     (prim['group_id'], prim['name'], prim['node_id']))
    elif op == 'group_delete':
        conn.execute('DELETE FROM ledger_net_group WHERE id=?', (prim['group_id'],))
    elif op == 'group_node':
        conn.execute('UPDATE ledger_net_group SET node_id=? WHERE id=?', (prim['node_id'], prim['group_id']))
    elif op == 'split_add':
        conn.execute('INSERT INTO ledger_split_tx(src_tx_id) VALUES(?)', (prim['src_tx_id'],))
    elif op == 'split_remove':
        conn.execute('DELETE FROM ledger_split_tx WHERE src_tx_id=?', (prim['src_tx_id'],))
    else:
        raise LedgerError('UNKNOWN_PRIMITIVE', f'未知原语 {op}。')


def _invert(prim: dict) -> dict:
    """一个原语的逆。"""
    op = prim['op']
    if op == 'node_create':
        return {'op': 'node_delete', 'node_id': prim['node_id']}
    if op == 'node_delete':
        return {'op': 'node_create', 'node_id': prim['node_id'], 'name': prim['name'], 'parent_id': prim['parent_id']}
    if op == 'node_parent':
        return {'op': 'node_parent', 'node_id': prim['node_id'], 'parent_id': prim['old_parent_id'],
                'old_parent_id': prim['parent_id']}
    if op == 'tx_place':
        if prim['had_row']:
            return {'op': 'tx_place', 'tx_id': prim['tx_id'], 'role': prim['role'],
                    'node_id': prim['old_node_id'], 'group_id': prim['old_group_id'], 'had_row': True}
        return {'op': 'tx_unplace', 'tx_id': prim['tx_id'], 'role': prim['role'],
                'node_id': prim['node_id'], 'group_id': prim['group_id']}
    if op == 'tx_unplace':
        return {'op': 'tx_place', 'tx_id': prim['tx_id'], 'role': prim['role'],
                'node_id': prim['node_id'], 'group_id': prim['group_id'], 'had_row': True}
    if op == 'group_create':
        return {'op': 'group_delete', 'group_id': prim['group_id']}
    if op == 'group_delete':
        return {'op': 'group_create', 'group_id': prim['group_id'], 'name': prim['name'], 'node_id': prim['node_id']}
    if op == 'group_node':
        return {'op': 'group_node', 'group_id': prim['group_id'], 'node_id': prim['old_node_id'],
                'old_node_id': prim['node_id']}
    if op == 'split_add':
        return {'op': 'split_remove', 'src_tx_id': prim['src_tx_id']}
    if op == 'split_remove':
        return {'op': 'split_add', 'src_tx_id': prim['src_tx_id']}
    raise LedgerError('UNKNOWN_PRIMITIVE', f'未知原语 {op}。')


def _run(conn, prims: list[dict], name: str) -> None:
    """执行一串原语并记日志（正向 + 逆向）。整个过程在一个事务里。"""
    if not prims:
        raise LedgerError('NO_EFFECT', '这个操作没有任何效果。')
    undo = [_invert(p) for p in reversed(prims)]
    with conn:
        # 新操作把「重做栈」清掉：已经撤销、但还没被重做的那些操作作废。
        # 否则「撤销 → 改点别的 → 重做」会把旧操作复活，状态和操作历史对不上。
        conn.execute("UPDATE ledger_op_log SET state='discarded' WHERE state='undone'")
        for p in prims:
            _apply(conn, p)
        conn.execute(
            "INSERT INTO ledger_op_log(op_json, undo_json, state) VALUES(?, ?, 'applied')",
            (json.dumps({'name': name, 'prims': prims}, ensure_ascii=False),
             json.dumps(undo, ensure_ascii=False)),
        )


# ── 构造原语 ────────────────────────────────────────────────────────────────

def _prim_tx_place(conn, tx: TxRef, node_id: str, group_id: str | None) -> dict | None:
    old = _placement(conn, tx.tx_id, tx.role)
    if node_id == root_id(conn) and group_id is None and old is None:
        return None                      # 本来就在根、也不属于任何组 → 什么都不用写
    return {
        'op': 'tx_place', 'tx_id': tx.tx_id, 'role': tx.role,
        'node_id': node_id, 'group_id': group_id,
        'had_row': old is not None,
        'old_node_id': old['node_id'] if old else None,
        'old_group_id': old['net_group_id'] if old else None,
    }


def _prim_tx_unplace(conn, tx: TxRef) -> dict | None:
    old = _placement(conn, tx.tx_id, tx.role)
    if old is None:
        return None
    return {'op': 'tx_unplace', 'tx_id': tx.tx_id, 'role': tx.role,
            'node_id': old['node_id'], 'group_id': old['net_group_id']}


def _place_tx(conn, tx: TxRef, node_id: str, group_id: str | None) -> dict | None:
    """把交易放到某个节点：归到根且不属于任何组时 → 删行；否则 → 写行。"""
    if node_id == root_id(conn) and group_id is None:
        return _prim_tx_unplace(conn, tx)
    return _prim_tx_place(conn, tx, node_id, group_id)


# ── 用户操作 ────────────────────────────────────────────────────────────────

def create_node(conn, name: str, items: list) -> str:
    """操作 1：创建新的 M（N16）。

    - 被选项求 LCA，新节点挂在 LCA 下；
    - top() 折叠后各项改挂到新节点；
    - 折叠掉的项（被选中的节点覆盖的那些）不动。
    """
    parsed = [parse_item(i) for i in items]
    if not parsed:
        raise LedgerError('EMPTY_SELECTION', '至少要选一项才能建分组。')
    name = (name or '').strip()
    if not name:
        raise LedgerError('EMPTY_NAME', '分组名不能为空。')

    kept_nodes, kept_txs = _fold(conn, parsed)
    kept_txs = _expand_groups(conn, kept_txs)
    if not kept_nodes and not kept_txs:
        raise LedgerError('EMPTY_SELECTION', '选中的项互相覆盖，没有可以新建的东西。')

    parents = [_parent_of(conn, n) for n in kept_nodes]
    parents += [_resolve_node(conn, t) for t in kept_txs]
    lca = _lca(conn, parents)

    new_id = uuid4().hex
    prims = [{'op': 'node_create', 'node_id': new_id, 'name': name, 'parent_id': lca}]
    for n in kept_nodes:
        prims.append({'op': 'node_parent', 'node_id': n, 'parent_id': new_id, 'old_parent_id': _parent_of(conn, n)})
    for t in kept_txs:
        g = _group_of(conn, t)
        if g and g['node_id'] != new_id:
            prims.append({'op': 'group_node', 'group_id': g['id'], 'node_id': new_id, 'old_node_id': g['node_id']})
        p = _place_tx(conn, t, new_id, g['id'] if g else None)
        if p:
            prims.append(p)
    _run(conn, prims, 'create_node')
    return new_id


def move(conn, items: list, target: str) -> None:
    """操作 2：移动（N17）。

    移动的是 top(item_ids) —— 一个去重后、保持原有结构的集合，
    集合内元素**不合并**，只是同时换了父节点。
    """
    parsed = [parse_item(i) for i in items]
    if not parsed:
        raise LedgerError('EMPTY_SELECTION', '没有要移动的项。')
    target = str(target)
    _node(conn, target)

    kept_nodes, kept_txs = _fold(conn, parsed)
    kept_txs = _expand_groups(conn, kept_txs)
    if not kept_nodes and not kept_txs:
        raise LedgerError('EMPTY_SELECTION', '选中的项互相覆盖，没有可以移动的东西。')

    for n in kept_nodes:
        # 目标不能落在被移动节点自己的子树里（否则成环）
        if target == n or _is_ancestor(conn, n, target):
            raise LedgerError('CYCLE', '不能把一个分组移到它自己的子树里。')

    prims = []
    for n in kept_nodes:
        prims.append({'op': 'node_parent', 'node_id': n, 'parent_id': target, 'old_parent_id': _parent_of(conn, n)})
    for t in kept_txs:
        g = _group_of(conn, t)
        if g and g['node_id'] != target:
            prims.append({'op': 'group_node', 'group_id': g['id'], 'node_id': target, 'old_node_id': g['node_id']})
        p = _place_tx(conn, t, target, g['id'] if g else None)
        if p:
            prims.append(p)
    _run(conn, prims, 'move')


def dissolve_node(conn, node_id: str) -> None:
    """解散一个 M：子节点上移到父节点。根节点不可解散。"""
    n = _node(conn, node_id)
    if n['parent_id'] is None:
        raise LedgerError('CANNOT_DISSOLVE_ROOT', '不能解散总账根节点。')
    parent = n['parent_id']

    prims = []
    for child in _children_nodes(conn, node_id):
        prims.append({'op': 'node_parent', 'node_id': child, 'parent_id': parent, 'old_parent_id': node_id})
    for tx in _txs_at(conn, node_id):
        g = _group_of(conn, tx)
        if g and g['node_id'] != parent:
            prims.append({'op': 'group_node', 'group_id': g['id'], 'node_id': parent, 'old_node_id': g['node_id']})
        p = _place_tx(conn, tx, parent, g['id'] if g else None)
        if p:
            prims.append(p)
    prims.append({'op': 'node_delete', 'node_id': node_id, 'name': n['name'], 'parent_id': parent})
    _run(conn, prims, 'dissolve_node')


def _txs_at(conn, node_id: str) -> list[TxRef]:
    rows = conn.execute(
        'SELECT tx_id, role FROM ledger_placement WHERE node_id=? ORDER BY tx_id, role', (node_id,)
    ).fetchall()
    return [TxRef(int(r['tx_id']), r['role']) for r in rows]


def create_net_group(conn, items: list, name: str | None = None) -> str:
    """操作 3：新建 +2 关系（N10 / N18）。

    成员**必须同父**（Q1：不能放宽）—— 跨 M 直接拒绝并报出各自的父节点。
    """
    parsed = [parse_item(i) for i in items]
    txs: list[TxRef] = []
    for p in parsed:
        if not isinstance(p, TxRef):
            raise LedgerError('NOT_A_TX', '只有交易可以是净额组成员。')
        if p.key not in {t.key for t in txs}:
            txs.append(p)
    if len(txs) < 2:
        raise LedgerError('GROUP_TOO_SMALL', '一个 +2 组至少要有 2 个成员。')

    by_node: dict[str, list[str]] = {}
    for t in txs:
        if _group_of(conn, t):
            raise LedgerError('ALREADY_GROUPED', f'{t} 已经属于别的净额组了。')
        by_node.setdefault(_resolve_node(conn, t), []).append(str(t))
    if len(by_node) > 1:
        detail = {'按父节点': {_node_name(conn, n): m for n, m in by_node.items()}}
        raise LedgerError(
            'SAME_PARENT_REQUIRED',
            '不能跨分组建立 +2 关系：' + '；'.join(
                f'「{_node_name(conn, n)}」下有 {"、".join(m)}' for n, m in by_node.items()
            ),
            detail,
        )

    node = next(iter(by_node))
    gid = uuid4().hex
    prims = [{'op': 'group_create', 'group_id': gid, 'name': name, 'node_id': node}]
    for t in txs:
        p = _place_tx(conn, t, node, gid)
        assert p is not None
        prims.append(p)
    _run(conn, prims, 'create_net_group')
    return gid


def add_group_member(conn, group_id: str, tx) -> None:
    """+2 组增员（N10）。新成员必须与组内现有成员**同父**。"""
    t = parse_item(tx)
    if not isinstance(t, TxRef):
        raise LedgerError('NOT_A_TX', '只有交易可以是净额组成员。')
    g = conn.execute('SELECT * FROM ledger_net_group WHERE id=?', (group_id,)).fetchone()
    if g is None:
        raise LedgerError('GROUP_NOT_FOUND', '这个净额组不存在。')
    g = dict(g)
    if _group_of(conn, t):
        raise LedgerError('ALREADY_GROUPED', f'{t} 已经属于别的净额组了。')
    node = _resolve_node(conn, t)
    if node != g['node_id']:
        raise LedgerError(
            'SAME_PARENT_REQUIRED',
            f'{t} 在「{_node_name(conn, node)}」下，组在「{_node_name(conn, g["node_id"])}」下：不能跨分组加入净额组。',
            {'tx_node': _node_name(conn, node), 'group_node': _node_name(conn, g['node_id'])},
        )
    p = _place_tx(conn, t, node, group_id)
    assert p is not None
    _run(conn, [p], 'add_group_member')


def remove_group_member(conn, group_id: str, tx) -> None:
    """+2 组减员（N10）。减到不足 2 个成员时，组自动解散（R3）。"""
    t = parse_item(tx)
    if not isinstance(t, TxRef):
        raise LedgerError('NOT_A_TX', '只有交易可以是净额组成员。')
    g = conn.execute('SELECT * FROM ledger_net_group WHERE id=?', (group_id,)).fetchone()
    if g is None:
        raise LedgerError('GROUP_NOT_FOUND', '这个净额组不存在。')
    g = dict(g)
    members = _members(conn, group_id)
    if t.key not in {m.key for m in members}:
        raise LedgerError('NOT_IN_GROUP', f'{t} 不在这个净额组里。')

    prims = []
    p = _place_tx(conn, t, g['node_id'], None)
    if p:
        prims.append(p)
    remaining = [m for m in members if m.key != t.key]
    if len(remaining) < 2:
        for m in remaining:
            q = _place_tx(conn, m, g['node_id'], None)
            if q:
                prims.append(q)
        prims.append({'op': 'group_delete', 'group_id': group_id, 'name': g['name'], 'node_id': g['node_id']})
    _run(conn, prims, 'remove_group_member')


def split_tx(conn, tx_id: int) -> None:
    """分裂（N19）：产生一个独立的 t′。

    - t′ 的 IE 向量刚好反过来（μ）；
    - t′ 继承 t 此刻的路径，但**不继承** t 的 +2 关系；
    - 同一个 t 只能分裂一次（ledger_split_tx 主键保证）。
    """
    exists = conn.execute('SELECT 1 FROM ledger_split_tx WHERE src_tx_id=?', (tx_id,)).fetchone()
    if exists:
        raise LedgerError('ALREADY_SPLIT', '这笔交易已经分裂过了。')
    raw = conn.execute('SELECT 1 FROM transactions_raw WHERE id=?', (tx_id,)).fetchone()
    if raw is None:
        raise LedgerError('TX_NOT_FOUND', '原始流水里没有这笔交易。')

    src = TxRef(int(tx_id), 'primary')
    node = _resolve_node(conn, src)
    prims = [{'op': 'split_add', 'src_tx_id': int(tx_id)}]
    p = _place_tx(conn, TxRef(int(tx_id), 'mirror'), node, None)
    if p:
        prims.append(p)
    _run(conn, prims, 'split_tx')


def unsplit_tx(conn, tx_id: int) -> None:
    """取消分裂：只砍掉 t′，t 原样保留（N19）。"""
    exists = conn.execute('SELECT 1 FROM ledger_split_tx WHERE src_tx_id=?', (tx_id,)).fetchone()
    if not exists:
        raise LedgerError('NOT_SPLIT', '这笔交易当前没有分裂。')
    prims = []
    mirror = TxRef(int(tx_id), 'mirror')
    g = _group_of(conn, mirror)
    p = _prim_tx_unplace(conn, mirror)
    if p:
        prims.append(p)
    if g:
        remaining = [m for m in _members(conn, g['id']) if m.key != mirror.key]
        if len(remaining) < 2:
            for m in remaining:
                q = _place_tx(conn, m, g['node_id'], None)
                if q:
                    prims.append(q)
            prims.append({'op': 'group_delete', 'group_id': g['id'], 'name': g['name'], 'node_id': g['node_id']})
    prims.append({'op': 'split_remove', 'src_tx_id': int(tx_id)})
    _run(conn, prims, 'unsplit_tx')


# ── 撤销 / 重做 ─────────────────────────────────────────────────────────────

def undo(conn) -> str:
    """撤销最后一次操作：执行它的逆原语序列。"""
    row = conn.execute(
        "SELECT * FROM ledger_op_log WHERE state='applied' ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LedgerError('NOTHING_TO_UNDO', '没有可以撤销的操作。')
    with conn:
        for p in json.loads(row['undo_json']):
            _apply(conn, p)
        conn.execute("UPDATE ledger_op_log SET state='undone' WHERE seq=?", (row['seq'],))
    return json.loads(row['op_json'])['name']


def redo(conn) -> str:
    """重做：把最后一次撤销的操作再执行一遍。"""
    row = conn.execute(
        "SELECT * FROM ledger_op_log WHERE state='undone' ORDER BY seq ASC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LedgerError('NOTHING_TO_REDO', '没有可以重做的操作。')
    op = json.loads(row['op_json'])
    with conn:
        for p in op['prims']:
            _apply(conn, p)
        conn.execute("UPDATE ledger_op_log SET state='applied' WHERE seq=?", (row['seq'],))
    return op['name']


def history(conn) -> list[dict]:
    """操作日志（给人看）。"""
    out = []
    for r in conn.execute('SELECT * FROM ledger_op_log ORDER BY seq'):
        op = json.loads(r['op_json'])
        out.append({'seq': r['seq'], 'name': op['name'], 'state': r['state'],
                    'prims': len(op['prims']), 'at': r['created_at']})
    return out
