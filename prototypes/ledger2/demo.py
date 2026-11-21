"""账本代数 V2 · 跑一遍。

几组简单数据，把每个能力跑出来给人看：
  A 基础账本（无 +2、无树）→ 零写入，读出 = [[t1],[t2],…]+1
  B 树 + 移动 + +2 组       → 折叠、同父约束、净额效果
  C 时间范围                 → 范围内没有交易的分类整个不出现
  D 分裂 + 撤销/重做         → t′ 的向量反过来；操作可逆

用法：python -m prototypes.ledger2.demo
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from prototypes.ledger2 import (
    LedgerError, init_db, seed_transactions, read_ledger, read_scope, scope_by_date, render, to_json,
    create_node, move, dissolve_node, create_net_group, add_group_member, remove_group_member,
    split_tx, unsplit_tx, undo, redo, history,
)

# ── 虚构数据（金额单位：分）─────────────────────────────────────────────────
DATA = [
    dict(id=1, tx_time='2026-01-05 10:12:00', date='2026-01-05', direction=-1, amount=3000,
         category='餐饮', counterparty='示例小店', item='午饭', source='demo'),
    dict(id=2, tx_time='2026-01-08 09:00:00', date='2026-01-08', direction=1, amount=800000,
         category='工资', counterparty='示例公司', item='月薪', source='demo'),
    dict(id=3, tx_time='2026-01-09 19:30:00', date='2026-01-09', direction=-1, amount=2000,
         category='餐饮', counterparty='示例小店', item='晚饭', source='demo'),
    dict(id=4, tx_time='2026-02-02 12:05:00', date='2026-02-02', direction=-1, amount=2500,
         category='餐饮', counterparty='示例小店', item='外卖', source='demo'),
    dict(id=5, tx_time='2026-02-03 08:10:00', date='2026-02-03', direction=-1, amount=10000,
         category='交通', counterparty='示例地铁', item='月票', source='demo'),
    dict(id=6, tx_time='2026-01-20 21:00:00', date='2026-01-20', direction=1, amount=5000,
         category='退款', counterparty='示例商家', item='退款到账', source='demo'),
    dict(id=7, tx_time='2026-03-05 15:00:00', date='2026-03-05', direction=-1, amount=20000,
         category='购物', counterparty='示例网店', item='日用品', source='demo'),
]


def hr(title: str) -> None:
    print('\n' + '=' * 78)
    print(title)
    print('=' * 78)


def show(conn, title: str, scope=None) -> None:
    root = read_ledger(conn)
    tree = read_scope(root, scope) if scope is not None else root
    print(f'\n--- {title} ---')
    print(render(tree))


def main() -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix='ledger2_demo_'))
    db = tmpdir / 'demo.db'
    conn = init_db(db)
    seed_transactions(conn, DATA)

    # ── A 基础账本 ──────────────────────────────────────────────────────────
    hr('A · 基础账本：没有 +2、没有树 —— 一行都不写')
    root = read_ledger(conn)
    print(render(root))
    writes = conn.execute('SELECT COUNT(*) FROM ledger_placement').fetchone()[0]
    groups = conn.execute('SELECT COUNT(*) FROM ledger_net_group').fetchone()[0]
    print(f'\n[检查] ledger_placement 行数 = {writes}（应为 0）')
    print(f'[检查] ledger_net_group 行数 = {groups}（应为 0）')
    print('[检查] 根值 = 所有交易 +1：e=支出 375.00，i=收入 8050.00')

    # ── B 树 + +2 ───────────────────────────────────────────────────────────
    hr('B · 建树、移动、+2 净额组')
    food = create_node(conn, '餐饮', ['tx:1', 'tx:3', 'tx:4'])
    transport = create_node(conn, '交通', ['tx:5'])
    shopping = create_node(conn, '购物', ['tx:7'])
    salary = create_node(conn, '工资', ['tx:2'])
    print(f'[建] 餐饮 / 交通 / 购物 / 工资 四个分组已建立')
    show(conn, 'B1 · 建完树')

    print('\n[拒绝] 试着把「餐饮的 t1」和「工资的 t2」放进同一个 +2 组：')
    try:
        create_net_group(conn, ['tx:1', 'tx:2'], '跨组净额')
        print('  !! 不该成功')
    except LedgerError as e:
        print(f'  code={e.code}  {e.message}')

    print('\n[移动] 把 t6（退款到账 50.00）挪进「餐饮」，好让它和 t1 同父：')
    move(conn, ['tx:6'], food)
    show(conn, 'B2 · t6 已挪进餐饮')

    print('\n[建组] t1（支出 30.00）+ t6（收入 50.00）→ ν：两边抵消后 = 净收入 20.00：')
    g = create_net_group(conn, ['tx:1', 'tx:6'], '餐饮-内部对冲')
    show(conn, 'B3 · 组建立后（组里的交易不再作为节点的直接子项出现）')

    print('\n[加成员] 把 t3（支出 20.00）也加进这个组 —— 支出 30+20=50 与收入 50 完全抵消：')
    add_group_member(conn, g, 'tx:3')
    show(conn, 'B4 · 组里多了一个成员')

    print('\n[减成员] 把 t3 移出组（组里还剩 2 个，不解散；ν 又变回净收入 20.00）：')
    remove_group_member(conn, g, 'tx:3')
    show(conn, 'B5 · t3 已移出组')

    print('\n[JSON] 读出结构 —— 将来 HTTP / MCP 返回的就是这个形状：')
    print(json.dumps(to_json(read_ledger(conn)), ensure_ascii=False, indent=2))

    # ── C 时间范围 ──────────────────────────────────────────────────────────
    hr('C · 时间范围：范围内没有交易的分类，整个不出现')
    scope = scope_by_date(conn, '2026-01-01', '2026-01-31')
    print(f'[范围] 2026-01-01 ~ 2026-01-31，命中 {sorted(scope.tx_ids)}')
    show(conn, 'C1 · 只看一月（交通/购物 不出现；餐饮只剩一月里的 t1、t3）', scope)

    scope2 = scope_by_date(conn, '2026-02-01', '2026-02-28')
    print(f'\n[范围] 2026-02-01 ~ 2026-02-28，命中 {sorted(scope2.tx_ids)}')
    show(conn, 'C2 · 只看二月（餐饮只剩 t4，t1/t6 都不在范围内）', scope2)

    # ── D 分裂 + 撤销/重做 ─────────────────────────────────────────────────
    hr('D · 分裂 t′（向量刚好反过来）+ 撤销 / 重做')
    print('[分裂] t3（支出 20.00）→ t\'3（收入 20.00），继承路径、不继承组：')
    split_tx(conn, 3)
    show(conn, 'D1 · 分裂后')

    print('\n[撤销] 撤销这次分裂：')
    undo(conn)
    show(conn, 'D2 · 撤销后（t\'3 消失，t3 原样保留）')

    print('\n[重做] 再做回来：')
    redo(conn)
    show(conn, 'D3 · 重做后')

    print('\n[撤销 ×N] 连续撤销，把账本退回最初的样子：')
    while True:
        try:
            name = undo(conn)
            print(f'  撤销：{name}')
        except LedgerError:
            break
    show(conn, 'D4 · 全部撤销后（回到 A 的样子）')

    print('\n[操作日志]')
    for h in history(conn):
        print(f"  #{h['seq']:<3} {h['name']:<22} {h['state']:<8} 原语 {h['prims']} 条  {h['at']}")

    conn.close()
    print(f'\n（演示库：{db}）')


if __name__ == '__main__':
    main()
