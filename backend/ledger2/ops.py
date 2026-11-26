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
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

from . import facts, invariants
from .errors import LedgerError
from .model import GroupRef, TxRef
from .store import root_id
from .tx import atomic

_RUN_ID = ContextVar('ledger2_run_id', default=None)


@contextmanager
def history_run(run_id: str | None):
    token = _RUN_ID.set(run_id)
    try:
        yield
    finally:
        _RUN_ID.reset(token)


def _record_event(conn, seq: int, kind: str) -> None:
    conn.execute('INSERT INTO ledger2_replay_event(op_seq, kind, run_id) VALUES(?,?,?)',
                 (seq, kind, _RUN_ID.get()))

# ── 读表小工具 ──────────────────────────────────────────────────────────────

def _node(conn, nid: str) -> dict:
    row = conn.execute('SELECT * FROM ledger2_node WHERE id=?', (nid,)).fetchone()
    if row is not None:
        return dict(row)
    raise LedgerError('NODE_NOT_FOUND', f'分组 {nid} 不存在。')


def _parent_of(conn, nid: str) -> str:
    return _node(conn, nid)['parent_id']


def _node_name(conn, nid: str) -> str:
    """给人看的名字。根节点叫「总账」，其它叫自己的 name。"""
    row = conn.execute('SELECT name, parent_id FROM ledger2_node WHERE id=?', (nid,)).fetchone()
    if row is None:
        return nid
    return '总账' if row['parent_id'] is None else (row['name'] or nid)


def _children_nodes(conn, nid: str) -> list[str]:
    return [r['id'] for r in conn.execute('SELECT id FROM ledger2_node WHERE parent_id=?', (nid,))]


def _placement(conn, tx_id: int, role: str) -> dict | None:
    row = conn.execute(
        'SELECT * FROM ledger2_tx WHERE tx_id=? AND role=?', (tx_id, role)
    ).fetchone()
    return dict(row) if row else None


def _resolve_node(conn, tx: TxRef, *, alive_only: bool = True) -> str:
    """归属行里的 node_id。已初始化的交易必有行；没有行 = 没初始化（或镜像没有分裂）。

    alive_only：用户直接点名的交易必须存活；跟着组/节点一起搬的死交易允许（alive_only=False）。
    """
    if alive_only:
        facts.category_of(conn, tx.tx_id)      # 不存在或已删除的交易不能再被操作
    row = _placement(conn, tx.tx_id, tx.role)
    if row:
        return row['node_id']
    if tx.role == 'mirror':
        raise LedgerError('TX_NOT_FOUND', f'交易 {tx.tx_id} 没有镜像副本。')
    raise LedgerError('TX_NOT_INITIALIZED', f'交易 {tx.tx_id} 还没有初始化，不在账本里。')


def _group(conn, gid: str) -> dict:
    row = conn.execute('SELECT * FROM ledger2_net_group WHERE id=?', (gid,)).fetchone()
    if row is None:
        raise LedgerError('GROUP_NOT_FOUND', f'净额组 {gid} 不存在。')
    return dict(row)


def _group_of(conn, tx: TxRef) -> dict | None:
    row = _placement(conn, tx.tx_id, tx.role)
    if not row or not row['net_group_id']:
        return None
    g = conn.execute('SELECT * FROM ledger2_net_group WHERE id=?', (row['net_group_id'],)).fetchone()
    if g is None:
        raise LedgerError('GROUP_NOT_FOUND', f'归属行指向了不存在的组 {row["net_group_id"]}。')
    return dict(g)


def _members(conn, gid: str) -> list[TxRef]:
    rows = conn.execute(
        'SELECT tx_id, role FROM ledger2_tx WHERE net_group_id=? ORDER BY tx_id, role', (gid,)
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


def parse_item(raw) -> str | TxRef | GroupRef:
    """'tx:12' / 'txm:12' → 交易引用；'g:<id>' → +2 组；其它字符串 → 节点 id。"""
    if isinstance(raw, (TxRef, GroupRef)):
        return raw
    s = str(raw)
    if s.startswith('tx:'):
        return TxRef(int(s[3:]), 'primary')
    if s.startswith('txm:'):
        return TxRef(int(s[4:]), 'mirror')
    if s.startswith('g:'):
        if len(s) < 3:
            raise LedgerError('BAD_REF', f'「{s}」不是合法的净额组引用。')
        return GroupRef(s[2:])
    return s


# ── 折叠与展开（N16 / N17 / R1）─────────────────────────────────────────────

# 引擎层的多选只有两种解释方式：
#   * 最简 fold   —— 移动 / 加入 / 新建：只描述「顶层」，子树原样保留；
#   * 最繁 expand —— 分裂 / 解散 / 取消分裂：找出选择里的全部可执行对象。

def _fold(conn, items: list):
    """最简：去掉被集合里其它元素覆盖的项。返回 (节点, +2 组, 交易)。

    覆盖规则：
    * 节点项若是另一个选中节点的后代 → 去掉；
    * +2 组的位置节点落在某个保留选中节点的子树里 → 去掉；
    * 交易在某个保留选中节点的子树里，或它所属的组已被选中 → 去掉。
    """
    node_items = [i for i in items if isinstance(i, str)]
    group_items = [i for i in items if isinstance(i, GroupRef)]
    tx_items = [i for i in items if isinstance(i, TxRef)]

    kept_nodes: list[str] = []
    for n in dict.fromkeys(node_items):
        _node(conn, n)                                  # 不存在 → NODE_NOT_FOUND
        if any(_is_ancestor(conn, other, n) for other in node_items if other != n):
            continue
        kept_nodes.append(n)

    kept_groups: list[GroupRef] = []
    seen_groups: set[str] = set()
    for g in group_items:
        if g.id in seen_groups:
            continue
        seen_groups.add(g.id)
        row = _group(conn, g.id)
        if any(row['node_id'] == kn or _is_ancestor(conn, kn, row['node_id']) for kn in kept_nodes):
            continue
        kept_groups.append(g)

    kept_gids = {g.id for g in kept_groups}
    kept_txs: list[TxRef] = []
    seen_txs: set[tuple[int, str]] = set()
    for t in tx_items:
        if t.key in seen_txs:
            continue
        cur = _resolve_node(conn, t)
        if any(kn == cur or _is_ancestor(conn, kn, cur) for kn in kept_nodes):
            continue
        g = _group_of(conn, t)
        if g is not None and g['id'] in kept_gids:
            continue
        seen_txs.add(t.key)
        kept_txs.append(t)
    return kept_nodes, kept_groups, kept_txs


def _harmonize(conn, nodes: list, groups: list, txs: list):
    """R1：保留下来的交易如果属于某个 +2 组，整组一起来，交易本身不再单独处理。"""
    kept_gids = {g.id for g in groups}
    loose: list[TxRef] = []
    for t in txs:
        g = _group_of(conn, t)
        if g is None:
            loose.append(t)
        elif g['id'] not in kept_gids:
            kept_gids.add(g['id'])
            groups.append(GroupRef(g['id']))
    return nodes, groups, loose


def fold_selection(conn, items: list):
    """最简解释（移动 / 加入 / 新建 M）。返回 (节点 id, GroupRef, TxRef)。"""
    nodes, groups, txs = _fold(conn, [parse_item(i) for i in items])
    return _harmonize(conn, nodes, groups, txs)


def fold_for_join(conn, items: list) -> list[TxRef]:
    """加入 +2：最简折叠后必须全是交易（不能有 M / +2）。"""
    nodes, groups, txs = _fold(conn, [parse_item(i) for i in items])
    if nodes or groups:
        raise LedgerError('JOIN_REQUIRES_TX', '加入净额组只能选交易（原始交易或镜像交易），不能有分组或净额组。')
    if not txs:
        raise LedgerError('EMPTY_SELECTION', '没有可以加入的交易。')
    return txs


def _subtree_nodes(conn, nid: str) -> list[str]:
    out: list[str] = []
    stack = [nid]
    while stack:
        cur = stack.pop()
        if cur in out:
            continue
        out.append(cur)
        stack.extend(_children_nodes(conn, cur))
    return out


def _groups_at(conn, nid: str) -> list[str]:
    return [r['id'] for r in conn.execute('SELECT id FROM ledger2_net_group WHERE node_id=?', (nid,))]


def expand_selection(conn, items: list):
    """最繁解释（分裂 / 解散 / 取消分裂）。

    - 选中的 M：整棵子树里的节点、+2 组、交易；
    - 选中的 +2：全部成员；
    - 选中的 t / t′：自己。
    返回 (节点 id 集合, 组 id 集合, 交易引用集合)。
    """
    nodes: set[str] = set()
    groups: set[str] = set()
    txs: set[tuple[int, str]] = set()
    for item in items:
        p = parse_item(item)
        if isinstance(p, str):
            for nid in _subtree_nodes(conn, p):
                nodes.add(nid)
                groups.update(_groups_at(conn, nid))
                for t in _txs_at(conn, nid):
                    txs.add(t.key)
        elif isinstance(p, GroupRef):
            g = _group(conn, p.id)
            groups.add(p.id)
            for m in _members(conn, g['id']):
                txs.add(m.key)
        else:
            _resolve_node(conn, p)                      # 不存在 / 未初始化 / 没有镜像 → 报错
            txs.add(p.key)
    return nodes, groups, txs


def dissolve_selection(conn, items: list):
    """解散的选择：只取明确选中的 M / +2，不展开子树。

    解散一个 M 的含义是「拆掉这一层，把直接内容上移到父节点」；子节点是
    被保留的内容，不是被解散对象。以前这里复用最繁展开，选中 M1 会把未选中
    的 M11、M111 一起拆掉。显式同时选中父子时，仍交给 dissolve() 按深度处理。
    """
    nodes: list[str] = []
    groups: list[str] = []
    for item in items:
        p = parse_item(item)
        if isinstance(p, str):
            _node(conn, p)                              # 不存在的节点直接报错
            if p not in nodes:
                nodes.append(p)
        elif isinstance(p, GroupRef):
            _group(conn, p.id)
            if p.id not in groups:
                groups.append(p.id)
        # 交易引用（t / t′）不是解散对象；只选交易时由 dissolve() 报 NOTHING_TO_DISSOLVE。
    return nodes, groups


def split_candidates(conn, keys) -> list[int]:
    """分裂：从最繁展开的交易引用里筛出「还没分裂的原始 t」。

    - M / +2 在展开时就已经摊成交易，这里不再拒绝；
    - t′（role='mirror'）不是可分裂对象，跳过；
    - 已经分裂过的 t 跳过（再分一次没有意义，也不该让整批失败）。
    一个都没有 → NOTHING_TO_SPLIT。
    """
    ids: list[int] = []
    for tx_id, role in sorted(keys):
        if role != 'primary' or tx_id in ids:
            continue
        if conn.execute('SELECT 1 FROM ledger2_split_tx WHERE src_tx_id=?', (tx_id,)).fetchone():
            continue
        ids.append(tx_id)
    if not ids:
        raise LedgerError('NOTHING_TO_SPLIT', '选中的对象里没有可以生成镜像的原始交易（镜像交易和已经生成过镜像的会被跳过）。')
    return ids


def split_sources(conn, keys) -> list[int]:
    """取消分裂：从一批交易引用里筛出「当前确实分裂过」的源交易 id。"""
    ids = {int(tx_id) for tx_id, _role in keys}
    hits = [tx_id for tx_id in sorted(ids)
            if conn.execute('SELECT 1 FROM ledger2_split_tx WHERE src_tx_id=?', (tx_id,)).fetchone()]
    if not hits:
        raise LedgerError('NOTHING_TO_UNSPLIT', '选中的对象里没有已生成镜像的交易。')
    return hits


# ── 原语 ────────────────────────────────────────────────────────────────────

def _apply(conn, prim: dict) -> None:
    op = prim['op']
    if op == 'node_create':
        conn.execute('INSERT INTO ledger2_node(id,parent_id,name,note) VALUES(?,?,?,?)',
                     (prim['node_id'], prim['parent_id'], prim['name'], prim.get('note', '')))
    elif op == 'node_delete':
        conn.execute('DELETE FROM ledger2_node WHERE id=?', (prim['node_id'],))
    elif op == 'node_parent':
        conn.execute('UPDATE ledger2_node SET parent_id=? WHERE id=?', (prim['parent_id'], prim['node_id']))
    elif op == 'node_update':
        conn.execute('UPDATE ledger2_node SET name=?, note=? WHERE id=?',
                     (prim['name'], prim['note'], prim['node_id']))
    elif op == 'tx_place':
        conn.execute(
            'INSERT INTO ledger2_tx(tx_id,role,node_id,net_group_id) VALUES(?,?,?,?) '
            'ON CONFLICT(tx_id,role) DO UPDATE SET node_id=excluded.node_id, net_group_id=excluded.net_group_id',
            (prim['tx_id'], prim['role'], prim['node_id'], prim['group_id']))
    elif op == 'tx_unplace':
        conn.execute('DELETE FROM ledger2_tx WHERE tx_id=? AND role=?', (prim['tx_id'], prim['role']))
    elif op == 'tx_restore':
        row = prim['row']
        conn.execute(
            'INSERT OR REPLACE INTO ledger2_tx'
            '(tx_id,role,node_id,net_group_id,initialized,title,note,category_path) VALUES(?,?,?,?,?,?,?,?)',
            (row['tx_id'], row['role'], row['node_id'], row['net_group_id'], row['initialized'],
             row['title'], row['note'], row['category_path']))
    elif op == 'group_create':
        conn.execute('INSERT INTO ledger2_net_group(id,name,note,node_id) VALUES(?,?,?,?)',
                     (prim['group_id'], prim['name'], prim.get('note', ''), prim['node_id']))
    elif op == 'group_delete':
        conn.execute('DELETE FROM ledger2_net_group WHERE id=?', (prim['group_id'],))
    elif op == 'group_node':
        conn.execute('UPDATE ledger2_net_group SET node_id=? WHERE id=?', (prim['node_id'], prim['group_id']))
    elif op == 'node_rename':
        conn.execute('UPDATE ledger2_node SET name=? WHERE id=?', (prim['name'], prim['node_id']))
    elif op == 'group_rename':
        conn.execute('UPDATE ledger2_net_group SET name=? WHERE id=?', (prim['name'], prim['group_id']))
    elif op == 'group_update':
        conn.execute('UPDATE ledger2_net_group SET name=?, note=? WHERE id=?',
                     (prim['name'], prim['note'], prim['group_id']))
    elif op == 'split_add':
        conn.execute('INSERT INTO ledger2_split_tx(src_tx_id) VALUES(?)', (prim['src_tx_id'],))
    elif op == 'split_remove':
        conn.execute('DELETE FROM ledger2_split_tx WHERE src_tx_id=?', (prim['src_tx_id'],))
    elif op == 'manual_set':
        new = prim['new']
        if new is None:
            conn.execute("DELETE FROM ledger2_tx WHERE tx_id=? AND role='primary'", (prim['tx_id'],))
        else:
            cur = conn.execute(
                "UPDATE ledger2_tx SET initialized=1, title=?, note=?, category_path=? "
                "WHERE tx_id=? AND role='primary'",
                (new['title'], new['note'], json.dumps(new['category_path'], ensure_ascii=False), prim['tx_id']))
            if cur.rowcount == 0:
                raise LedgerError('TX_NOT_INITIALIZED', f'交易 {prim["tx_id"]} 没有初始化行，无法写人工字段。')
    else:
        raise LedgerError('UNKNOWN_PRIMITIVE', f'未知原语 {op}。')


def _invert(prim: dict) -> dict:
    """一个原语的逆。"""
    op = prim['op']
    if op == 'node_create':
        return {'op': 'node_delete', 'node_id': prim['node_id']}
    if op == 'node_delete':
        return {'op': 'node_create', 'node_id': prim['node_id'], 'name': prim['name'],
                'parent_id': prim['parent_id'], 'note': prim.get('note', '')}
    if op == 'node_parent':
        return {'op': 'node_parent', 'node_id': prim['node_id'], 'parent_id': prim['old_parent_id'],
                'old_parent_id': prim['parent_id']}
    if op == 'node_update':
        return {'op': 'node_update', 'node_id': prim['node_id'], 'name': prim['old_name'],
                'note': prim['old_note'], 'old_name': prim['name'], 'old_note': prim['note']}
    if op == 'tx_place':
        if prim['had_row']:
            return {'op': 'tx_place', 'tx_id': prim['tx_id'], 'role': prim['role'],
                    'node_id': prim['old_node_id'], 'group_id': prim['old_group_id'], 'had_row': True}
        return {'op': 'tx_unplace', 'tx_id': prim['tx_id'], 'role': prim['role'],
                'node_id': prim['node_id'], 'group_id': prim['group_id']}
    if op == 'tx_unplace':
        if prim.get('old_row') is not None:
            return {'op': 'tx_restore', 'row': prim['old_row']}
        return {'op': 'tx_place', 'tx_id': prim['tx_id'], 'role': prim['role'],
                'node_id': prim['node_id'], 'group_id': prim['group_id'], 'had_row': True}
    if op == 'tx_restore':
        return {'op': 'tx_unplace', 'tx_id': prim['row']['tx_id'], 'role': prim['row']['role'],
                'node_id': prim['row']['node_id'], 'group_id': prim['row']['net_group_id'],
                'old_row': prim['row']}
    if op == 'group_create':
        return {'op': 'group_delete', 'group_id': prim['group_id']}
    if op == 'group_delete':
        return {'op': 'group_create', 'group_id': prim['group_id'], 'name': prim['name'],
                'note': prim.get('note', ''), 'node_id': prim['node_id']}
    if op == 'group_node':
        return {'op': 'group_node', 'group_id': prim['group_id'], 'node_id': prim['old_node_id'],
                'old_node_id': prim['node_id']}
    if op == 'node_rename':
        return {'op': 'node_rename', 'node_id': prim['node_id'], 'name': prim['old_name'], 'old_name': prim['name']}
    if op == 'group_rename':
        return {'op': 'group_rename', 'group_id': prim['group_id'], 'name': prim['old_name'], 'old_name': prim['name']}
    if op == 'group_update':
        return {'op': 'group_update', 'group_id': prim['group_id'], 'name': prim['old_name'],
                'note': prim['old_note'], 'old_name': prim['name'], 'old_note': prim['note']}
    if op == 'split_add':
        return {'op': 'split_remove', 'src_tx_id': prim['src_tx_id']}
    if op == 'split_remove':
        return {'op': 'split_add', 'src_tx_id': prim['src_tx_id']}
    if op == 'manual_set':
        return {'op': 'manual_set', 'tx_id': prim['tx_id'], 'new': prim['old'], 'old': prim['new']}
    raise LedgerError('UNKNOWN_PRIMITIVE', f'未知原语 {op}。')


def _run(conn, prims: list[dict], name: str) -> None:
    """执行一串原语并记日志（正向 + 逆向）。整个过程在一个事务里，写后自检不过就回滚。"""
    if not prims:
        raise LedgerError('NO_EFFECT', '这个操作没有任何效果。')
    undo = [_invert(p) for p in reversed(prims)]
    with atomic(conn):
        # 新操作把「重做栈」清掉，否则「撤销 → 改点别的 → 重做」会把旧操作复活。
        conn.execute("UPDATE ledger2_op_log SET state='discarded' WHERE state='undone'")
        for p in prims:
            _apply(conn, p)
        invariants.check(conn)
        logged = conn.execute(
            "INSERT INTO ledger2_op_log(op_json, undo_json, state) VALUES(?, ?, 'applied')",
            (json.dumps({'name': name, 'prims': prims}, ensure_ascii=False),
             json.dumps(undo, ensure_ascii=False)),
        )
        _record_event(conn, logged.lastrowid, 'apply')


# ── 构造原语 ────────────────────────────────────────────────────────────────

def _prim_tx_place(conn, tx: TxRef, node_id: str, group_id: str | None) -> dict | None:
    old = _placement(conn, tx.tx_id, tx.role)
    if old is not None and old['node_id'] == node_id and old['net_group_id'] == group_id:
        return None                      # 本来就在那儿 → 什么都不用写
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
            'node_id': old['node_id'], 'group_id': old['net_group_id'], 'old_row': old}


def _place_tx(conn, tx: TxRef, node_id: str, group_id: str | None) -> dict | None:
    """把交易放到某个节点（已初始化的交易恒有归属行：更新，不删）。"""
    return _prim_tx_place(conn, tx, node_id, group_id)


def _prim_group_move(conn, gid: str, target: str) -> list[dict]:
    """整组搬家：组的位置 + 每一个成员的归属行（R1：组不能原地拆散）。"""
    g = _group(conn, gid)
    prims: list[dict] = []
    if g['node_id'] != target:
        prims.append({'op': 'group_node', 'group_id': gid, 'node_id': target, 'old_node_id': g['node_id']})
    for m in _members(conn, gid):
        p = _place_tx(conn, m, target, gid)
        if p:
            prims.append(p)
    return prims


# ── 用户操作 ────────────────────────────────────────────────────────────────

def create_node(conn, name: str, items: list, note: str = '') -> str:
    """操作 1：创建新的 M（N16）。

    - 被选项求 LCA，新节点挂在 LCA 下；
    - top() 折叠后各项改挂到新节点；
    - 折叠掉的项（被选中的节点覆盖的那些）不动；
    - note 是这个节点自己的备注（可选，不参与金额）。
    """
    parsed = [parse_item(i) for i in items]
    if not parsed:
        raise LedgerError('EMPTY_SELECTION', '至少要选一项才能建分组。')
    name = (name or '').strip()
    if not name:
        raise LedgerError('EMPTY_NAME', '分组名不能为空。')

    kept_nodes, kept_groups, kept_txs = _fold(conn, parsed)
    kept_nodes, kept_groups, kept_txs = _harmonize(conn, kept_nodes, kept_groups, kept_txs)
    if not kept_nodes and not kept_groups and not kept_txs:
        raise LedgerError('EMPTY_SELECTION', '选中的项互相覆盖，没有可以新建的东西。')

    parents = [_parent_of(conn, n) for n in kept_nodes]
    parents += [_group(conn, g.id)['node_id'] for g in kept_groups]
    parents += [_resolve_node(conn, t, alive_only=False) for t in kept_txs]
    lca = _lca(conn, parents)

    new_id = uuid4().hex
    prims = [{'op': 'node_create', 'node_id': new_id, 'name': name,
              'parent_id': lca, 'note': (note or '').strip()}]
    for n in kept_nodes:
        prims.append({'op': 'node_parent', 'node_id': n, 'parent_id': new_id, 'old_parent_id': _parent_of(conn, n)})
    for g in kept_groups:
        prims += _prim_group_move(conn, g.id, new_id)
    for t in kept_txs:
        p = _place_tx(conn, t, new_id, None)
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

    kept_nodes, kept_groups, kept_txs = _fold(conn, parsed)
    kept_nodes, kept_groups, kept_txs = _harmonize(conn, kept_nodes, kept_groups, kept_txs)
    if not kept_nodes and not kept_groups and not kept_txs:
        raise LedgerError('EMPTY_SELECTION', '选中的项互相覆盖，没有可以移动的东西。')

    for n in kept_nodes:
        # 目标不能落在被移动节点自己的子树里（否则成环）
        if target == n or _is_ancestor(conn, n, target):
            raise LedgerError('CYCLE', '不能把一个分组移到它自己的子树里。')

    prims = []
    for n in kept_nodes:
        prims.append({'op': 'node_parent', 'node_id': n, 'parent_id': target, 'old_parent_id': _parent_of(conn, n)})
    for g in kept_groups:
        prims += _prim_group_move(conn, g.id, target)
    for t in kept_txs:
        p = _place_tx(conn, t, target, None)
        if p:
            prims.append(p)
    _run(conn, prims, 'move')


def dissolve_node(conn, node_id: str) -> None:
    """解散一个 M（legacy 单入口）：子节点 / 交易上移到父节点，组整体上移。"""
    dissolve(conn, [str(node_id)], [])


def dissolve(conn, node_ids, group_ids) -> None:
    """批量解散（引擎层的「解散」）：M 和 +2 可以混着来，一次事务、一次撤销。

    - 选中 M：内容上移到最近的**未被解散**祖先，节点删除；
    - 选中 +2：成员退回组所在的节点（该节点也在解散集合里就继续上移），组删除。
    """
    nodes = list(dict.fromkeys(str(n) for n in node_ids))
    gids = list(dict.fromkeys(str(g) for g in group_ids))
    if not nodes and not gids:
        raise LedgerError('NOTHING_TO_DISSOLVE', '选中的对象里没有可解散的分组或净额组。')
    D = set(nodes)
    originals: dict[str, dict] = {}
    for n in nodes:
        row = _node(conn, n)
        if row['parent_id'] is None:
            raise LedgerError('CANNOT_DISSOLVE_ROOT', '不能解散总账根节点。')
        originals[n] = row

    def surviving(n: str) -> str:
        p = _parent_of(conn, n)
        while p in D:
            p = _parent_of(conn, p)
        return p

    depth = {n: len(_ancestor_chain(conn, n)) for n in nodes}
    ordered = sorted(nodes, key=lambda n: depth[n], reverse=True)   # 深的先删，避免外键引用

    prims: list[dict] = []
    handled: set[str] = set()
    moved: set[tuple[int, str]] = set()

    for gid in gids:
        g = _group(conn, gid)
        target = surviving(g['node_id']) if g['node_id'] in D else g['node_id']
        for m in _members(conn, gid):
            p = _place_tx(conn, m, target, None)
            if p:
                prims.append(p)
            moved.add(m.key)
        prims.append({'op': 'group_delete', 'group_id': gid, 'name': g['name'],
                      'note': g.get('note', ''), 'node_id': g['node_id']})
        handled.add(gid)

    for n in ordered:
        target = surviving(n)
        for row in conn.execute('SELECT * FROM ledger2_net_group WHERE node_id=?', (n,)):
            gid = row['id']
            if gid in handled:
                continue
            prims.append({'op': 'group_node', 'group_id': gid, 'node_id': target, 'old_node_id': n})
            for m in _members(conn, gid):
                p = _place_tx(conn, m, target, gid)
                if p:
                    prims.append(p)
                moved.add(m.key)
        for t in _txs_at(conn, n):
            if t.key in moved:
                continue
            p = _place_tx(conn, t, target, None)
            if p:
                prims.append(p)
        for c in _children_nodes(conn, n):
            if c in D:
                continue
            prims.append({'op': 'node_parent', 'node_id': c, 'parent_id': target, 'old_parent_id': n})
        row = originals[n]
        prims.append({'op': 'node_delete', 'node_id': n, 'name': row['name'],
                      'parent_id': row['parent_id'], 'note': row['note']})
    _run(conn, prims, 'dissolve')


def _txs_at(conn, node_id: str) -> list[TxRef]:
    rows = conn.execute(
        'SELECT tx_id, role FROM ledger2_tx WHERE node_id=? ORDER BY tx_id, role', (node_id,)
    ).fetchall()
    return [TxRef(int(r['tx_id']), r['role']) for r in rows]


def create_net_group(conn, items: list, name: str | None = None, note: str = '') -> str:
    """操作 3：新建 +2 关系（N10 / N18）。

    成员**必须同父**（Q1：不能放宽）—— 跨 M 直接拒绝并报出各自的父节点。
    「新建时必须命名」由引擎层（service）把关；这里保留 name 可选，供低层测试与初始化用。
    """
    parsed = [parse_item(i) for i in items]
    txs: list[TxRef] = []
    for p in parsed:
        if not isinstance(p, TxRef):
            raise LedgerError('NOT_A_TX', '只有交易可以是净额组成员。')
        if p.key not in {t.key for t in txs}:
            txs.append(p)
    if len(txs) < 2:
        raise LedgerError('GROUP_TOO_SMALL', '一个净额组至少要有 2 个成员。')

    by_node: dict[str, list[str]] = {}
    for t in txs:
        if _group_of(conn, t):
            raise LedgerError('ALREADY_GROUPED', f'{t} 已经属于别的净额组了。')
        by_node.setdefault(_resolve_node(conn, t), []).append(str(t))
    if len(by_node) > 1:
        detail = {'按父节点': {_node_name(conn, n): m for n, m in by_node.items()}}
        raise LedgerError(
            'SAME_PARENT_REQUIRED',
            '不能跨分组建立净额关系：' + '；'.join(
                f'「{_node_name(conn, n)}」下有 {"、".join(m)}' for n, m in by_node.items()
            ),
            detail,
        )

    node = next(iter(by_node))
    gid = uuid4().hex
    prims = [{'op': 'group_create', 'group_id': gid, 'name': name,
              'note': (note or '').strip(), 'node_id': node}]
    for t in txs:
        p = _place_tx(conn, t, node, gid)
        assert p is not None
        prims.append(p)
    _run(conn, prims, 'create_net_group')
    return gid


def add_group_members(conn, group_id: str, txs, op_name: str = 'add_group_member') -> None:
    """+2 组批量增员（N10）。每个新成员都必须与组**同父**；一次事务、一次撤销。

    数据层兜底：传进来的非交易（M / +2）一律 `NOT_A_TX` —— 引擎层的「加入不能有 M」靠它防。
    """
    g = _group(conn, group_id)
    refs: list[TxRef] = []
    seen: set[tuple[int, str]] = set()
    for raw in txs:
        t = parse_item(raw)
        if not isinstance(t, TxRef):
            raise LedgerError('NOT_A_TX', '只有交易可以是净额组成员。')
        if t.key in seen:
            continue
        seen.add(t.key)
        if _group_of(conn, t):
            raise LedgerError('ALREADY_GROUPED', f'{t} 已经属于别的净额组了。')
        node = _resolve_node(conn, t)
        if node != g['node_id']:
            raise LedgerError(
                'SAME_PARENT_REQUIRED',
                f'{t} 在「{_node_name(conn, node)}」下，组在「{_node_name(conn, g["node_id"])}」下：不能跨分组加入净额组。',
                {'tx_node': _node_name(conn, node), 'group_node': _node_name(conn, g['node_id'])},
            )
        refs.append(t)
    if not refs:
        raise LedgerError('EMPTY_SELECTION', '没有可以加入的交易。')
    prims = [p for p in (_place_tx(conn, t, g['node_id'], group_id) for t in refs) if p]
    _run(conn, prims, op_name)


def add_group_member(conn, group_id: str, tx) -> None:
    """+2 组增员（legacy 单入口）。"""
    add_group_members(conn, group_id, [tx])


def remove_group_member(conn, group_id: str, tx) -> None:
    """+2 组减员（N10）。减到不足 2 个成员时，组自动解散（R3）。"""
    t = parse_item(tx)
    if not isinstance(t, TxRef):
        raise LedgerError('NOT_A_TX', '只有交易可以是净额组成员。')
    g = conn.execute('SELECT * FROM ledger2_net_group WHERE id=?', (group_id,)).fetchone()
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
        prims.append({'op': 'group_delete', 'group_id': group_id, 'name': g['name'],
                  'note': g.get('note', ''), 'node_id': g['node_id']})
    _run(conn, prims, 'remove_group_member')


def update_node(conn, node_id: str, name: str | None = None, note: str | None = None) -> None:
    """改一个节点的名字 / 备注（不给的不动）。根节点可以改备注，但不能改名。"""
    n = _node(conn, node_id)
    new_name = n['name']
    if name is not None:
        if n['parent_id'] is None:
            raise LedgerError('CANNOT_RENAME_ROOT', '不能给总账根节点改名。')
        new_name = (name or '').strip()
        if not new_name:
            raise LedgerError('EMPTY_NAME', '分组名不能为空。')
    new_note = n['note'] if note is None else (note or '').strip()
    if new_name == n['name'] and new_note == n['note']:
        raise LedgerError('NO_EFFECT', '名字和备注都没有变化。')
    _run(conn, [{'op': 'node_update', 'node_id': node_id, 'name': new_name, 'note': new_note,
                 'old_name': n['name'], 'old_note': n['note']}], 'update_node')


def rename_node(conn, node_id: str, name: str) -> None:
    """改名（update_node 的窄版本；错误码与操作名保持不变）。"""
    n = _node(conn, node_id)
    if n['parent_id'] is None:
        raise LedgerError('CANNOT_RENAME_ROOT', '不能给总账根节点改名。')
    name = (name or '').strip()
    if not name:
        raise LedgerError('EMPTY_NAME', '分组名不能为空。')
    if name == n['name']:
        raise LedgerError('NO_EFFECT', '名字没有变化。')
    _run(conn, [{'op': 'node_update', 'node_id': node_id, 'name': name, 'note': n['note'],
                 'old_name': n['name'], 'old_note': n['note']}], 'rename_node')


def update_group(conn, group_id: str, name: str | None = None, note: str | None = None) -> None:
    """改一个 +2 组的名字 / 备注（不给的不动）。组名可以清空（读出时用 id 兜底）。"""
    g = _group(conn, group_id)
    old_name = g['name'] or ''
    old_note = g.get('note') or ''
    new_name = old_name if name is None else (name or '').strip()
    new_note = old_note if note is None else (note or '').strip()
    if new_name == old_name and new_note == old_note:
        raise LedgerError('NO_EFFECT', '名字和备注都没有变化。')
    _run(conn, [{'op': 'group_update', 'group_id': group_id, 'name': new_name, 'note': new_note,
                 'old_name': old_name, 'old_note': old_note}], 'update_group')


def rename_group(conn, group_id: str, name: str) -> None:
    """改名（update_group 的窄版本）。"""
    update_group(conn, group_id, name=name)


def dissolve_group(conn, group_id: str) -> None:
    """整组解散（legacy 单入口）：成员回到组所在的节点，不再净额。"""
    _group(conn, group_id)
    dissolve(conn, [], [group_id])


def split_txs(conn, tx_ids: list[int], op_name: str = 'split') -> list[int]:
    """批量分裂：每个 t 各生成一个 t′（落在 t 此刻所在的节点），一次事务、一次撤销。

    返回真正分裂的源交易 id（升序），前端据此保持「原来的 t + 新的 t′」选中。
    """
    ids = list(dict.fromkeys(int(i) for i in tx_ids))
    if not ids:
        raise LedgerError('EMPTY_SELECTION', '没有可以生成镜像的交易。')
    prims: list[dict] = []
    for tx_id in ids:
        if conn.execute('SELECT 1 FROM ledger2_split_tx WHERE src_tx_id=?', (tx_id,)).fetchone():
            raise LedgerError('ALREADY_SPLIT', f'交易 {tx_id} 已经生成过镜像了。')
        node = _resolve_node(conn, TxRef(tx_id, 'primary'))
        prims.append({'op': 'split_add', 'src_tx_id': tx_id})
        prims.append({'op': 'tx_place', 'tx_id': tx_id, 'role': 'mirror', 'node_id': node, 'group_id': None,
                      'had_row': False, 'old_node_id': None, 'old_group_id': None})
    _run(conn, prims, op_name)
    return ids


def split_tx(conn, tx_id: int, node_id: str | None = None) -> list[int]:
    """分裂（N19）：产生一个独立的 t′。

    - t′ 的 IE 向量刚好反过来（μ）；
    - t′ 默认落在 t 此刻所在的节点（同一路径），也可以用 node_id 指定别的位置；
    - t′ **不继承** t 的 +2 关系；
    - 同一个 t 只能分裂一次（ledger2_split_tx 主键保证）。
    """
    exists = conn.execute('SELECT 1 FROM ledger2_split_tx WHERE src_tx_id=?', (tx_id,)).fetchone()
    if exists:
        raise LedgerError('ALREADY_SPLIT', '这笔交易已经生成过镜像了。')
    src = TxRef(int(tx_id), 'primary')
    node = _resolve_node(conn, src)             # 不存在 / 已删除 / 未初始化 → 报错
    if node_id is not None:
        node = _node(conn, str(node_id))['id']
    prims = [{'op': 'split_add', 'src_tx_id': int(tx_id)},
             {'op': 'tx_place', 'tx_id': int(tx_id), 'role': 'mirror', 'node_id': node, 'group_id': None,
              'had_row': False, 'old_node_id': None, 'old_group_id': None}]
    _run(conn, prims, 'split_tx')
    return [int(tx_id)]


def unsplit_txs(conn, tx_ids: list[int], op_name: str = 'unsplit') -> list[int]:
    """批量取消分裂：砍掉这些 t 的 t′，t 原样保留；组不足 2 人自动解散（R3）。

    返回真正被取消分裂的源交易 id（升序），前端据此把选中的 t′ 换成原型 t。
    """
    ids = list(dict.fromkeys(int(i) for i in tx_ids))
    if not ids:
        raise LedgerError('EMPTY_SELECTION', '没有可以取消镜像的交易。')
    removed: set[tuple[int, str]] = set()
    prims: list[dict] = []
    for tx_id in ids:
        if not conn.execute('SELECT 1 FROM ledger2_split_tx WHERE src_tx_id=?', (tx_id,)).fetchone():
            raise LedgerError('NOT_SPLIT', f'交易 {tx_id} 当前没有镜像。')
        mirror = TxRef(tx_id, 'mirror')
        removed.add(mirror.key)
        p = _prim_tx_unplace(conn, mirror)
        if p:
            prims.append(p)

    # 组解散要按「整批移除之后」的成员数算，否则同一组里连着删两个会算错。
    affected: dict[str, dict] = {}
    for key in removed:
        g = _group_of(conn, TxRef(key[0], key[1]))
        if g:
            affected[g['id']] = g
    for gid, g in affected.items():
        remaining = [m for m in _members(conn, gid) if m.key not in removed]
        if len(remaining) < 2:
            for m in remaining:
                q = _place_tx(conn, m, g['node_id'], None)
                if q:
                    prims.append(q)
            prims.append({'op': 'group_delete', 'group_id': gid, 'name': g['name'],
                          'note': g.get('note', ''), 'node_id': g['node_id']})
    for tx_id in ids:
        prims.append({'op': 'split_remove', 'src_tx_id': tx_id})
    _run(conn, prims, op_name)
    return ids


def unsplit_tx(conn, tx_id: int) -> list[int]:
    """取消分裂（legacy 单入口）：只砍掉 t′，t 原样保留（N19）。"""
    return unsplit_txs(conn, [int(tx_id)], 'unsplit_tx')


# ── 初始化 / 卸载 ───────────────────────────────────────────────────────────
#
# 初始化 = 让一批交易「存在于总账这棵树里」：写归属行 + 写人工字段（打初始化标记）。
# 没初始化的交易在所有读出里直接被过滤。卸载是它的反操作。两者都走原语 + 逆序，可撤销。

def _norm_ref(raw) -> TxRef:
    ref = TxRef(int(raw), 'primary') if isinstance(raw, int) else parse_item(raw)
    if not isinstance(ref, TxRef):
        raise LedgerError('NOT_A_TX', f'「{raw}」不是交易。')
    return ref


def _node_path(conn, node_id: str, planned: dict[str, dict]) -> list[str]:
    """根以下各级节点名（不含根）。planned = 这次 init 要新建、还没落库的节点 {id: {name, parent_id}}。"""
    names: list[str] = []
    cur: str | None = node_id
    while cur is not None:
        if cur in planned:
            names.append(planned[cur]['name'])
            cur = planned[cur]['parent_id']
            continue
        row = _node(conn, cur)
        if row['parent_id'] is None:
            break
        names.append(row['name'])
        cur = row['parent_id']
    return list(reversed(names))


def init_txs(conn, tx_ids: list, op: dict | None = None) -> dict:
    """[t1..tn; op]_init —— 初始化一批交易。整个操作原子：任何一项不合法就整体不生效。

    op（全部可选）::

        {
          'nodes':  [{'ref': 'a', 'name': '餐饮', 'parent': 'root'|已有节点id|前面的 ref, 'id': 可指定新节点 id,
                      'note': 节点备注（可选）}],
          'place':  [{'node': 已有节点id|ref, 'txs': [tx_id, ...]}],     # 没提到的交易放根下
          'splits': [{'tx': tx_id, 'node': 节点id|ref|None}],            # 默认分到同一节点（同一路径）
          'groups': [{'name': '...', 'txs': [tx_id | 'tx:5' | 'txm:5', ...]}],  # 成员须在同一节点
          'meta':   {tx_id: {'title': '...', 'note': '...'}},            # 标题 / 备注，不给就是空
        }

    执行顺序固定：建节点 → 放置 → 分裂 → 组 +2（所以组成员可以是刚分出来的 t′，并在放置/分裂之后检查同父）。
    返回 {'nodes': {ref: 节点id}, 'groups': [组id, ...]}。
    """
    op = op or {}
    ids = [int(raw) for raw in tx_ids]
    if not ids:
        raise LedgerError('EMPTY_SELECTION', '没有要初始化的交易。')
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise LedgerError('DUPLICATE_TX', f'交易列表里有重复：{dup}。', {'tx_ids': dup})
    id_set = set(ids)
    for i in ids:
        facts.category_of(conn, i)               # 不存在 / 已删除 → TX_NOT_FOUND
        if facts.is_initialized(conn, i):
            raise LedgerError('ALREADY_INITIALIZED', f'交易 {i} 已经初始化过了。', {'tx_id': i})

    root = root_id(conn)
    prims: list[dict] = []

    # 1. 建节点（父节点必须是已有节点或前面已定义的 ref）
    refs: dict[str, str] = {}
    planned: dict[str, dict] = {}

    def node_of(token, what: str) -> str:
        token = str(token)
        if token in refs:
            return refs[token]
        if token in planned or conn.execute('SELECT 1 FROM ledger2_node WHERE id=?', (token,)).fetchone():
            return token
        raise LedgerError('NODE_NOT_FOUND', f'{what}指向的节点「{token}」既不是已有节点，也不是前面定义的 ref。')

    for spec in op.get('nodes') or []:
        ref = str(spec.get('ref') or '')
        name = (spec.get('name') or '').strip()
        if not ref or ref in refs:
            raise LedgerError('BAD_OP', f'新节点的 ref 不能为空或重复：「{ref}」。')
        if not name:
            raise LedgerError('EMPTY_NAME', '新节点的名字不能为空。')
        parent = node_of(spec.get('parent', root), f'新节点「{name}」的父节点')
        nid = str(spec.get('id') or uuid4().hex)
        if conn.execute('SELECT 1 FROM ledger2_node WHERE id=?', (nid,)).fetchone() or nid in planned:
            raise LedgerError('NODE_EXISTS', f'节点 id「{nid}」已经存在。')
        note = (spec.get('note') or '').strip()
        refs[ref] = nid
        planned[nid] = {'name': name, 'parent_id': parent, 'note': note}
        prims.append({'op': 'node_create', 'node_id': nid, 'name': name,
                      'parent_id': parent, 'note': note})

    # 2. 放置
    where: dict[tuple[int, str], str] = {}
    for spec in op.get('place') or []:
        target = node_of(spec.get('node'), '放置')
        for raw in spec.get('txs') or []:
            t = int(raw)
            if t not in id_set:
                raise LedgerError('NOT_IN_INIT_SET', f'放置里的交易 {t} 不在这次要初始化的列表里。', {'tx_id': t})
            if (t, 'primary') in where:
                raise LedgerError('DUPLICATE_PLACE', f'交易 {t} 被放置了两次。', {'tx_id': t})
            where[(t, 'primary')] = target
    for t in ids:
        where.setdefault((t, 'primary'), root)

    # 3. 分裂（t′ 默认跟 t 同一节点，也可指定）
    split_ids: list[int] = []
    for spec in op.get('splits') or []:
        t = int(spec['tx'])
        if t not in id_set:
            raise LedgerError('NOT_IN_INIT_SET', f'生成镜像里的交易 {t} 不在这次要初始化的列表里。', {'tx_id': t})
        if t in split_ids:
            raise LedgerError('ALREADY_SPLIT', f'交易 {t} 被生成了两次镜像。', {'tx_id': t})
        target = spec.get('node')
        where[(t, 'mirror')] = node_of(target, '生成镜像') if target else where[(t, 'primary')]
        split_ids.append(t)

    # 4. 组 +2（放置与分裂之后检查同父；成员可以是 t′）
    def label(n: str) -> str:
        return planned[n]['name'] if n in planned else _node_name(conn, n)

    grouped: dict[tuple[int, str], str] = {}
    gprims: list[dict] = []
    group_ids: list[str] = []
    for spec in op.get('groups') or []:
        members: list[TxRef] = []
        for raw in spec.get('txs') or []:
            ref = _norm_ref(raw)
            if ref.key not in {m.key for m in members}:
                members.append(ref)
        if len(members) < 2:
            raise LedgerError('GROUP_TOO_SMALL', '一个净额组至少要有 2 个成员。')
        by_node: dict[str, list[str]] = {}
        for m in members:
            if m.key not in where:
                raise LedgerError('NOT_IN_INIT_SET', f'净额组成员 {m} 不在这次初始化的范围内（镜像交易要先在 splits 里生成）。')
            if m.key in grouped:
                raise LedgerError('ALREADY_GROUPED', f'{m} 已经属于别的净额组了。')
            by_node.setdefault(where[m.key], []).append(str(m))
        if len(by_node) > 1:
            raise LedgerError(
                'SAME_PARENT_REQUIRED',
                '不能跨分组建立净额关系：' + '；'.join(
                    f'「{label(n)}」下有 {"、".join(v)}' for n, v in by_node.items()),
                {'按父节点': {label(n): v for n, v in by_node.items()}},
            )
        node = next(iter(by_node))
        gid = uuid4().hex
        group_ids.append(gid)
        gprims.append({'op': 'group_create', 'group_id': gid, 'name': spec.get('name'),
                       'note': (spec.get('note') or '').strip(), 'node_id': node})
        for m in members:
            grouped[m.key] = gid

    # 5. 人工字段
    meta = {int(k): v for k, v in (op.get('meta') or {}).items()}
    for t in meta:
        if t not in id_set:
            raise LedgerError('NOT_IN_INIT_SET', f'meta 里的交易 {t} 不在这次要初始化的列表里。', {'tx_id': t})

    prims += gprims
    for t in split_ids:
        prims.append({'op': 'split_add', 'src_tx_id': t})
    for key, node in where.items():
        prims.append({'op': 'tx_place', 'tx_id': key[0], 'role': key[1], 'node_id': node,
                      'group_id': grouped.get(key), 'had_row': False, 'old_node_id': None, 'old_group_id': None})
    for t in ids:
        m = meta.get(t, {})
        prims.append({
            'op': 'manual_set', 'tx_id': t, 'old': None,
            'new': {'title': (m.get('title') or '').strip(), 'note': (m.get('note') or '').strip(),
                    'category_path': _node_path(conn, where[(t, 'primary')], planned)},
        })
    _run(conn, prims, 'init')
    return {'nodes': refs, 'groups': group_ids}


def uninst_txs(conn, tx_ids: list) -> None:
    """[t1..tn]_uninst —— 卸载：注销这些交易的 4 个初始化字段，并把它们从树里拿走。

    连带：它们的 t′（分裂）一并撤掉；所在 +2 组少于 2 人时自动解散（R3）。可撤销。
    """
    ids = [int(i) for i in tx_ids]
    if not ids:
        raise LedgerError('EMPTY_SELECTION', '没有要卸载的交易。')
    if len(set(ids)) != len(ids):
        raise LedgerError('DUPLICATE_TX', '交易列表里有重复。')
    for i in ids:
        row = conn.execute(
            "SELECT 1 FROM ledger2_tx WHERE tx_id=? AND role='primary' AND initialized=1", (i,)
        ).fetchone()
        if row is None:
            raise LedgerError('NOT_INITIALIZED', f'交易 {i} 还没有初始化，无法卸载。', {'tx_id': i})

    gone = {(i, role) for i in ids for role in ('primary', 'mirror')}
    rows = {}
    for i in ids:
        for role in ('primary', 'mirror'):
            r = _placement(conn, i, role)
            if r:
                rows[(i, role)] = r

    prims: list[dict] = []
    dissolved: list[dict] = []
    for gid in sorted({r['net_group_id'] for r in rows.values() if r['net_group_id']}):
        g = dict(conn.execute('SELECT * FROM ledger2_net_group WHERE id=?', (gid,)).fetchone())
        remaining = [m for m in _members(conn, gid) if m.key not in gone]
        if len(remaining) < 2:                     # R3：不足 2 人 → 整组解散，剩下的人变回普通成员
            for m in remaining:
                q = _place_tx(conn, m, g['node_id'], None)
                if q:
                    prims.append(q)
            dissolved.append({'op': 'group_delete', 'group_id': gid, 'name': g['name'], 'node_id': g['node_id']})
    for key, r in rows.items():
        prims.append({'op': 'tx_unplace', 'tx_id': key[0], 'role': key[1],
                      'node_id': r['node_id'], 'group_id': r['net_group_id'], 'old_row': r})
    prims += dissolved
    for i in ids:
        if conn.execute('SELECT 1 FROM ledger2_split_tx WHERE src_tx_id=?', (i,)).fetchone():
            prims.append({'op': 'split_remove', 'src_tx_id': i})
    _run(conn, prims, 'uninst')


def update_manual(conn, tx_id: int, title: str | None = None, note: str | None = None) -> None:
    """改一笔已初始化交易的标题 / 备注（不给的不动）。category_path 不在这里改。"""
    row = conn.execute(
        "SELECT * FROM ledger2_tx WHERE tx_id=? AND role='primary' AND initialized=1", (int(tx_id),)
    ).fetchone()
    if row is None:
        raise LedgerError('NOT_INITIALIZED', f'交易 {tx_id} 还没有初始化。', {'tx_id': int(tx_id)})
    old = {'title': row['title'], 'note': row['note'], 'category_path': json.loads(row['category_path'])}
    new = dict(old)
    if title is not None:
        new['title'] = title.strip()
    if note is not None:
        new['note'] = note.strip()
    if new == old:
        raise LedgerError('NO_EFFECT', '标题和备注都没有变化。')
    _run(conn, [{'op': 'manual_set', 'tx_id': int(tx_id), 'old': old, 'new': new}], 'update_manual')


# ── 撤销 / 重做 ─────────────────────────────────────────────────────────────

def undo(conn) -> str:
    """撤销最后一次操作：执行它的逆原语序列。"""
    with atomic(conn):
        row = conn.execute(
            "SELECT * FROM ledger2_op_log WHERE state='applied' ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise LedgerError('NOTHING_TO_UNDO', '没有可以撤销的操作。')
        for p in json.loads(row['undo_json']):
            _apply(conn, p)
        invariants.check(conn)
        conn.execute("UPDATE ledger2_op_log SET state='undone' WHERE seq=?", (row['seq'],))
        _record_event(conn, row['seq'], 'undo')
    return json.loads(row['op_json'])['name']


def redo(conn) -> str:
    """重做：把最后一次撤销的操作再执行一遍。"""
    with atomic(conn):
        row = conn.execute(
            "SELECT * FROM ledger2_op_log WHERE state='undone' ORDER BY seq ASC LIMIT 1"
        ).fetchone()
        if row is None:
            raise LedgerError('NOTHING_TO_REDO', '没有可以重做的操作。')
        op = json.loads(row['op_json'])
        for p in op['prims']:
            _apply(conn, p)
        invariants.check(conn)
        conn.execute("UPDATE ledger2_op_log SET state='applied' WHERE seq=?", (row['seq'],))
        _record_event(conn, row['seq'], 'redo')
    return op['name']


def history(conn) -> list[dict]:
    """操作日志（给人看）。"""
    out = []
    for r in conn.execute('SELECT * FROM ledger2_op_log ORDER BY seq'):
        op = json.loads(r['op_json'])
        out.append({'seq': r['seq'], 'name': op['name'], 'state': r['state'],
                    'prims': len(op['prims']), 'at': r['created_at']})
    return out
