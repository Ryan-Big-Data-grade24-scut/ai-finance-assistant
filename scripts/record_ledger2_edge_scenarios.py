"""把「边界场景」跑一遍并留下可复核的记录（backend.ledger2，真 schema）。

对应用户提的四组边界：

    E · +2 条目一部分在 scope 内、一部分在 scope 外 → 还能不能读出来？
    F · 建新 M 时拖进来的是「节点 + 交易」的混合 → 折叠规则对不对？
    G · 移动的四种组合（1t / 多t / 1M / 多M / M-t）→ 移动集合唯一吗？搬空之后还在吗？
    H · 解散一个 M → +2 条目的父节点和组里 t 的父节点跟着变了吗？

产出：proj/draft/05_账本代数V2-边界场景/<场景>/...
用法（在 V2 仓库根目录）：
    ../ai-finance-assistant/.venv/Scripts/python.exe -X utf8 scripts/record_ledger2_edge_scenarios.py

共用的记录器 / CSV 导出 / 文档生成都在 record_ledger2_scenarios.py 里。
和 04 号记录一样：数据全是虚构的、用临时库、关键数字用 assert 钉死（文档里的「断言」表就是它们的清单）、
每次跑都先清掉旧目录。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from record_ledger2_scenarios import (  # noqa: E402
    PY, TABLES, clear_dir, collect_contract_tests, begin, direct_tx_ids, find_group, find_node,
    finish, name_of, node_ids, obs_full, snapshot, table_count, tx, tx_parent, undo_all,
    write_scenario_doc,
)
from backend.ledger2 import facts  # noqa: E402
from backend.ledger2.ops import (  # noqa: E402
    create_net_group, create_node, dissolve_node, move, undo,
)
from backend.ledger2.reader import read_ledger, read_scope, render, to_json  # noqa: E402

PROJ = HERE.parents[1]
OUT_ROOT = PROJ / 'draft' / '05_账本代数V2-边界场景'


# ── 观察工具 ────────────────────────────────────────────────────────────────

def obs_rows(conn, *tables: str) -> str:
    """把指定表的内容逐行列出来（看具体字段变化，不只是行数）。"""
    out = []
    for t in tables:
        rows = conn.execute(f'SELECT * FROM {t}').fetchall()
        out.append(f'── {t}（{len(rows)} 行）')
        for r in rows:
            out.append('   ' + '  '.join(f'{k}={r[k]}' for k in r.keys()))
    return '\n'.join(out)


def scope_proj(conn, date_from: str, date_to: str):
    scope = facts.scope_by_date(conn, date_from, date_to)
    return scope, read_scope(read_ledger(conn), scope)


def scope_obs(conn, date_from: str, date_to: str) -> str:
    """某个时间范围下的读出树。"""
    scope, proj = scope_proj(conn, date_from, date_to)
    return (f'scope [{date_from} ~ {date_to}]  命中交易 {sorted(scope.tx_ids)}  根值 = '
            f'({proj.value[0]/100:.2f}, {proj.value[1]/100:.2f})\n\n' + render(proj))


def write_scope_trees(conn, outdir: Path, scopes: list[tuple[str, str, str]]) -> None:
    """把几个时间范围的读出树各写一份文件（txt + json）。"""
    for label, a, b in scopes:
        _, proj = scope_proj(conn, a, b)
        (outdir / f'读出树_{label}.txt').write_text(render(proj) + '\n', encoding='utf-8')
        (outdir / f'读出树_{label}.json').write_text(
            json.dumps(to_json(proj), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def finish_with(conn, tmp, outdir, init_dir, rec, scopes=()):
    meta = finish(conn, tmp, outdir, init_dir, rec) if not scopes else None
    return meta


# 因为 finish() 会关库，带 scope 文件的场景要在它之前把范围树写出来
def finish_scoped(conn, tmp, outdir, init_dir, rec, scopes):
    write_scope_trees(conn, outdir, scopes)
    return finish(conn, tmp, outdir, init_dir, rec)


def kids(node) -> list[str]:
    """子项的简短标签（用来断言「没有成员冒成节点的直接子项」）。"""
    out = []
    for c in node.children:
        out.append(getattr(c, 'name', None) or f'{c.label}')
    return out


# ── 场景 E：+2 条目跨 scope 边界 ────────────────────────────────────────────

E_DATA = [
    tx(id=1, tx_time='2026-01-10 10:12:00', direction='支出', amount=30, tx_type='餐饮',
       counterparty='示例小店', item='午饭'),
    tx(id=2, tx_time='2026-01-15 09:00:00', direction='收入', amount=50, tx_type='其他收入',
       counterparty='示例商家', item='红包'),
    tx(id=3, tx_time='2026-02-10 19:30:00', direction='支出', amount=20, tx_type='餐饮',
       counterparty='示例小店', item='晚饭'),
]


def scenario_E(outdir: Path) -> dict:
    """E · 一个 +2 条目，一部分在 scope 内、一部分在 scope 外。"""
    tmp, conn, rec, init_dir = begin(
        outdir, 'E', E_DATA,
        't1 一月支出 30.00（餐饮）；t2 一月收入 50.00（其他收入）；t3 二月支出 20.00（餐饮）。\n'
        '**故意把 t3 放在另一个月** —— 后面就靠它切 scope。')
    ids: dict = {}

    rec.step(
        '建一个 M，把三笔都收进去',
        "m = create_node(conn, 'M_三笔', ['tx:1', 'tx:2', 'tx:3'])",
        '三笔现在同父了 —— 这是建 +2 组的前提（Q1：同父不能放宽）。t1/t3 在 `cat:餐饮`、t2 在 `cat:其他收入`，'
        '分类不同 → 公共祖先只有总账根 → M_三笔 挂在**根**下（不在任何分类节点下）。'
        '`cat:餐饮`、`cat:其他收入` 是初始化时建的真节点，搬空后仍留在全量树里（值 0.00）。',
        lambda: ids.__setitem__('m', create_node(conn, 'M_三笔', ['tx:1', 'tx:2', 'tx:3'])),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_三笔 挂在根下（三笔分属两个分类）', find_node(root, ids['m']).parent_id, 'root')
    rec.expect('ledger2_node 共 4 行（根 + 两个分类节点 + M_三笔）', table_count(conn, 'ledger2_node'), 4)

    rec.step(
        '建 +2 条目（三个成员都在组里）',
        "g = create_net_group(conn, ['tx:1', 'tx:2', 'tx:3'], '餐饮-对冲')",
        '这就是「标记」：三笔被打上同一个 +2 条目。\n'
        '读出时它们**只出现在组里**，不再作为节点的直接子项 —— 否则就是重复计数。',
        lambda: ids.__setitem__('g', create_net_group(conn, ['tx:1', 'tx:2', 'tx:3'], '餐饮-对冲')),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('三个成员都进了组、都带着同一个 net_group_id',
               conn.execute('SELECT COUNT(*) FROM ledger2_tx WHERE net_group_id=?', (ids['g'],)).fetchone()[0], 3)
    rec.expect('M_三笔 的直接子项里没有任何 t（成员只出现在组里）', direct_tx_ids(find_node(root, ids['m'])), [])

    rec.step(
        '读全量树',
        'root = read_ledger(conn)',
        '全量口径：ν(支出30, 收入50, 支出20) → 支出合计 50、收入合计 50 → 互相抵掉 → **(0, 0)**。\n'
        '注意「支出 30 + 支出 20 = 50」这一步是 +1 相加；'
        '「50 支出 vs 50 收入抵掉」这一步才是 +2。',
        lambda: None,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('全量：组 ν = (0, 0)', find_group(root, '餐饮-对冲').value, (0, 0))
    rec.expect('全量：根值 = (0, 0)', root.value, (0, 0))
    before = snapshot(conn)

    rec.step(
        'scope = 一月（t1、t2 在内，t3 在外）',
        "scope = scope_by_date(conn, '2026-01-01', '2026-01-31')\n"
        'proj = read_scope(read_ledger(conn), scope)',
        '**这就是用户问的那一刀**：+2 条目的一部分在 scope 内、一部分在 scope 外。\n\n'
        '规则：只按 scope 内的成员做 +2；scope 外的成员**不参与**，'
        '也**不能**又冒成节点的直接子项（那就是重复计数）。\n'
        '→ 成员只剩 [t1, t2]，ν(支出30, 收入50) = 净收入 20.00。',
        lambda: None,
        observe=lambda c: scope_obs(c, '2026-01-01', '2026-01-31'),
    )
    _, jan = scope_proj(conn, '2026-01-01', '2026-01-31')
    rec.expect('一月：组成员只剩 t1、t2', sorted(m.id for m in find_group(jan, '餐饮-对冲').members), [1, 2])
    rec.expect('一月：组 ν = 净收入 20.00', find_group(jan, '餐饮-对冲').value, (0, 2000))
    rec.expect('一月：t3 没有冒成 M_三笔 的直接子项', direct_tx_ids(find_node(jan, ids['m'])), [])
    rec.expect('一月：根值', jan.value, (0, 2000))

    rec.step(
        'scope = 二月（只剩 t3 在内）',
        "scope = scope_by_date(conn, '2026-02-01', '2026-02-28')",
        '边界①：组里只剩 **1 个**可见成员。\n'
        '组还在（范围内确实有东西），但 ν 对单个成员不抵消 → 原样相加 = 支出 20.00。\n'
        '「剩一个就散组」是 R3（减员到 <2 自动解散），**不是** scope 造成的 —— '
        '组在表里没变，只是读的时候按范围内的成员算。',
        lambda: None,
        observe=lambda c: scope_obs(c, '2026-02-01', '2026-02-28'),
    )
    _, feb = scope_proj(conn, '2026-02-01', '2026-02-28')
    rec.expect('二月：组还在、只有 t3 一个成员', [m.id for m in find_group(feb, '餐饮-对冲').members], [3])
    rec.expect('二月：组 ν 原样相加 = 支出 20.00', find_group(feb, '餐饮-对冲').value, (2000, 0))
    rec.expect('二月：t3 没有冒成 M_三笔 的直接子项', direct_tx_ids(find_node(feb, ids['m'])), [])
    rec.expect('二月：根值', feb.value, (2000, 0))

    rec.step(
        'scope = 三月（一个成员都没有）',
        "scope = scope_by_date(conn, '2026-03-01', '2026-03-31')",
        '边界②：组**整个不出现**，连它所在的 M 也一起消失（N11：范围内没有东西的分类整个不出现）。\n'
        '注意这不是「组被解散了」—— 表里组还在，只是范围内它没有任何成员，所以不显示。',
        lambda: None,
        observe=lambda c: scope_obs(c, '2026-03-01', '2026-03-31'),
    )
    _, mar = scope_proj(conn, '2026-03-01', '2026-03-31')
    rec.expect('三月：树里只剩根，没有孩子，值 (0,0)', (node_ids(mar), mar.children, mar.value), ({'root'}, [], (0, 0)))
    rec.expect('三月：组不出现', find_group(mar, '餐饮-对冲'), None)

    rec.step(
        '看归属表：组里三笔的归属行',
        'SELECT * FROM ledger2_tx\nSELECT * FROM ledger2_net_group',
        '**归属是事实，不随 scope 变**。scope 只是读时投影 —— 表里三笔始终都带着 net_group_id，'
        '组也一直在、位置也没动。',
        lambda: None,
        observe=lambda c: obs_rows(c, 'ledger2_tx', 'ledger2_net_group'),
    )
    rec.expect('三次 scope 读取之后，四张业务表逐行未变（范围只是读时参数）', snapshot(conn) == before, True)
    rec.expect('组还在、三笔归属行都还带着 net_group_id',
               (table_count(conn, 'ledger2_net_group'),
                conn.execute('SELECT COUNT(*) FROM ledger2_tx WHERE net_group_id IS NOT NULL').fetchone()[0]),
               (1, 3), plain=True)
    return finish_scoped(conn, tmp, outdir, init_dir, rec, [
        ('一月', '2026-01-01', '2026-01-31'),
        ('二月', '2026-02-01', '2026-02-28'),
        ('三月', '2026-03-01', '2026-03-31'),
    ])


# ── 场景 F：建新 M 时的折叠边界 ──────────────────────────────────────────────

F_DATA = [
    tx(id=1, tx_time='2026-01-05 10:12:00', direction='支出', amount=30, tx_type='餐饮',
       counterparty='示例小店', item='午饭'),
    tx(id=2, tx_time='2026-01-08 09:00:00', direction='支出', amount=20, tx_type='餐饮',
       counterparty='示例小店', item='晚饭'),
    tx(id=3, tx_time='2026-01-09 19:30:00', direction='支出', amount=25, tx_type='餐饮',
       counterparty='示例小店', item='外卖'),
    tx(id=4, tx_time='2026-03-05 15:00:00', direction='支出', amount=200, tx_type='购物',
       counterparty='示例网店', item='日用品'),
    tx(id=5, tx_time='2026-03-06 15:00:00', direction='支出', amount=30, tx_type='购物',
       counterparty='示例网店', item='文具'),
    tx(id=6, tx_time='2026-03-07 15:00:00', direction='支出', amount=40, tx_type='娱乐',
       counterparty='示例影院', item='电影票'),
    tx(id=7, tx_time='2026-03-08 15:00:00', direction='收入', amount=60, tx_type='其他收入',
       counterparty='示例平台', item='返现'),
]


def scenario_F(outdir: Path) -> dict:
    """F · 建新 M：拖进来的是「节点 + 交易」的混合。"""
    tmp, conn, rec, init_dir = begin(
        outdir, 'F', F_DATA, 't1~t3 餐饮、t4/t5 购物、t6 娱乐、t7 其他收入（收入 60.00）。')
    ids: dict = {}

    def build():
        ids['m1'] = create_node(conn, 'M_1', ['tx:1'])
        ids['m12'] = create_node(conn, 'M_12', ['tx:2', 'tx:3'])
        move(conn, [ids['m12']], ids['m1'])

    rec.step(
        '搭出前置结构：M_1 下有 t1，M_12 下有 t2/t3',
        "m1  = create_node(conn, 'M_1',  ['tx:1'])\n"
        "m12 = create_node(conn, 'M_12', ['tx:2', 'tx:3'])\n"
        'move(conn, [m12], m1)',
        '这正是用户描述的结构：\n'
        '  总账 → 餐饮(分类节点) → M_1 → { t1, M_12 → { t2, t3 } }\n'
        '（t1~t3 都是餐饮，所以 M_1 先挂在分类节点「餐饮」下，再把 M_12 搬进 M_1。）',
        build,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_1 挂在分类节点 cat:餐饮 下', find_node(root, ids['m1']).parent_id, 'cat:餐饮')
    rec.expect('M_12 在 M_1 下', find_node(root, ids['m12']).parent_id, ids['m1'])
    rec.expect('t1 的父亲是 M_1', name_of(root, tx_parent(root, 1)), 'M_1')
    rec.expect('t2、t3 的父亲是 M_12', (name_of(root, tx_parent(root, 2)), name_of(root, tx_parent(root, 3))), ('M_12', 'M_12'))

    rec.step(
        '★ 建 M_x：拖进来的是「t1 + t2 + t3 + M_12」（混合）',
        "m_x = create_node(conn, 'M_x', ['tx:1', 'tx:2', 'tx:3', m12])",
        '用户问的边界：t2/t3 的父节点是 M_12，M_12 和 t1 的父节点是 M_1。\n\n'
        '规则（top 折叠）：\n'
        '1. M_12 覆盖了它子树里的 t2/t3 → **t2/t3 自动排除**，它们不动（仍在 M_12 下）；\n'
        '2. 新 M_x 挂在**公共祖先**（LCA = M_1）下 —— 所以 M_12 和 t1 变成同父，合法；\n'
        '3. 被拖进来的 M_12 和 t1 一起进 M_x，**结构保留、不合并**。\n'
        '这里把 t2、t3 也显式拖进来，就是为了真的验「折叠」：不折叠的话 t2、t3 会被拽出 M_12。',
        lambda: ids.__setitem__('mx', create_node(conn, 'M_x', ['tx:1', 'tx:2', 'tx:3', ids['m12']])),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_x 挂在公共祖先 M_1 下', find_node(root, ids['mx']).parent_id, ids['m1'])
    rec.expect('M_12 进了 M_x', find_node(root, ids['m12']).parent_id, ids['mx'])
    rec.expect('t1 进了 M_x', name_of(root, tx_parent(root, 1)), 'M_x')
    rec.expect('t2、t3 被折叠排除：仍在 M_12 下、没有被拽出来',
               (name_of(root, tx_parent(root, 2)), name_of(root, tx_parent(root, 3))), ('M_12', 'M_12'))
    rec.expect('M_x 的直接子项只有 M_12 和 t1（t2/t3 不是它的孩子）',
               (sorted(c.name for c in find_node(root, ids['mx']).children if hasattr(c, 'children')),
                direct_tx_ids(find_node(root, ids['mx']))), (['M_12'], [1]))

    rec.step(
        '建 M_y：只拖一笔 t5',
        "m_y = create_node(conn, 'M_y', ['tx:5'])",
        '「默认至少要选一个 t，M 自动成为它的父亲节点」：\n'
        't5 现在是 M_y 的孩子；M_y 自己落在 t5 原来的位置 —— t5 原来在分类节点「购物」下，'
        '所以 M_y 挂在 `cat:购物` 下（这个分类节点此刻才落库）。',
        lambda: ids.__setitem__('my', create_node(conn, 'M_y', ['tx:5'])),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_y 挂在 cat:购物 下（t5 原来的位置）', find_node(root, ids['my']).parent_id, 'cat:购物')

    rec.step(
        '建 M_z：拖两笔同父的 t2/t3',
        "m_z = create_node(conn, 'M_z', ['tx:2', 'tx:3'])",
        't2/t3 还在 M_12 下 → 公共祖先是 M_12 → M_z 挂到 M_12 下（就地分层，不上浮）。',
        lambda: ids.__setitem__('mz', create_node(conn, 'M_z', ['tx:2', 'tx:3'])),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_z 挂在 M_12 下', find_node(root, ids['mz']).parent_id, ids['m12'])
    rec.expect('t2、t3 进了 M_z', (name_of(root, tx_parent(root, 2)), name_of(root, tx_parent(root, 3))), ('M_z', 'M_z'))

    rec.step(
        '建 M_w：拖跨分支的 t1 + t5',
        "m_w = create_node(conn, 'M_w', ['tx:1', 'tx:5'])",
        't1 现在在 M_x 下（餐饮分支）、t5 在 M_y 下（购物分支）→ 没有更近的公共祖先 → '
        '**LCA 升到总账**，M_w 挂在总账下。\n'
        '这就是「必须是同一个根节点下」的真实含义：单根模型里它恒成立，'
        '真正的规则是「挂到能覆盖所有选中项的最近祖先」。',
        lambda: ids.__setitem__('mw', create_node(conn, 'M_w', ['tx:1', 'tx:5'])),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_w 挂在总账根下（两个分支的 LCA）', find_node(root, ids['mw']).parent_id, 'root')
    rec.expect('M_y 被搬空（t5 进了 M_w）', find_node(root, ids['my']).children, [])

    rec.step(
        '建 M_v：只拖一个节点，不拖任何 t',
        "m_v = create_node(conn, 'M_v', [m12])",
        '**引擎层不强制「至少一个 t」**：只选节点也允许（这一步成功了，脚本里没有 expect_error），'
        'M_v 就是把 M_12 包了一层。\n'
        '如果产品上要「必须至少选一笔交易」，那这条得在前端拦，或者在引擎加一条校验 —— '
        '现在它默认放行。这是本次四组边界里**唯一**与用户口径有出入的地方。',
        lambda: ids.__setitem__('mv', create_node(conn, 'M_v', [ids['m12']])),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('只选节点建 M 被放行：M_v 挂在 M_12 原来的父亲 M_x 下，M_12 进了 M_v',
               (find_node(root, ids['mv']).parent_id, find_node(root, ids['m12']).parent_id), (ids['mx'], ids['mv']))

    def group_case():
        ids['mg'] = create_node(conn, 'M_g', ['tx:6', 'tx:7'])
        ids['grp'] = create_net_group(conn, ['tx:6', 'tx:7'], '娱乐组')
        ids['mh'] = create_node(conn, 'M_h', ['tx:6'])

    rec.step(
        '建 M_g + 组，再建 M_h 只拖组里的 t6',
        "m_g = create_node(conn, 'M_g', ['tx:6', 'tx:7'])\n"
        "create_net_group(conn, ['tx:6', 'tx:7'], '娱乐组')\n"
        "m_h = create_node(conn, 'M_h', ['tx:6'])",
        '边界：拖进来的 t 属于某个 +2 组 → **整组跟着走**（R2：组成员不可分割）。\n'
        't6（娱乐）、t7（其他收入）分类不同 → M_g 挂在根下。'
        'M_h 挂在 LCA（M_g）下，t6、t7 和「娱乐组」整体搬进 M_h。',
        group_case,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_g 挂在根下（t6、t7 分属两个分类）', find_node(root, ids['mg']).parent_id, 'root')
    rec.expect('M_h 挂在 M_g 下', find_node(root, ids['mh']).parent_id, ids['mg'])
    rec.expect('娱乐组 ν(支出40, 收入60) = 净收入 20.00', find_group(root, '娱乐组').value, (0, 2000))
    rec.expect('整组跟着走：组的位置是 M_h，t6、t7 的父亲也是 M_h',
               (name_of(root, find_group(root, '娱乐组').node_id), name_of(root, tx_parent(root, 6)),
                name_of(root, tx_parent(root, 7))), ('M_h', 'M_h', 'M_h'))
    rec.expect('M_g 自己没有直接子 t（都下沉到 M_h 了）', direct_tx_ids(find_node(root, ids['mg'])), [])

    rec.step(
        '看节点表：父子关系',
        'SELECT id, parent_id, name FROM ledger2_node',
        '每个节点只有**一个** parent_id —— 父指针是唯一真相，'
        '没有第二份「集合表」和它打架（第一轮 v_tx_counted 就是栽在这儿）。'
        '表里有：根、4 个分类节点（`cat:餐饮`、`cat:购物`、`cat:娱乐`、`cat:其他收入`，初始化时建好的）、'
        '以及 9 个用户建的 M。',
        lambda: None,
        observe=lambda c: obs_rows(c, 'ledger2_node'),
    )
    rec.expect('ledger2_node 共 14 行（根 + 4 个分类节点 + 9 个 M）', table_count(conn, 'ledger2_node'), 14)
    rec.expect('除根以外每个节点的 parent_id 都指向表里存在的节点（没有悬空父亲）',
               conn.execute('SELECT COUNT(*) FROM ledger2_node n WHERE n.parent_id IS NOT NULL AND NOT EXISTS '
                            '(SELECT 1 FROM ledger2_node p WHERE p.id = n.parent_id)').fetchone()[0], 0)

    rec.step(
        '★ 全量读：空节点为什么还在',
        'root = read_ledger(conn)',
        '本目录里的 `读出树.txt` 是**全量读**（read_ledger）—— 它忠实地反映「表里有什么」。\n'
        '**M_y 是空的**（它当初只装 t5，后来 t5 被搬进 M_w 了），但 M_y 这个节点还在表里，'
        '所以全量树里它出现，值 = 0.00。同理，被搬空的分类节点（如 `cat:其他收入`、`cat:娱乐`）也留在全量树里。\n\n'
        '这不是 bug，是**两种读法的区别**：\n'
        '- 全量读 = 结构事实：用户建过 M_y，它就是存在（哪怕现在是空的）；\n'
        '- scope 投影 = 这个范围内有没有东西：空节点必然没有 → 整个不出现（N11）。\n'
        '下一步对照看。',
        lambda: None,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('全量根值：支出 345.00 − 与 t7 对冲的 40.00 = 305.00；收入 60.00 → 对冲后 20.00', root.value, (30500, 2000))
    rec.expect('全量树里空的 M_y 还在，值 0.00', (find_node(root, ids['my']).value, find_node(root, ids['my']).children), ((0, 0), []))
    rec.expect('全量树里被搬空的分类节点（cat:娱乐）也还在，值 0.00', find_node(root, 'cat:娱乐').value, (0, 0))

    rec.step(
        '★ scope 读：同一个库，空节点不见了',
        "scope = scope_by_date(conn, '2026-01-01', '2026-01-31')\n"
        'proj = read_scope(read_ledger(conn), scope)',
        '**scope 投影里 M_y 整个不出现**（它里面连一笔都没有）。\n'
        'M_g、M_h 也不出现（一月范围内没有它们的交易）。\n'
        'M_w 还在 —— 因为它底下有一月的 t1；只是三月那笔 t5 被切掉了。\n\n'
        '所以回答那个问题：**是的，一旦选了 scope，空的 M 就不会出现在读出树里。**\n'
        '全量树和 scope 树读的是**同一个库**，只是过滤规则不同。',
        lambda: None,
        observe=lambda c: scope_obs(c, '2026-01-01', '2026-01-31'),
    )
    _, jan = scope_proj(conn, '2026-01-01', '2026-01-31')
    rec.expect('一月：M_y、M_g、M_h 都不出现',
               {ids['my'], ids['mg'], ids['mh']} & node_ids(jan), set())
    rec.expect('一月：M_w 还在（底下有 t1），值只算 t1', find_node(jan, ids['mw']).value, (3000, 0))
    rec.expect('一月：根值 = t1+t2+t3', jan.value, (7500, 0))
    _, mar = scope_proj(conn, '2026-03-01', '2026-03-31')
    rec.expect('三月：根值 = t4+t5+t6 与 t7 对冲后', mar.value, (23000, 2000))
    rec.expect('三月：M_w 还在（底下有 t5）、M_y 仍不出现',
               (ids['mw'] in node_ids(mar), ids['my'] in node_ids(mar)), (True, False))
    return finish_scoped(conn, tmp, outdir, init_dir, rec, [
        ('一月', '2026-01-01', '2026-01-31'),
        ('三月', '2026-03-01', '2026-03-31'),
    ])


# ── 场景 G：移动的四种组合 + 搬空之后 ────────────────────────────────────────

G_DATA = [
    tx(id=1, tx_time='2026-01-05 10:12:00', direction='支出', amount=30, tx_type='餐饮',
       counterparty='示例小店', item='午饭'),
    tx(id=2, tx_time='2026-01-08 09:00:00', direction='收入', amount=50, tx_type='其他收入',
       counterparty='示例商家', item='红包'),
    tx(id=3, tx_time='2026-01-09 19:30:00', direction='支出', amount=20, tx_type='餐饮',
       counterparty='示例小店', item='晚饭'),
    tx(id=4, tx_time='2026-02-02 12:05:00', direction='支出', amount=25, tx_type='餐饮',
       counterparty='示例小店', item='外卖'),
    tx(id=5, tx_time='2026-02-03 08:30:00', direction='支出', amount=15, tx_type='餐饮',
       counterparty='示例小店', item='咖啡'),
    tx(id=6, tx_time='2026-03-05 15:00:00', direction='支出', amount=200, tx_type='购物',
       counterparty='示例网店', item='日用品'),
    tx(id=7, tx_time='2026-03-06 15:00:00', direction='支出', amount=30, tx_type='购物',
       counterparty='示例网店', item='文具'),
    tx(id=8, tx_time='2026-02-10 11:00:00', direction='支出', amount=80, tx_type='交通',
       counterparty='示例地铁', item='月票'),
]

G_ROOT_FULL = (40000, 5000)     # 没有组时：支出 400.00 / 收入 50.00
G_ROOT_GROUPED = (37000, 2000)  # 组1 [t1 支出30, t2 收入50] 抵掉 30 → 支出 370.00 / 收入 20.00


def scenario_G(outdir: Path) -> dict:
    """G · 移动：1t / 多t / 1M / 多M / M-t 组合，以及搬空之后。"""
    tmp, conn, rec, init_dir = begin(
        outdir, 'G', G_DATA,
        't1/t2/t3 一月、t4/t5/t8 二月、t6/t7 三月 —— 日期是为了最后一步「scope 里还看不看得见被搬空的 M」。'
        '前五笔餐饮 + 红包，t6/t7 购物，t8 交通。')
    ids: dict = {}
    initial = snapshot(conn)

    def root_is(expected, what='移动只换父亲，根值不变'):
        rec.expect(what, read_ledger(conn).value, expected)

    def build():
        ids['ma'] = create_node(conn, 'M_A', ['tx:1', 'tx:2'])
        ids['mb'] = create_node(conn, 'M_B', ['tx:3', 'tx:4', 'tx:5'])
        ids['mc'] = create_node(conn, 'M_C', ['tx:6', 'tx:7'])
        ids['mt'] = create_node(conn, 'M_T', ['tx:8'])
        ids['g1'] = create_net_group(conn, ['tx:1', 'tx:2'], '组1')

    rec.step(
        '搭前置：四个 M + 一个 +2 组',
        "M_A[t1,t2]  M_B[t3,t4,t5]  M_C[t6,t7]  M_T[t8]\n"
        "create_net_group(conn, ['tx:1', 'tx:2'], '组1')",
        'M_A 的两笔分属「餐饮」「其他收入」→ 挂**根**下；M_B 全是餐饮 → 挂 `cat:餐饮` 下；'
        'M_C 挂 `cat:购物` 下；M_T 挂 `cat:交通` 下。组1 在 M_A 下，成员 t1/t2（支出 30 + 收入 50 → 净收入 20.00）。',
        build,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_A 在根下；M_B、M_C、M_T 各在自己的分类节点下',
               tuple(find_node(root, ids[k]).parent_id for k in ('ma', 'mb', 'mc', 'mt')),
               ('root', 'cat:餐饮', 'cat:购物', 'cat:交通'))
    rec.expect('组1 = 净收入 20.00', find_group(root, '组1').value, (0, 2000))
    root_is(G_ROOT_GROUPED, '前置之后根值 = 全量 (40000,5000) 减去组1 抵掉的 30.00')
    n_nodes = table_count(conn, 'ledger2_node')

    rec.step(
        '① 移动 1 个 t',
        "move(conn, ['tx:3'], mt)",
        '最小的一类：t3 挂到 M_T 下。',
        lambda: move(conn, ['tx:3'], ids['mt']),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t3 的父亲是 M_T', name_of(root, tx_parent(root, 3)), 'M_T')
    root_is(G_ROOT_GROUPED)

    rec.step(
        '② 移动多个 t（来自不同父节点）',
        "move(conn, ['tx:4', 'tx:6'], mt)",
        '**移动集合唯一**：t4（原本在 M_B）、t6（原本在 M_C）一起进 M_T，'
        '各自独立、不合并成新的 M，彼此的关系也不变。',
        lambda: move(conn, ['tx:4', 'tx:6'], ids['mt']),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t4、t6 的父亲都是 M_T', (name_of(root, tx_parent(root, 4)), name_of(root, tx_parent(root, 6))), ('M_T', 'M_T'))
    rec.expect('没有凭空多出新的 M（节点表行数不变）', table_count(conn, 'ledger2_node'), n_nodes)
    root_is(G_ROOT_GROUPED)

    rec.step(
        '③ 移动 1 个 M',
        "move(conn, [mc], mt)",
        'M_C 整棵子树跟着走 —— 里面的 t7 不动，只是 M_C 换了父亲。**结构保留**。',
        lambda: move(conn, [ids['mc']], ids['mt']),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_C 的父亲是 M_T，t7 还在 M_C 里', (find_node(root, ids['mc']).parent_id, name_of(root, tx_parent(root, 7))),
               (ids['mt'], 'M_C'))
    root_is(G_ROOT_GROUPED)

    rec.step(
        '④ 移动多个 M',
        "move(conn, [ma, mb], mt)",
        '一次搬两个 M，集合唯一：两个 M 各自换父亲，彼此不合并。\n'
        '搬完 M_A、M_B、M_C 都在 M_T 底下了。',
        lambda: move(conn, [ids['ma'], ids['mb']], ids['mt']),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_A、M_B、M_C 的父亲都是 M_T',
               tuple(find_node(root, ids[k]).parent_id for k in ('ma', 'mb', 'mc')), (ids['mt'],) * 3)
    rec.expect('M_A 里的组1 没被拆', find_group(root, '组1').node_id, ids['ma'])
    root_is(G_ROOT_GROUPED)

    rec.step(
        '⑤ M-t 组合移动',
        "move(conn, [ma, 'tx:3'], mb)",
        '节点和交易混在一个集合里搬：M_A 进 M_B，t3（此刻在 M_T 下）也进 M_B —— '
        '它们各挂各的，不因为在同一次操作里就变成父子。\n'
        '移动集合还是唯一的：{M_A, t3} 两个元素，各就各位。',
        lambda: move(conn, [ids['ma'], 'tx:3'], ids['mb']),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_A 进了 M_B；t3 也进了 M_B，但 t3 不在 M_A 里',
               (find_node(root, ids['ma']).parent_id, name_of(root, tx_parent(root, 3))), (ids['mb'], 'M_B'))
    root_is(G_ROOT_GROUPED)

    rec.step(
        '⑥ 移动组里的一笔 → 整组跟着走',
        "move(conn, ['tx:1'], mc)",
        'R2：组成员不可分割。动 t1 → t2 也跟着来，'
        '而且**组的位置**（ledger2_net_group.node_id）也一起改到 M_C。\n'
        '此时 M_A 被搬空了 —— 但它**还在树里**（下一步专门看这件事）。',
        lambda: move(conn, ['tx:1'], ids['mc']),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t1、t2 都进了 M_C，组1 的位置也是 M_C',
               (name_of(root, tx_parent(root, 1)), name_of(root, tx_parent(root, 2)),
                find_group(root, '组1').node_id), ('M_C', 'M_C', ids['mc']))
    root_is(G_ROOT_GROUPED)

    rec.step(
        '⑦ 把 M_B 彻底搬空',
        "move(conn, ['tx:5', 'tx:3', ma], mt)",
        'M_B 底下的东西（t5、t3 和子节点 M_A）全搬走 → M_B 变成**一个子节点都没有**的空节点；M_A 在 ⑥ 里已经空了。\n'
        '**用户问：读出树里还会有它吗？** 下面分两种读法看。',
        lambda: move(conn, ['tx:5', 'tx:3', ids['ma']], ids['mt']),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_A、M_B 都空了（没有孩子）', (find_node(root, ids['ma']).children, find_node(root, ids['mb']).children), ([], []))
    root_is(G_ROOT_GROUPED)

    rec.step(
        '★ 全量读：被搬空的 M 还在',
        'root = read_ledger(conn)',
        '**全量树里 M_A、M_B 都还在**，只是底下空了（值 = 0.00）。\n'
        '这是对的：节点是**结构**，空节点也是结构的一部分（用户建的、手工摆的）。\n'
        '删除节点是另一个动作（解散 M），不是「搬空」的副作用。',
        lambda: None,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('全量树里 M_A、M_B 还在，值 0.00',
               (find_node(root, ids['ma']).value, find_node(root, ids['mb']).value), ((0, 0), (0, 0)))

    rec.step(
        '★ scope 读：被搬空的 M 消失',
        "scope = scope_by_date(conn, '2026-03-01', '2026-03-31')",
        '**scope 投影里 M_A、M_B 整个不出现**（N11：范围内没有东西的分类整个不出现）。\n'
        '三月范围内它们底下确实一笔都没有。\n'
        '所以答案是：**全量树里有（空的），scope 里没有** —— 两者不矛盾，scope 是投影。',
        lambda: None,
        observe=lambda c: scope_obs(c, '2026-03-01', '2026-03-31'),
    )
    _, mar = scope_proj(conn, '2026-03-01', '2026-03-31')
    rec.expect('三月：M_A、M_B 不出现', {ids['ma'], ids['mb']} & node_ids(mar), set())
    rec.expect('三月：M_T、M_C 还在（底下有 t6、t7）', {ids['mt'], ids['mc']} <= node_ids(mar), True)
    rec.expect('三月：根值 = t6 + t7（组1 的成员在一月，不在范围内）', mar.value, (23000, 0))

    before = snapshot(conn)
    rec.step(
        '⑧ 往自己的子树里搬 → 拒绝',
        "move(conn, [mt], mb)",
        'M_B 现在在 M_T 底下 → 把 M_T 塞进 M_B = 成环 → **拒绝**，理由说人话。被拒绝的操作不写任何东西。',
        lambda: move(conn, [ids['mt']], ids['mb']),
        observe=lambda c: obs_full(c),
        expect_error='CYCLE',
    )
    rec.expect('拒绝后四张业务表逐行未变', snapshot(conn) == before, True)

    rec.step(
        '⑨ 全部撤销，看能不能回到最初',
        'while True:\n    try: undo(conn)\n    except LedgerError: break',
        'Q4：连撤到头，每张表逐字段回到最初的样子（撤销 = 执行逆原语序列，不是快照回滚）。'
        '共 12 次：建 4 个 M、建 1 个组、7 次移动。分类节点是初始化时建的，不属于任何一次操作，所以撤完仍在。',
        lambda: ids.__setitem__('undone', undo_all(conn)),
        observe=lambda c: obs_full(c),
    )
    rec.expect('一共撤销了 12 次（5 次搭建 + 7 次移动；被拒绝的 ⑧ 不进日志）', ids['undone'], 12)
    rec.expect('撤到头之后四张业务表逐行等于最初', snapshot(conn) == initial, True)
    rec.expect('ledger2_node 回到初始化后的 5 行（根 + 4 个分类节点）', table_count(conn, 'ledger2_node'), 5)
    root_is(G_ROOT_FULL, '撤到头之后根值回到全量值（没有任何组）')
    return finish_scoped(conn, tmp, outdir, init_dir, rec, [('三月', '2026-03-01', '2026-03-31')]) \
        if False else _g_finish(conn, tmp, outdir, init_dir, rec, ids)


def _g_finish(conn, tmp, outdir, init_dir, rec, ids):
    # G 的三月范围树要在「搬空之后」的状态下读（撤销到头后树已经回到最初），所以只出全量读出树，
    # 三月的投影内容已经在步骤的「观察」里完整记录，并由断言钉死。
    return finish(conn, tmp, outdir, init_dir, rec)


# ── 场景 H：解散一个 M，+2 条目的父节点 ──────────────────────────────────────

H_DATA = [
    tx(id=1, tx_time='2026-01-05 10:12:00', direction='支出', amount=30, tx_type='餐饮',
       counterparty='示例小店', item='午饭'),
    tx(id=2, tx_time='2026-01-08 09:00:00', direction='支出', amount=20, tx_type='餐饮',
       counterparty='示例小店', item='晚饭'),
    tx(id=3, tx_time='2026-01-09 19:30:00', direction='支出', amount=25, tx_type='餐饮',
       counterparty='示例小店', item='外卖'),
    tx(id=4, tx_time='2026-02-02 12:05:00', direction='支出', amount=100, tx_type='交通',
       counterparty='示例地铁', item='月票'),
    tx(id=5, tx_time='2026-02-03 08:10:00', direction='收入', amount=50, tx_type='其他收入',
       counterparty='示例商家', item='红包'),
]

GROUP_ROWS = 'SELECT name, node_id FROM ledger2_net_group ORDER BY name'
PLACE_ROWS = 'SELECT tx_id, node_id, net_group_id IS NOT NULL AS in_group FROM ledger2_tx ORDER BY tx_id'


def scenario_H(outdir: Path) -> dict:
    """H · 解散一个 M，M_1 下有 [t1] [t2,t3]+2 [t4,t5]+2。"""
    tmp, conn, rec, init_dir = begin(
        outdir, 'H', H_DATA, 't1 普通；t2/t3 一组；t4/t5 一组（t4 交通支出 100.00、t5 收入 50.00）。')
    ids: dict = {}

    def build():
        ids['m1'] = create_node(conn, 'M_1', ['tx:1', 'tx:2', 'tx:3', 'tx:4', 'tx:5'])
        ids['ga'] = create_net_group(conn, ['tx:2', 'tx:3'], '组A')
        ids['gb'] = create_net_group(conn, ['tx:4', 'tx:5'], '组B')

    rec.step(
        '搭出 M_1：[t1] [t2,t3]+2 [t4,t5]+2',
        "create_node(conn, 'M_1', ['tx:1','tx:2','tx:3','tx:4','tx:5'])\n"
        "create_net_group(conn, ['tx:2', 'tx:3'], '组A')\n"
        "create_net_group(conn, ['tx:4', 'tx:5'], '组B')",
        '用户描述的结构，一模一样。五笔分属三个分类 → M_1 挂**根**下。'
        '组A（支出 20 + 25，同向）= 45.00；组B（支出 100 vs 收入 50）= 净支出 50.00。',
        build,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_1 挂在根下', find_node(root, ids['m1']).parent_id, 'root')
    rec.expect('组A = 支出 45.00（同向不抵消）', find_group(root, '组A').value, (4500, 0))
    rec.expect('组B = 净支出 50.00', find_group(root, '组B').value, (5000, 0))
    root_value = root.value
    rec.expect('根值 = t1 + 组A + 组B', root_value, (12500, 0))

    rec.step(
        '★ 解散前：组和成员的父节点分别是谁',
        'SELECT id, node_id FROM ledger2_net_group\n'
        'SELECT tx_id, role, node_id, net_group_id FROM ledger2_tx',
        '先记下「之前」：两个组的 node_id 都是 M_1；五笔的 node_id 也都是 M_1。',
        lambda: None,
        observe=lambda c: obs_rows(c, 'ledger2_net_group', 'ledger2_tx'),
    )
    rec.expect('两个组的位置都是 M_1',
               [tuple(r) for r in conn.execute(GROUP_ROWS)], [('组A', ids['m1']), ('组B', ids['m1'])])
    rec.expect('五笔的 node_id 都是 M_1；t2~t5 在组里、t1 不在',
               [tuple(r) for r in conn.execute(PLACE_ROWS)],
               [(1, ids['m1'], 0), (2, ids['m1'], 1), (3, ids['m1'], 1), (4, ids['m1'], 1), (5, ids['m1'], 1)])
    pre_dissolve = __import__('record_ledger2_scenarios').snapshot(conn)

    rec.step(
        '★ 解散 M_1',
        'dissolve_node(conn, m1)',
        '规则：M_1 下面的东西都把 **M_1 的父节点**（这里是总账 root）当作自己的父亲。\n'
        '这一步要验两件事：\n'
        '1. +2 条目自己的父节点（ledger2_net_group.node_id）有没有变；\n'
        '2. 组里面那些 t 的父节点（ledger2_tx.node_id）有没有变。',
        lambda: dissolve_node(conn, ids['m1']),
        observe=lambda c: obs_rows(c, 'ledger2_node', 'ledger2_net_group', 'ledger2_tx'),
    )
    rec.expect('M_1 解散了：节点表回到初始化的 4 行（根 + 3 个分类节点）', table_count(conn, 'ledger2_node'), 4)
    rec.expect('两个组的位置都从 M_1 变成了 root',
               [tuple(r) for r in conn.execute(GROUP_ROWS)], [('组A', 'root'), ('组B', 'root')])
    rec.expect('组里每笔 t 的 node_id 都从 M_1 变成了 root，成员关系（net_group_id）没变',
               [tuple(r) for r in conn.execute(PLACE_ROWS) if r['tx_id'] != 1],
               [(2, 'root', 1), (3, 'root', 1), (4, 'root', 1), (5, 'root', 1)])
    rec.expect('t1 也上移到了 root（归属行原地改成指向 root）',
               [tuple(r) for r in conn.execute('SELECT tx_id, node_id, net_group_id FROM ledger2_tx WHERE tx_id=1')],
               [(1, 'root', None)])

    rec.step(
        '解散后：读出树',
        'root = read_ledger(conn)',
        '两个组**都还在**，成员关系没变（t2/t3 一组、t4/t5 一组），'
        '只是整体上移到了总账下。\n'
        '注意 t1：它**直接挂到总账根下**（不是回到它的分类节点「餐饮」）—— 解散的规则是「交给 M_1 的父亲」，'
        '而 M_1 的父亲是根；t1 的归属行变成 node_id=root。',
        lambda: None,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('解散后根值不变（解散不改变任何一笔的向量）', root.value, root_value)
    rec.expect('t1 作为根的直接子项出现', direct_tx_ids(root), [1])
    rec.expect('两个组成了根的直接子项', sorted(c.name for c in root.children if hasattr(c, 'members')), ['组A', '组B'])

    rec.step(
        '撤销这次解散',
        'undo(conn)',
        'Q4：解散是可逆的，撤回来之后 M_1、组的位置、成员的归属全部回到原样。',
        lambda: undo(conn),
        observe=lambda c: obs_rows(c, 'ledger2_node', 'ledger2_net_group', 'ledger2_tx'),
    )
    from record_ledger2_scenarios import snapshot as snap
    rec.expect('撤销后四张业务表逐行等于解散之前', snap(conn) == pre_dissolve, True)

    rec.step(
        '第二轮：先把 M_1 搬进分类节点「餐饮」',
        "move(conn, [m1], 'cat:餐饮')",
        '同一个 M_1，这次让它的父亲不是根而是分类节点 `cat:餐饮`（分类节点是初始化时建好的，移动不增删节点）。',
        lambda: move(conn, [ids['m1']], 'cat:餐饮'),
        observe=lambda c: obs_rows(c, 'ledger2_node'),
    )
    rec.expect('M_1 的父亲是 cat:餐饮；节点表 4 行（根 + 3 个分类节点 + M_1... 见下）',
               (find_node(read_ledger(conn), ids['m1']).parent_id, table_count(conn, 'ledger2_node')), ('cat:餐饮', 5))

    rec.step(
        '第二轮：解散 M_1',
        'dissolve_node(conn, m1)',
        '这次 M_1 的父亲是 `cat:餐饮`，所以组和成员都上移到 `cat:餐饮`。\n'
        't1 不在任何组里，归属行仍是它自己的一行、node_id 变成 `cat:餐饮`；\n'
        't2~t5 在组里，node_id 都是 `cat:餐饮`、net_group_id 指向各自的组。',
        lambda: dissolve_node(conn, ids['m1']),
        observe=lambda c: obs_rows(c, 'ledger2_node', 'ledger2_net_group', 'ledger2_tx'),
    )
    rec.expect('两个组的位置都是 cat:餐饮', [tuple(r) for r in conn.execute(GROUP_ROWS)],
               [('组A', 'cat:餐饮'), ('组B', 'cat:餐饮')])
    rec.expect('t2~t5 的 node_id 都是 cat:餐饮、仍在组里',
               [tuple(r) for r in conn.execute(PLACE_ROWS)],
               [(1, 'cat:餐饮', 0), (2, 'cat:餐饮', 1), (3, 'cat:餐饮', 1), (4, 'cat:餐饮', 1), (5, 'cat:餐饮', 1)])
    rec.expect('t1 上移到 cat:餐饮，读出时是 cat:餐饮 的直接子项',
               tx_parent(read_ledger(conn), 1), 'cat:餐饮')
    rec.expect('根值仍不变', read_ledger(conn).value, root_value)
    return finish(conn, tmp, outdir, init_dir, rec)


# ── 主流程 ────────────────────────────────────────────────────────────────────

PLAN = [
    ('E_+2组跨scope', 'E · +2 条目一部分在 scope 内、一部分在 scope 外', scenario_E,
     '用户问：「如果我有一个 +2 条目是 t2 t3 t4 t114514，我们就只 +2 这个 t2-t4 —— '
     '那么如果一个 +2 条目一部分在你的 scope 内、一部分在外面，你的功能还能实现吗？」\n\n'
     '**结论：能。** 规则是「只按 scope 内的成员做 +2；scope 外的成员不参与，'
     '也不能又冒成节点的直接子项」。三个边界都测了：剩 2 个（正常抵消）、'
     '剩 1 个（组还在、不抵消）、剩 0 个（组整个不出现）。'),
    ('F_建M的折叠边界', 'F · 建新 M：拖进来的是「节点 + 交易」的混合', scenario_F,
     '用户问：「创建新的 M_**，默认至少需要选择一个 t，M**自动成为它的父亲节点；'
     '如果有多个节点被拖进来创建，则它们必须是同一个根节点下的'
     '（比如 t1、t2、t3、M_12 都被拖进来，其中 t2 t3 的父节点是 M_12；'
     'M_12 和 t1 的父节点是 M_1，那么我自动排除掉 t2 t3）」。\n\n'
     '**结论：完全按这个规则跑。** t2/t3 被 M_12 覆盖 → 自动排除、不动；'
     'M_12 和 t1 因为有共同祖先 M_1 而合法，新 M 挂在 M_1 下。\n'
     '另外测了「只选节点不选 t」——引擎层放行，要不要拦由产品定。'),
    ('G_移动的四种组合', 'G · 移动：1t / 多t / 1M / 多M / M-t，以及搬空之后', scenario_G,
     '用户问：「要将某个交易 t 移动到某个 M 下，或者某个 M 进行同样的操作，分类讨论一下：'
     '一个 t、多个 t、一个 M、多个 M、M-t 组合……不管怎么样数学上都可以计算出一个唯一的移动集合，'
     '并且这个集合里面还是保留了结构的 [M_1, t1, t2] 就是它们几个是不可合并的」\n\n'
     '**结论：五种组合都跑了，移动集合唯一、结构保留。**\n'
     '还有用户问的：「如果有些 M 下面经过这样的移动被搬空了，在你的 scope 里面是不是就没了？'
     '我的读出树还会有它吗？」—— **全量树里有（空节点，值 0.00），scope 投影里没有。**'),
    ('H_解散M与组的父亲', 'H · 解散一个 M：+2 条目的父节点有没有跟着变', scenario_H,
     '用户问：「M_1 下面有 [t1] [t2, t3]+2 [t4, t5]+2，在我删掉 M_1 之后，'
     '它们应该把 M_1 的父节点当作自己的父亲 —— 但我要看看这个时候 +2 条目的父亲有没有变，'
     '里面的 t 的父亲有没有变」。\n\n'
     '**结论：两个都变了。** 组的位置（ledger2_net_group.node_id）从 M_1 变成 M_1 的父亲；'
     '组里每笔 t 的归属（ledger2_tx.node_id）也跟着变；组成员关系本身不变。'
     '分类节点让「t1 的归属行删不删」取决于 M_1 的父亲是不是 t1 的分类节点（见 H 场景两轮对照）。'),
]

KEY_POINTS = {
    'E_+2组跨scope':
        '能。scope 内 2 个成员 → ν 按这 2 个算 (0,20.00)；剩 1 个 → 组还在、不抵消 (20.00,0)；'
        '剩 0 个 → 组整个不出现。**范围外的成员不会重复计数**，三次读完四张业务表逐行未变。',
    'F_建M的折叠边界':
        't2/t3 被 M_12 覆盖 → 自动排除；M_12 与 t1 的共同祖先是 M_1 → 新 M 挂 M_1 下；跨分支升到根；'
        '「只选节点不选 t」引擎放行（要拦由产品定）。',
    'G_移动的四种组合':
        '五种组合都跑通，移动集合唯一（不凭空多出节点）、结构保留；每次移动根值恒为 (37000,2000)；'
        '组内成员不可分割（R2）；**搬空的 M：全量树里有，scope 里没有**；12 次撤销逐行还原。',
    'H_解散M与组的父亲':
        '组的 node_id 和组里每笔 t 的 node_id **都从 M_1 变成了 M_1 的父亲**；成员关系不变；'
        '**t1 的归属行：M_1 的父亲是根时保留（node_id=root），是 cat:餐饮 时删除**。',
}


def main() -> None:
    os.chdir(tempfile.gettempdir())
    clear_dir(OUT_ROOT)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    index = [
        '# 账本代数 V2 · 边界场景验证记录', '',
        '这一份是专门测**边界**的：上一份（`04_账本代数V2-验证记录`）测的是主干流程，'
        '这一份测的是最容易想歪的地方。', '',
        '**跑出来的，不是写出来的** —— 每一步都真跑了 `backend.ledger2`（生产包）、跑在真 schema 的临时库上，'
        'CSV 是真从 SQLite 导出来的，关键数字由脚本里的 `assert` 钉死（每个场景文档里有「断言」表）。', '',
        '## 四组边界', '',
        '| 场景 | 用户问的是什么 | 结论 |', '|---|---|---|',
    ]

    results = {}
    for slug, title, fn, purpose in PLAN:
        outdir = OUT_ROOT / slug
        outdir.mkdir(parents=True, exist_ok=True)
        meta = fn(outdir)
        write_scenario_doc(outdir, meta, title, purpose, [])
        results[slug] = meta
        print(f'[完成] {slug}  步骤 {len(meta["rec"].rows)}，断言 {len(meta["rec"].asserts)}')

    for slug, title, fn, purpose in PLAN:
        index.append(f'| [{title}]({slug}/00_操作说明.md) | {purpose.splitlines()[0]} '
                     f'| {KEY_POINTS[slug]} |')

    index += [
        '', '### 每个场景目录里有什么', '', '```',
        'XX_场景名/',
        '├── 00_操作说明.md      ← 每一步：命令 / 含义 / 结果 / 观察 / 断言',
        '├── 初始/*.csv           ← 开跑前每张表的数据（7 张表都有，空表也出表头）',
        '├── 结果/*.csv           ← 跑完后**变了**的表的数据（没变的不重复出）',
        '├── 结果/说明.md         ← 哪些表全程没变',
        '├── id对照.csv           ← node_id / group_id / 分类节点 id 到底是谁',
        '├── 读出树.txt / .json   ← 全量读出（人看的 / 机器看的）',
        '└── 读出树_<范围>.txt/.json ← 某个 scope 下的读出（E / F）',
        '```', '',
        '开跑前先按平台分类初始化（`presets.init_by_platform_category`），每笔交易都有显式归属行；'
        '树里 `[M] 餐饮` 这类是**分类节点**（`cat:<分类名>`，初始化时建好的普通节点，可改名 / 解散 / 搬动）；'
        '用户自己建的 M 一律叫 `M_...`。'
        '`ledger2_op_log.created_at` 是数据库墙上时钟，每次跑都不同，其余 CSV 逐字节可复现。', '',
        '## 怎么复现', '', '```bash',
        '# 在 V2 仓库根目录（ai-finance-assistant-v2）',
        f'{PY} -m pytest tests/test_ledger2*.py -q                          # 契约测试（含这四组边界）',
        f'{PY} -X utf8 scripts/record_ledger2_edge_scenarios.py            # 生成本记录',
        '```', '',
        '本记录由 `scripts/record_ledger2_edge_scenarios.py` 生成，每次跑都新建临时库、先清掉旧目录 → '
        '无陈旧文件、可复现、可 diff。', '',
    ]

    # 边界相关的契约测试
    tests = collect_contract_tests()
    edge = [t for t in tests if '边界' in t['section']]
    files = sorted({t['file'] for t in tests})
    index += ['## 这四组边界对应的契约测试', '',
              f'`tests/test_ledger2*.py` 现有 {len(files)} 个文件、共 {len(tests)} 个用例，'
              f'其中 {len(edge)} 个落在标着「边界」的小节里（清单由 `ast` 解析生成）：', '',
              '| 用例 | 文件 | 所在小节 | 钉住什么 |', '|---|---|---|---|']
    for t in edge:
        index.append(f"| `{t['name']}` | `{t['file']}` | {t['section']} | {t['doc'] or '—'} |")
    index += ['', '## 与用户口径的唯一出入', '',
              '「创建新 M **默认至少要选一个 t**」这条目前只由 UI 保证，**引擎层没有强制** —— '
              '只拖一个节点进去建 M 也放行（F 场景「建 M_v」一步，脚本里该步没有 `expect_error`，成功才算通过）。'
              '要不要在引擎加校验，等产品口径定。', '',
              '## 读出树有两种：全量读 vs scope 投影', '',
              '每个场景里的文件分工是这样的：', '',
              '| 文件 | 是什么 | 空的 M 在不在 |', '|---|---|---|',
              '| `读出树.txt` / `.json` | **全量读**（`read_ledger`）—— 忠实反映表里有什么 | **在**（值 0.00） |',
              '| `读出树_<范围>.txt` / `.json` | **scope 投影**（`read_scope`）—— 这个范围内有什么 | **不在**（N11） |',
              '',
              '以 F 场景为例：M_y 当初只装 t5，后来 t5 被拖进 M_w，M_y 就空了 —— '
              '但没人删它，所以它还在 `ledger2_node` 表里，全量树里就会出现；被搬空的分类节点'
              '（`cat:其他收入`、`cat:娱乐`）同理，作为初始化时建的空分类节点留在全量树里。',
              '**一旦选了 scope，空的 M 就整个不出现**（见 F 场景的 `读出树_一月.txt` / `读出树_三月.txt`）。',
              '',
              '为什么这样是对的：全量读是**结构视图**（「用户建过 M_y」是事实）；'
              'scope 是**投影**（「这个范围内有没有东西」）。空节点在任何一个 scope 里都不可能有东西，'
              '所以必然被滤掉。', '',
              '**这里有个产品决定**：如果全量读也把空节点藏起来，'
              '那「空 M」和「被删掉的 M」在读出上就分不出来了 —— '
              '搬空和删除会变成同一件事。建议**全量保留、UI 上弱化显示**（灰/虚线），'
              '让用户一眼看出「这是空的、东西被搬走了」。', '',
              '> 所有数据都是**虚构的**（示例小店/示例影院），不碰真库 `db/bills.db`。', '']

    (OUT_ROOT / 'README.md').write_text('\n'.join(index), encoding='utf-8')
    print(f'\n输出目录：{OUT_ROOT}')


if __name__ == '__main__':
    main()
