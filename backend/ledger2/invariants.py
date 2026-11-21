"""写后自检：返回违规列表；空 = 健康。ops 在事务里调用，不过就回滚。"""
from __future__ import annotations

from .errors import LedgerError


def violations(conn) -> list[str]:
    out: list[str] = []
    roots = conn.execute('SELECT id FROM ledger2_node WHERE parent_id IS NULL').fetchall()
    if len(roots) != 1:
        out.append(f'根节点数量应为 1，实际 {len(roots)}')

    parent = {r['id']: r['parent_id'] for r in conn.execute('SELECT id, parent_id FROM ledger2_node')}
    for nid in parent:
        seen, cur = set(), nid
        while cur is not None:
            if cur in seen:
                out.append(f'节点 {nid} 的父链成环')
                break
            seen.add(cur)
            cur = parent.get(cur)

    for g in conn.execute('SELECT id, name, node_id FROM ledger2_net_group'):
        members = conn.execute(
            'SELECT node_id FROM ledger2_tx WHERE net_group_id=?', (g['id'],)).fetchall()
        label = g['name'] or g['id'][:8]
        if len(members) < 2:
            out.append(f'净额组「{label}」只有 {len(members)} 个成员')
        if any(m['node_id'] != g['node_id'] for m in members):
            out.append(f'净额组「{label}」的成员与组不同父')

    for r in conn.execute(
            "SELECT p.tx_id FROM ledger2_tx p WHERE p.role='mirror' "
            "AND NOT EXISTS (SELECT 1 FROM ledger2_split_tx s WHERE s.src_tx_id=p.tx_id)"):
        out.append(f'交易 {r["tx_id"]} 有镜像交易行但缺少镜像记录')

    # 初始化：一行 = 已初始化（正常状态 initialized 恒为 1；0 只可能是坏数据）。
    roles = {'primary': '原始交易', 'mirror': '镜像交易'}
    for r in conn.execute("SELECT tx_id, role FROM ledger2_tx WHERE initialized != 1"):
        out.append(f'交易 {r["tx_id"]}（{roles.get(r["role"], r["role"])}）的行标成了未初始化')
    for r in conn.execute(
            "SELECT s.src_tx_id FROM ledger2_split_tx s WHERE NOT EXISTS "
            "(SELECT 1 FROM ledger2_tx p WHERE p.tx_id=s.src_tx_id AND p.role='mirror')"):
        out.append(f'交易 {r["src_tx_id"]} 有镜像记录但没有对应的镜像交易行')
    return out


def check(conn) -> None:
    found = violations(conn)
    if found:
        raise LedgerError('INVARIANT_VIOLATION', '账本结构自检未通过：' + '；'.join(found), {'violations': found})
