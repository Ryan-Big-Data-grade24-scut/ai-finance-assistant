"""把账本代数 V2 的**初始化 / 卸载 / 人工字段**（数据层）跑一遍，落到
proj/draft/06_账本代数V2-初始化/。

用法（在 V2 仓库根目录）：
    ../ai-finance-assistant/.venv/Scripts/python.exe -X utf8 scripts/record_ledger2_init_scenarios.py

和 draft/04 的区别：04 的每个场景都**假设初始化已经跑完**（从「每笔交易都在树里」开始）；
这里每个场景都从「一笔都没初始化」开始，验的就是初始化和卸载本身：

    I  平台分类初始化（预设）：空账本 → 6 个节点 / 7 行归属 / 7 行人工字段；晚到的交易能补；重复跑被拒。
    J  自定义初始化 op：新节点 / 旧节点 / 嵌套路径、放置、+2 组同父检查、分裂（默认同路径 / 显式位置）、
       组成员可以是 t′、标题备注；非法输入整体原子拒绝。
    K  卸载：注销 4 个初始化字段、带走 t′、组不足 2 人自动解散；可撤销 / 重做；可重新初始化。
    L  读出前提与人工字段：未初始化的交易在所有读出里被过滤，也不影响别的交易；
       退款**不自动归并**（事实层只见原子），要抵消就建 +2 组；
       `category_path` 初始化时写一次、之后改名 / 移动不刷新；`update_manual` 只改标题 / 备注；
       节点备注 `ledger2_node.note` 可随初始化写入，之后用 `update_node` 改。

原则（同 04）：
* 所有数据都是**虚构的**，不碰真库 db/bills.db，每次跑都新建临时库。
* 跑在**真 schema** 上（`backend.db.init_db`），账本表是 `ledger2_*`。
* 关键数字用 assert 钉死（`Recorder.expect`），文档里的「断言」表就是这些 assert 的清单。
* 新建 id 用固定种子的 UUID，CSV 逐字节可复现（只有 `ledger2_op_log.created_at` 是墙上时钟）。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))        # 复用 04 的记录件
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from record_ledger2_scenarios import (  # noqa: E402
    DATA, PY, ROOT_FULL, TABLES, Recorder, add_tx, clear_dir, collect_contract_tests,
    direct_tx_ids, dump_tables, find_group, find_node, finish, make_db, name_of, obs_full,
    row_counts, seed_uuids, snapshot, table_count, tx, tx_parent, write_scenario_doc,
)
from backend.ledger2 import facts, ops, presets  # noqa: E402
from backend.ledger2.errors import LedgerError  # noqa: E402
from backend.ledger2.model import NetGroup, Tx  # noqa: E402
from backend.ledger2.ops import (  # noqa: E402
    create_net_group, init_txs, redo, uninst_txs, undo, update_manual, update_node,
)
from backend.ledger2.reader import read_ledger  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PROJ = REPO.parent
OUT_ROOT = PROJ / 'draft' / '06_账本代数V2-初始化'

# 06 的通用说明（04 的那份说「开跑前已经初始化」，这里正好相反）
INIT_LEGEND = [
    '每个场景都从**一笔都没初始化**的库开始：`ledger2_tx` 是空表，'
    '`ledger2_node` 只有建表时放的总账根。此时读出是一棵空树 —— **没初始化 = 读不出来**。',
    '初始化 / 卸载都是**普通操作**：一次操作 = 一条 `ledger2_op_log`，可以用 `undo` / `redo`。',
    '初始化时写入的 4 个人工字段和**归属信息在同一行**（`ledger2_tx`）：`initialized=1`、`title`（标题）、'
    '`note`（备注，库里叫 note、模型里叫 memo）、`category_path`（初始化那一刻的完整路径，根以下各级节点名），'
    '以及 `node_id` / `net_group_id`。节点也有自己的 `note`（初始化 op 的 `nodes[].note` 可写）。',
    '**每笔已初始化的交易（包括分裂出的 t′）都有一行显式 `ledger2_tx`**；'
    '没有归属行的交易在读出里直接被过滤。',
    '平台分类节点（`cat:<分类名>`）是初始化预设建出来的**普通节点**：`cat:` 只是命名约定，可以改名、解散、搬动。',
    '金额：库里是元，引擎里是整数分；`(e, i)` = (支出, 收入)。树里的 `e=30.00` 是元。',
    '`ledger2_op_log.created_at` 是数据库自己打的墙上时钟，每次跑都不同；其余 CSV 逐字节可复现（id 用固定种子）。',
]


# ── 小工具 ──────────────────────────────────────────────────────────────────

def begin_raw(outdir: Path, tag: str, rows: list[dict], meaning: str):
    """每个场景的开头：建临时库、播种**但不初始化**、记第 1 步、导「初始」CSV。"""
    seed_uuids(tag)
    tmp = Path(tempfile.mkdtemp(prefix=f'ledger2init_{tag}_'))
    conn = make_db(tmp / f'{tag}.db', rows, init=False)
    rec = Recorder(conn)
    rec.step(
        f'建库 + 播种 {len(rows)} 笔虚构交易（一笔都没初始化）',
        f"conn = make_db(tmp/'{tag}.db', rows, init=False)\n"
        '# = backend.db.init_db（真 schema）+ 插入 transactions_raw + store.ensure_schema（建 ledger2_* 表和根节点）',
        meaning,
        lambda: None,
        observe=lambda c: row_counts(c),
    )
    init_dir = outdir / '初始'
    dump_tables(conn, init_dir)
    return tmp, conn, rec, init_dir


def manual_row(conn, tx_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM ledger2_tx WHERE tx_id=? AND role='primary'", (int(tx_id),)).fetchone()
    return None if r is None else dict(r)


def primary_rows(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM ledger2_tx WHERE role='primary'").fetchone()[0]


def placement(conn, tx_id: int, role: str = 'primary'):
    r = conn.execute('SELECT node_id, net_group_id FROM ledger2_tx WHERE tx_id=? AND role=?',
                     (int(tx_id), role)).fetchone()
    return None if r is None else (r['node_id'], r['net_group_id'])


def raw_count(conn, tx_id: int) -> int:
    return conn.execute('SELECT COUNT(*) FROM transactions_raw WHERE id=?', (int(tx_id),)).fetchone()[0]


# ── 场景 I：平台分类初始化（预设）────────────────────────────────────────────

def scenario_I(outdir: Path) -> dict:
    """I 平台分类初始化：空账本 → 预设一次 → 每笔都在树里；晚到的能补；重复跑被拒。"""
    rows = [dict(r) for r in DATA]
    rows[2]['remark'] = '平台备注示例'      # t3 有平台备注 → 抄进 note
    rows[5]['item'] = ''                    # t6 商品为空 → 标题回退成交易对方
    tmp, conn, rec, init_dir = begin_raw(
        outdir, 'I', rows,
        '原始表 `transactions_raw` 是「事实」，账本层只读它、不改它。这张库里 **7 笔交易一笔都没初始化**：'
        '`ledger2_tx` 空、`ledger2_node` 只有建表时的根。读出是一棵空树。')
    rec.expect('未初始化时 ledger2_tx 0 行', table_count(conn, 'ledger2_tx'), 0)
    rec.expect('未初始化时 ledger2_node 只有根 1 行', table_count(conn, 'ledger2_node'), 1)
    rec.expect('未初始化时读出一棵空树', (read_ledger(conn).children, read_ledger(conn).value), ([], (0, 0)), plain=True)
    rec.expect('7 笔交易全在「未初始化」列表里（按时间排）', facts.uninitialized_ids(conn), [1, 2, 3, 6, 4, 5, 7])

    rec.step(
        '按平台分类初始化全部 7 笔',
        'presets.init_by_platform_category(conn)',
        '引擎层以后会把「初始化预设」包成一种初始化风格；数据层直接用 `presets` 拼一个 init op 交给 '
        '`ops.init_txs`：每个平台分类建一个节点（id `cat:<分类名>`）、每笔交易放进自己的分类节点、'
        '标题 = 商品（没有则交易对方）、备注 = 平台备注。整个预设是**一次操作**，可以一次撤销。',
        lambda: presets.init_by_platform_category(conn),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('节点 = 根 + 5 个分类节点', table_count(conn, 'ledger2_node'), 6)
    rec.expect('归属行 = 每笔交易一行（人工字段就在同一行）', table_count(conn, 'ledger2_tx'), 7)
    rec.expect('整个预设记成 1 条操作（init）',
               (table_count(conn, 'ledger2_op_log'), ops.history(conn)[0]['name']), (1, 'init'), plain=True)
    rec.expect('这条操作的原语数 = 建 5 个节点 + 放 7 笔 + 写 7 行人工字段',
               ops.history(conn)[0]['prims'], 19)
    rec.expect('根下面只有 5 个分类节点',
               sorted(c.id for c in root.children),
               sorted(['cat:餐饮', 'cat:工资', 'cat:交通', 'cat:其他收入', 'cat:购物']))
    rec.expect('分类节点读出时带 kind=category 标签', find_node(root, 'cat:餐饮').kind, 'category')
    rec.expect('根值 = 全部交易的向量之和 +1', root.value, ROOT_FULL)
    rec.expect('分类节点「餐饮」下挂着 t1、t3、t4', direct_tx_ids(find_node(root, 'cat:餐饮')), [1, 3, 4])
    rec.expect('分类节点「工资」下挂着 t2', direct_tx_ids(find_node(root, 'cat:工资')), [2])

    rec.expect('t1 的标题 = 商品「午饭」，备注为空',
               (manual_row(conn, 1)['title'], manual_row(conn, 1)['note']), ('午饭', ''), plain=True)
    rec.expect('t3 的备注抄了平台备注', manual_row(conn, 3)['note'], '平台备注示例')
    rec.expect('t6 没有商品 → 标题回退成交易对方', manual_row(conn, 6)['title'], '示例商家')
    rec.expect('t1 的分类路径 = 初始化时的完整路径（根以下）',
               facts.load_txs(conn)[1].category_path, ['餐饮'])
    rec.expect('t7 的分类路径 = 初始化时的完整路径（根以下）',
               facts.load_txs(conn)[7].category_path, ['购物'])
    rec.expect('读出里是 7 笔（全部已初始化）', sorted(facts.load_txs(conn)), [1, 2, 3, 4, 5, 6, 7])

    def late_arrival():
        add_tx(conn, id=8, tx_time='2026-04-01 10:00:00', direction='支出', amount=10,
               tx_type='餐饮', counterparty='示例小店', item='加餐')
        conn.commit()
        presets.init_by_platform_category(conn)

    rec.step(
        '晚到一笔交易（t8）：再跑一次预设，只补它',
        "add_tx(conn, id=8, ...)\npresets.init_by_platform_category(conn)",
        '预设默认只处理「存活但还没初始化」的交易：已经初始化的 7 笔不会被重复处理，'
        '`cat:餐饮` 节点直接复用、不重建。',
        late_arrival,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('补完以后归属行 8 行（人工字段同在一行）', table_count(conn, 'ledger2_tx'), 8)
    rec.expect('节点仍是 6 行（cat:餐饮 复用）', table_count(conn, 'ledger2_node'), 6)
    rec.expect('t8 落在 cat:餐饮 下', placement(conn, 8), ('cat:餐饮', None))
    rec.expect('cat:餐饮 下变成 4 笔', direct_tx_ids(find_node(root, 'cat:餐饮')), [1, 3, 4, 8])
    rec.expect('根值多了 t8 的 10.00 支出', root.value, (38500, 805000))
    rec.expect('操作日志 2 条，都是 init', [h['name'] for h in ops.history(conn)], ['init', 'init'])

    rec.step(
        '没有未初始化的交易时再跑预设 → 拒绝',
        'presets.init_by_platform_category(conn)',
        '初始化标记防重复：没有可初始化的交易时直接拒绝（NO_EFFECT），不写任何一行、不记日志。',
        lambda: presets.init_by_platform_category(conn),
        observe=lambda c: row_counts(c),
        expect_error='NO_EFFECT',
    )
    rec.expect('拒绝后日志仍是 2 条', table_count(conn, 'ledger2_op_log'), 2)

    rec.step(
        '撤销「补 t8」这次初始化',
        'undo(conn)',
        '初始化也是一次普通操作：撤销 = 逆原语序列。t8 的人工字段、归属行消失，节点和别的交易不动。',
        lambda: undo(conn),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('撤销后归属行回到 7 行', table_count(conn, 'ledger2_tx'), 7)
    rec.expect('t8 又变成未初始化', facts.is_initialized(conn, 8), False)
    rec.expect('读出里只剩 7 笔', sorted(facts.load_txs(conn)), [1, 2, 3, 4, 5, 6, 7])
    rec.expect('根值回到 7 笔', root.value, ROOT_FULL)

    rec.step(
        '重做',
        'redo(conn)',
        '重做 = 再正着执行一遍原语序列。t8 回到树里。',
        lambda: redo(conn),
        observe=lambda c: obs_full(c),
    )
    rec.expect('重做后 t8 又初始化了', facts.is_initialized(conn, 8), True)
    rec.expect('重做后根值', read_ledger(conn).value, (38500, 805000))
    return finish(conn, tmp, outdir, init_dir, rec)


# ── 场景 J：自定义初始化 op ──────────────────────────────────────────────────

def scenario_J(outdir: Path) -> dict:
    """J 自定义初始化：节点 / 放置 / 组同父 / 分裂 / 成员是 t′ / 标题备注 / 原子拒绝。"""
    tmp, conn, rec, init_dir = begin_raw(
        outdir, 'J', DATA,
        '同 I：7 笔虚构交易，一笔都没初始化。初始化预设只是一种「拼 op 的方式」；'
        '这里直接手写 op，把节点、放置、分裂、+2 组、人工字段一次配好。')
    ids: dict = {}

    def first():
        out = init_txs(conn, [1, 3], {
            'nodes': [{'ref': 'eat', 'name': '吃饭', 'note': '工作日午餐'},
                      {'ref': 'brk', 'name': '早餐', 'parent': 'eat'}],
            'place': [{'node': 'brk', 'txs': [1]}],
            'meta': {1: {'title': ' 早饭 ', 'note': '加蛋'}},
        })
        ids.update(eat=out['nodes']['eat'], brk=out['nodes']['brk'])

    rec.step(
        '初始化 t1、t3：建两层节点，t1 放进「早餐」，t3 不指定就放根下',
        "out = init_txs(conn, [1, 3], {\n"
        "  'nodes': [{'ref': 'eat', 'name': '吃饭'}, {'ref': 'brk', 'name': '早餐', 'parent': 'eat'}],\n"
        "  'place': [{'node': 'brk', 'txs': [1]}],\n"
        "  'meta':  {1: {'title': ' 早饭 ', 'note': '加蛋'}},\n"
        "})",
        'op 的节点可以引用「这次刚建的节点」（ref），父节点可以是根、已有节点 id、或前面的 ref。'
        '没在 `place` 里提到的交易默认放**根**下。`meta` 的标题 / 备注会 trim。'
        '注意 category_path 是**初始化那一刻的完整路径**（根以下各级名字）。',
        first,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t1 在「早餐」下；t3 在根下',
               (placement(conn, 1), placement(conn, 3)), ((ids['brk'], None), ('root', None)), plain=True)
    rec.expect('t1 的标题 / 备注（trim 过）', (manual_row(conn, 1)['title'], manual_row(conn, 1)['note']),
               ('早饭', '加蛋'), plain=True)
    rec.expect('t3 没给 meta → 标题备注为空、分类路径为空',
               (manual_row(conn, 3)['title'], manual_row(conn, 3)['note'], manual_row(conn, 3)['category_path']),
               ('', '', '[]'), plain=True)
    rec.expect('节点的备注在初始化时写进去了', find_node(root, ids['eat']).note, '工作日午餐')
    rec.expect('t1 的分类路径 = 吃饭 / 早餐', facts.load_txs(conn)[1].category_path, ['吃饭', '早餐'])
    rec.expect('t3 的分类路径 = 空（放在根下）', facts.load_txs(conn)[3].category_path, [])
    rec.expect('根值 = t1 30.00 + t3 20.00 支出', root.value, (5000, 0))
    rec.expect('节点 3 行（根 + 吃饭 + 早餐）', table_count(conn, 'ledger2_node'), 3)

    def second():
        init_txs(conn, [2], {'place': [{'node': ids['eat'], 'txs': [2]}]})

    rec.step(
        '再初始化 t2，放进**已有**节点「吃饭」',
        "init_txs(conn, [2], {'place': [{'node': ids['eat'], 'txs': [2]}]})",
        '节点既可以是这次新建的（ref），也可以是库里已有的节点 id。第二批初始化单独记一条操作。',
        second,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t2 在「吃饭」下', placement(conn, 2), (ids['eat'], None))
    rec.expect('t2 的分类路径 = 吃饭', facts.load_txs(conn)[2].category_path, ['吃饭'])
    rec.expect('「吃饭」的值 = 早餐里的 t1 (30.00 支出) + t2 (8000.00 收入)',
               find_node(root, ids['eat']).value, (3000, 800000))
    rec.expect('操作日志 2 条', table_count(conn, 'ledger2_op_log'), 2)

    def third():
        out = init_txs(conn, [4, 6], {
            'nodes': [{'ref': 'misc', 'name': '杂项'}],
            'place': [{'node': 'misc', 'txs': [4, 6]}],
            'groups': [{'name': '退款对冲', 'txs': [4, 6]}],
        })
        ids.update(misc=out['nodes']['misc'], g1=out['groups'][0])

    rec.step(
        '初始化 t4、t6：放进同一个新节点，并建一个 +2 组',
        "init_txs(conn, [4, 6], {\n"
        "  'nodes': [{'ref': 'misc', 'name': '杂项'}],\n"
        "  'place': [{'node': 'misc', 'txs': [4, 6]}],\n"
        "  'groups': [{'name': '退款对冲', 'txs': [4, 6]}],\n"
        "})",
        'Q1：+2 组成员必须同父。t4（支出 25.00）和 t6（收入 50.00）放在同一个节点，才允许组。'
        '组的 ν = 两笔 +1 之后分量同时减掉较小者 → 净收入 25.00。'
        '组的位置 = 成员共同的父亲（杂项），显式存在 `ledger2_net_group.node_id`。',
        third,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    g1 = find_group(root, '退款对冲')
    rec.expect('组 ν(支出25, 收入50) = 净收入 25.00', g1.value, (0, 2500))
    rec.expect('组的位置 = 杂项', name_of(root, g1.node_id), '杂项')
    rec.expect('根值：t4 / t6 成组之后只贡献净收入 25.00（不成组会是 (7500, 810000)）',
               root.value, (5000, 802500))
    rec.expect('t4、t6 的归属行都指向杂项、都带着组 id',
               (placement(conn, 4), placement(conn, 6)),
               ((ids['misc'], ids['g1']), (ids['misc'], ids['g1'])), plain=True)
    rec.expect('节点 4 行（根 + 吃饭 + 早餐 + 杂项）', table_count(conn, 'ledger2_node'), 4)

    before = snapshot(conn)
    rec.step(
        '跨节点的 +2 组 → 拒绝（整批不生效）',
        "init_txs(conn, [5, 7], {\n"
        "  'nodes': [{'ref': 'a', 'name': 'M_A'}, {'ref': 'b', 'name': 'M_B'}],\n"
        "  'place': [{'node': 'a', 'txs': [5]}, {'node': 'b', 'txs': [7]}],\n"
        "  'groups': [{'name': '跨节点组', 'txs': [5, 7]}],\n"
        "})",
        'N18 / Q1：「同父约束」不能放宽 —— 被拒绝的信息要说得出各自在哪（看结果栏）。'
        '整批原子：M_A / M_B 都不会被建出来，t5 / t7 保持未初始化，账本逐行不变。',
        lambda: init_txs(conn, [5, 7], {
            'nodes': [{'ref': 'a', 'name': 'M_A'}, {'ref': 'b', 'name': 'M_B'}],
            'place': [{'node': 'a', 'txs': [5]}, {'node': 'b', 'txs': [7]}],
            'groups': [{'name': '跨节点组', 'txs': [5, 7]}],
        }),
        observe=lambda c: row_counts(c),
        expect_error='SAME_PARENT_REQUIRED',
    )
    rec.expect('拒绝后账本逐行不变', snapshot(conn) == before, True)
    rec.expect('M_A / M_B 没有落库（节点仍是 4 行）', table_count(conn, 'ledger2_node'), 4)
    rec.expect('t5、t7 仍是未初始化', [facts.is_initialized(conn, 5), facts.is_initialized(conn, 7)],
               [False, False], plain=True)

    rec.step(
        '初始化 t5 并分裂：不指定位置 → t′ 跟 t 同路径',
        "init_txs(conn, [5], {'splits': [{'tx': 5}], 'meta': {5: {'note': '月票'}}})",
        'N19：分裂的 t′ 向量反过来（支出 100.00 → 收入 100.00），默认**跟 t 在同一路径**（这里都在根下）。'
        't′ 是普通叶子，也必须有一行显式归属行。',
        lambda: init_txs(conn, [5], {'splits': [{'tx': 5}], 'meta': {5: {'note': '月票'}}}),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t5 和 t′5 都在根下',
               (placement(conn, 5), placement(conn, 5, 'mirror')), (('root', None), ('root', None)), plain=True)
    rec.expect('根下直接挂着的 primary', direct_tx_ids(root, 'primary'), [3, 5])
    rec.expect('根下直接挂着的 mirror', direct_tx_ids(root, 'mirror'), [5])
    rec.expect('分裂表 1 行', table_count(conn, 'ledger2_split_tx'), 1)
    rec.expect('根值：t5 的 100.00 支出 + t′5 的 100.00 收入', root.value, (15000, 812500))

    rec.step(
        '初始化 t7 并分裂，**显式指定** t′ 的位置：「吃饭」',
        "init_txs(conn, [7], {'splits': [{'tx': 7, 'node': ids['eat']}]})",
        't′ 的位置可以单独指定（`splits[i].node`）。t7 本身默认在根下，t′7 放进「吃饭」。'
        't7 的 category_path 仍是它自己（primary）初始化时的路径 = 空。',
        lambda: init_txs(conn, [7], {'splits': [{'tx': 7, 'node': ids['eat']}]}),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t7 在根下、t′7 在吃饭下',
               (placement(conn, 7), placement(conn, 7, 'mirror')),
               (('root', None), (ids['eat'], None)), plain=True)
    rec.expect('t′7 在读出树里的父亲是「吃饭」', tx_parent(root, 7, 'mirror'), ids['eat'])
    rec.expect('t7 的分类路径 = 空（primary 放在根下）', facts.load_txs(conn)[7].category_path, [])
    rec.expect('分裂表 2 行', table_count(conn, 'ledger2_split_tx'), 2)
    rec.expect('根值：t7 的 200.00 支出 + t′7 的 200.00 收入', root.value, (35000, 832500))
    rec.expect('「吃饭」的值 = t1 + t2 + t′7', find_node(root, ids['eat']).value, (3000, 820000))
    s6 = snapshot(conn)

    def seventh():
        add_tx(conn, id=8, tx_time='2026-04-01 09:00:00', direction='支出', amount=10,
               tx_type='餐饮', counterparty='示例小店', item='加餐')
        add_tx(conn, id=9, tx_time='2026-04-02 09:00:00', direction='支出', amount=10,
               tx_type='购物', counterparty='示例网店', item='纸巾')
        conn.commit()
        out = init_txs(conn, [8, 9], {
            'splits': [{'tx': 8}],
            'groups': [{'name': '含镜像成员的组', 'txs': ['tx:8', 'txm:8', 'tx:9']}],
        })
        ids['g2'] = out['groups'][0]

    rec.step(
        '晚到两笔：初始化 t8、t9，分裂 t8，并让**刚分出来的 t′8** 进 +2 组',
        "init_txs(conn, [8, 9], {'splits': [{'tx': 8}],\n"
        "  'groups': [{'name': '含镜像成员的组', 'txs': ['tx:8', 'txm:8', 'tx:9']}]})",
        '执行顺序固定：建节点 → 放置 → 分裂 → 组。所以组成员可以直接写 `txm:8`（这次刚分出来的 t′）。'
        'Q2：组成员可以是 t′。这里三笔都在根下：t8 支出 10、t′8 收入 10、t9 支出 10 → ν = 净支出 10.00。',
        seventh,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    g2 = find_group(root, '含镜像成员的组')
    rec.expect('组有 3 个成员，其中一个是 t′8',
               sorted((m.id, m.role) for m in g2.members),
               [(8, 'mirror'), (8, 'primary'), (9, 'primary')], plain=True)
    rec.expect('组 ν(支出10+10, 收入10) = 净支出 10.00', g2.value, (1000, 0))
    rec.expect('组在根下', g2.node_id, 'root')
    rec.expect('根值', root.value, (36000, 832500))
    s7 = snapshot(conn)

    before = snapshot(conn)
    rec.step(
        '初始化过的交易再初始化一次 → 拒绝',
        'init_txs(conn, [1])',
        '初始化标记防重复：只要批量里有一笔已初始化，整批拒绝（ALREADY_INITIALIZED），不留半截状态。',
        lambda: init_txs(conn, [1]),
        observe=lambda c: row_counts(c),
        expect_error='ALREADY_INITIALIZED',
    )
    rec.expect('拒绝后账本逐行不变', snapshot(conn) == before, True)

    def add_two():
        add_tx(conn, id=10, tx_time='2026-05-01 09:00:00', direction='支出', amount=5,
               tx_type='其他', counterparty='示例商家', item='杂项')
        add_tx(conn, id=11, tx_time='2026-05-02 09:00:00', direction='支出', amount=5,
               tx_type='其他', counterparty='示例商家', item='杂项')
        conn.commit()

    rec.step(
        '补两笔未初始化的交易（只动事实表，不动账本）',
        "add_tx(conn, id=10, ...)\nadd_tx(conn, id=11, ...)\nconn.commit()",
        '`transactions_raw` 是事实表，账本层不读没初始化的交易 —— 所以这两行此刻对读出没有任何影响。'
        '下面用它们演示几种非法 op：都被整体拒绝，不会留下半截。',
        add_two,
        observe=lambda c: row_counts(c),
    )
    rec.expect('t10、t11 在未初始化列表里（读出里没有它们）',
               [10 in facts.uninitialized_ids(conn), 11 in facts.uninitialized_ids(conn)], [True, True], plain=True)
    rec.expect('归属行仍是 9 行（t10 / t11 没有）', primary_rows(conn), 9)

    before = snapshot(conn)
    rec.step(
        '组只有 1 个成员 → 拒绝',
        "init_txs(conn, [10, 11], {'groups': [{'txs': [10]}]})",
        '一个 +2 组至少 2 个成员（R3 的另一面：1 人的组不存在）。',
        lambda: init_txs(conn, [10, 11], {'groups': [{'txs': [10]}]}),
        observe=lambda c: row_counts(c),
        expect_error='GROUP_TOO_SMALL',
    )
    rec.step(
        '同一笔在 splits 里出现两次 → 拒绝',
        "init_txs(conn, [10, 11], {'splits': [{'tx': 10}, {'tx': 10}]})",
        '一笔交易只能分裂一次。',
        lambda: init_txs(conn, [10, 11], {'splits': [{'tx': 10}, {'tx': 10}]}),
        observe=lambda c: row_counts(c),
        expect_error='ALREADY_SPLIT',
    )
    rec.step(
        '组成员不在这次初始化的范围内 → 拒绝',
        "init_txs(conn, [10, 11], {'groups': [{'txs': [10, 'tx:5']}]})",
        '组成员必须先出现在这次 init 的交易列表里（t′ 要先在 splits 里分出来）。'
        't5 早就初始化过了，不能再被这样引用。',
        lambda: init_txs(conn, [10, 11], {'groups': [{'txs': [10, 'tx:5']}]}),
        observe=lambda c: row_counts(c),
        expect_error='NOT_IN_INIT_SET',
    )
    rec.expect('三次拒绝之后账本逐行不变', snapshot(conn) == before, True)
    rec.expect('t10、t11 仍是未初始化',
               [facts.is_initialized(conn, 10), facts.is_initialized(conn, 11)], [False, False], plain=True)

    rec.step(
        '撤销最后一次初始化（含镜像成员的那个组）',
        'undo(conn)',
        '撤销 = 逆原语序列。t8 / t9 的人工字段、归属行、t′8 的分裂、那个组全部消失。',
        lambda: undo(conn),
        observe=lambda c: obs_full(c),
    )
    rec.expect('撤销后账本逐行回到第 7 步之前', snapshot(conn) == s6, True)
    rec.expect('撤销后根值回到第 7 步之前', read_ledger(conn).value, (35000, 832500))
    rec.expect('撤销后分裂表只剩 t5、t7 两次', table_count(conn, 'ledger2_split_tx'), 2)

    rec.step(
        '重做',
        'redo(conn)',
        '重做 = 再正着执行一遍原语序列。',
        lambda: redo(conn),
        observe=lambda c: obs_full(c),
    )
    rec.expect('重做后账本逐行回到第 8 步', snapshot(conn) == s7, True)
    rec.expect('重做后根值', read_ledger(conn).value, (36000, 832500))
    return finish(conn, tmp, outdir, init_dir, rec)


# ── 场景 K：卸载 ────────────────────────────────────────────────────────────

def scenario_K(outdir: Path) -> dict:
    """K 卸载：注销 4 个字段、带走 t′、组不足 2 人自动解散；可撤销 / 重做；可重新初始化。"""
    tmp, conn, rec, init_dir = begin_raw(
        outdir, 'K', DATA,
        '同 I：7 笔虚构交易，一笔都没初始化。先初始化一小批并建好组 / 分裂，再看卸载的连带效果。')
    ids: dict = {}

    def setup():
        out = init_txs(conn, [1, 2, 3], {
            'nodes': [{'ref': 'food', 'name': '餐饮'}],
            'place': [{'node': 'food', 'txs': [1, 3]}],
            'splits': [{'tx': 3}],
            'groups': [{'name': '餐饮对冲', 'txs': [1, 3]}],
        })
        ids.update(food=out['nodes']['food'], g=out['groups'][0])

    rec.step(
        '前置：初始化 t1、t2、t3；t1 + t3 同节点成组；t3 分裂出 t′3',
        "init_txs(conn, [1, 2, 3], {\n"
        "  'nodes': [{'ref': 'food', 'name': '餐饮'}],\n"
        "  'place': [{'node': 'food', 'txs': [1, 3]}],\n"
        "  'splits': [{'tx': 3}],\n"
        "  'groups': [{'name': '餐饮对冲', 'txs': [1, 3]}],\n"
        "})",
        '卸载要能处理「交易在组里」「交易分裂过」这两种情况，所以先把它们造出来。'
        't1、t3 都是支出、同向 → 组 ν 原样相加 = 支出 50.00。t′3 是收入 20.00。',
        setup,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('组的位置 = 餐饮', find_group(root, '餐饮对冲').node_id, ids['food'])
    rec.expect('组 ν = 支出 50.00', find_group(root, '餐饮对冲').value, (5000, 0))
    rec.expect('归属行 4 行：t1、t2、t3、t′3', table_count(conn, 'ledger2_tx'), 4)
    rec.expect('归属行 4 行（含 t′3），其中「已初始化」的主行 3 行', (table_count(conn, 'ledger2_tx'), primary_rows(conn)),
               (4, 3), plain=True)
    rec.expect('节点 2 行（根 + 餐饮）', table_count(conn, 'ledger2_node'), 2)
    rec.expect('根值：t1 30.00 + t3 20.00 支出、t2 8000.00 + t′3 20.00 收入',
               root.value, (5000, 802000))

    rec.step(
        '卸载 t1（它在 2 人组里）',
        'uninst_txs(conn, [1])',
        '卸载 = 注销这笔交易的 4 个初始化字段（整行人工字段删掉），并把它从树里拿走。'
        '连带：组只剩 t3 一个成员 → R3「不足 2 人自动解散」—— 组消失，t3 变回普通成员、仍留在「餐饮」下。'
        '事实表 `transactions_raw` 里的 t1 一动不动。',
        lambda: uninst_txs(conn, [1]),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t1 的人工字段整行没了（4 个字段全注销）', manual_row(conn, 1), None, plain=True)
    rec.expect('t1 的归属行没了', placement(conn, 1), None, plain=True)
    rec.expect('t1 在读出里被过滤', 1 in facts.load_txs(conn), False)
    rec.expect('t1 还在事实表里（卸载不删流水）', raw_count(conn, 1), 1)
    rec.expect('组已经解散', table_count(conn, 'ledger2_net_group'), 0)
    rec.expect('t3 还在「餐饮」下、不再属于任何组', placement(conn, 3), (ids['food'], None))
    rec.expect('操作日志最后一条是 uninst', ops.history(conn)[-1]['name'], 'uninst')
    rec.expect('根值：t1 的支出没了', root.value, (2000, 802000))

    rec.step(
        '卸载 t3（它分裂过）',
        'uninst_txs(conn, [3])',
        '卸载会连带撤销这笔交易的**分裂**：`ledger2_split_tx` 的记录删掉、t′3 的归属行删掉，'
        't′ 在读出里随之消失。节点「餐饮」被搬空，但它是普通节点，不会因为空了就被删（范围投影里才会不出现）。',
        lambda: uninst_txs(conn, [3]),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('分裂记录没了', table_count(conn, 'ledger2_split_tx'), 0)
    rec.expect('t3 和 t′3 的归属行都没了',
               (placement(conn, 3), placement(conn, 3, 'mirror')), (None, None), plain=True)
    rec.expect('t3 在读出里被过滤', sorted(facts.load_txs(conn)), [2])
    rec.expect('已初始化的主行只剩 t2', primary_rows(conn), 1)
    rec.expect('节点仍是 2 行（空的「餐饮」不自动删）', table_count(conn, 'ledger2_node'), 2)
    rec.expect('「餐饮」的值 = (0, 0)', find_node(root, ids['food']).value, (0, 0))
    rec.expect('根值：只剩 t2', root.value, (0, 800000))

    rec.step(
        '重新初始化 t1（卸载之后可以再来一次）',
        'init_txs(conn, [1])',
        '卸载把初始化标记注销了，所以同一笔交易可以重新初始化。这次不指定 op：放根下、字段为空。',
        lambda: init_txs(conn, [1]),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t1 又初始化了、这次在根下', placement(conn, 1), ('root', None))
    rec.expect('t1 的人工字段是全新的空值',
               (manual_row(conn, 1)['title'], manual_row(conn, 1)['note'], manual_row(conn, 1)['category_path']),
               ('', '', '[]'), plain=True)
    rec.expect('根值', root.value, (3000, 800000))

    def build_group2():
        out = init_txs(conn, [3, 4], {'groups': [{'name': '同向组', 'txs': [3, 4]}]})
        ids['g2'] = out['groups'][0]

    rec.step(
        '再造一个 2 人组（t3 + t4，都在根下）',
        "init_txs(conn, [3, 4], {'groups': [{'name': '同向组', 'txs': [3, 4]}]})",
        't3 支出 20.00、t4 支出 25.00，同向 → 组 ν = 支出 45.00。',
        build_group2,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('组 ν = 支出 45.00', find_group(root, '同向组').value, (4500, 0))
    rec.expect('根值', root.value, (7500, 800000))

    before = snapshot(conn)
    rec.step(
        '把整个组一起卸载',
        'uninst_txs(conn, [3, 4])',
        '组成员可以整批卸载：组跟着解散、所有人的归属行和人工字段一起注销。',
        lambda: uninst_txs(conn, [3, 4]),
        observe=lambda c: obs_full(c),
    )
    after = snapshot(conn)
    root = read_ledger(conn)
    rec.expect('组没了', table_count(conn, 'ledger2_net_group'), 0)
    rec.expect('已初始化的主行只剩 t1、t2',
               sorted(r['tx_id'] for r in conn.execute(
                   "SELECT tx_id FROM ledger2_tx WHERE role='primary'")), [1, 2])
    rec.expect('根值回到卸载前减掉 t3、t4', root.value, (3000, 800000))

    rec.step(
        '撤销这次卸载',
        'undo(conn)',
        '卸载是普通操作：撤销 = 逆原语序列。组、t3 / t4 的归属行和人工字段全部逐行回来。',
        lambda: undo(conn),
        observe=lambda c: obs_full(c),
    )
    rec.expect('撤销后账本逐行回到卸载前', snapshot(conn) == before, True)
    rec.expect('组回来了', find_group(read_ledger(conn), '同向组').value, (4500, 0))

    rec.step(
        '重做',
        'redo(conn)',
        '重做 = 再正着执行一遍原语序列。',
        lambda: redo(conn),
        observe=lambda c: obs_full(c),
    )
    rec.expect('重做后账本逐行回到卸载后', snapshot(conn) == after, True)

    before = snapshot(conn)
    rec.step('卸载空列表 → 拒绝', 'uninst_txs(conn, [])',
             '没有要卸载的交易，直接拒绝（NO_EFFECT 语义的 EMPTY_SELECTION）。',
             lambda: uninst_txs(conn, []), observe=lambda c: row_counts(c), expect_error='EMPTY_SELECTION')
    rec.step('卸载列表有重复 → 拒绝', 'uninst_txs(conn, [2, 2])',
             '同一笔不能卸载两次。', lambda: uninst_txs(conn, [2, 2]),
             observe=lambda c: row_counts(c), expect_error='DUPLICATE_TX')
    rec.step('卸载一笔没初始化的交易 → 拒绝', 'uninst_txs(conn, [4])',
             't4 刚被卸载过，现在没有初始化标记，不能卸载。',
             lambda: uninst_txs(conn, [4]), observe=lambda c: row_counts(c), expect_error='NOT_INITIALIZED')
    rec.step(
        '混着合法与非法的一批 → 整批拒绝',
        'uninst_txs(conn, [1, 4])',
        't1 合法、t4 非法 → 整批不生效（t1 仍保持初始化）。',
        lambda: uninst_txs(conn, [1, 4]),
        observe=lambda c: row_counts(c),
        expect_error='NOT_INITIALIZED',
    )
    rec.expect('四次拒绝之后账本逐行不变', snapshot(conn) == before, True)
    rec.expect('t1 仍是已初始化', facts.is_initialized(conn, 1), True)
    return finish(conn, tmp, outdir, init_dir, rec)


# ── 场景 L：读出前提与人工字段 ───────────────────────────────────────────────

L_ROWS = DATA + [
    tx(id=20, tx_time='2026-03-01 10:00:00', direction='支出', amount=40, tx_type='购物',
       counterparty='示例商城', item='鞋', tx_no='ORD-1'),
    tx(id=21, tx_time='2026-03-02 10:00:00', direction='收入', amount=40, tx_type='退款',
       counterparty='示例商城', item='退款-鞋', tx_no='ORD-1'),
]


def scenario_L(outdir: Path) -> dict:
    """L 读出前提：未初始化被过滤且不影响别人、退款靠 +2 抵消、category_path 写一次、update_manual。"""
    tmp, conn, rec, init_dir = begin_raw(
        outdir, 'L', L_ROWS,
        '7 笔虚构交易 + 一对**虚构的**退款流水（t20 支出 40.00、t21 收入 40.00，同一个订单号），'
        '全部未初始化。用来验证：读出的前提是「已初始化」；事实层不替用户做退款归并，'
        '要抵消就建 +2 组。')
    rec.expect('读出是空树', (read_ledger(conn).children, read_ledger(conn).value), ([], (0, 0)), plain=True)
    rec.expect('9 笔全在未初始化列表里', sorted(facts.uninitialized_ids(conn)),
               [1, 2, 3, 4, 5, 6, 7, 20, 21])
    rec.expect('时间范围也投影不出任何交易',
               sorted(facts.scope_by_date(conn, '2026-01-01', '2026-12-31').tx_ids), [])

    rec.step(
        '只初始化「退款」那一笔（t21），用来退款的原单 t20 还没初始化',
        'init_txs(conn, [21])',
        '事实层把每一笔交易原样交给引擎：t21 就是「收入 40.00」，不会因为 t20 存在（或不存在于账本里）而改写。'
        '退款抵消是**账本语义**，由 +2 组表达，不是事实层的隐藏规则。',
        lambda: init_txs(conn, [21]),
        observe=lambda c: obs_full(c),
    )
    t21 = facts.load_txs(conn)[21]
    rec.expect('t21 在读出里，金额 = 收入 40.00', (t21.amount, t21.direction), (4000, 1), plain=True)
    rec.expect('t21 没有自动备注', t21.note, None)
    rec.expect('原单 t20 仍是未初始化（不可见）', 20 in facts.uninitialized_ids(conn), True)
    rec.expect('根值 = t21 自己的 +40.00', read_ledger(conn).value, (0, 4000))

    rec.step(
        '按平台分类初始化剩下的 8 笔',
        'presets.init_by_platform_category(conn)',
        '预设只处理未初始化的交易，所以不会碰已经初始化的 t21。'
        '初始化之后，t20 也能被读出：支出 40.00，和 t21 是两条独立的原子。',
        lambda: presets.init_by_platform_category(conn),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('已初始化的主行 9 行', primary_rows(conn), 9)
    rec.expect('节点 = 根 + 5 个分类节点（t21 是单独初始化的，没有建分类节点）',
               table_count(conn, 'ledger2_node'), 6)
    rec.expect('t20、t21 各是 40.00 的原子',
               ((facts.load_txs(conn)[20].amount, facts.load_txs(conn)[20].direction),
                (facts.load_txs(conn)[21].amount, facts.load_txs(conn)[21].direction)),
               ((4000, -1), (4000, 1)), plain=True)
    rec.expect('根值 = 7 笔原数据的和 + 退款对两条原子',
               root.value, (ROOT_FULL[0] + 4000, ROOT_FULL[1] + 4000), plain=True)

    rec.step(
        '把 t20 移到根下，再把 t20 / t21 建成一个 +2 组（退款对冲）',
        "move(conn, ['tx:20'], 'root')\n"
        "create_net_group(conn, ['tx:20', 'tx:21'], '退款对冲')",
        'Q1：组成员必须同父，所以先把 t20 从 `cat:购物` 移到根下。'
        '组 ν = 两条原子的净额 = 支出 40.00 + 收入 40.00 = 0 —— 这才是「退款归并」。'
        '事实层没有做任何事，账本里两条交易都还在、都看得到。',
        lambda: (ops.move(conn, ['tx:20'], 'root'),
                 create_net_group(conn, ['tx:20', 'tx:21'], '退款对冲')),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('组 ν = (0, 0)', find_group(root, '退款对冲').value, (0, 0))
    rec.expect('组成员就是两条真实交易',
               sorted((m.id, m.role) for m in find_group(root, '退款对冲').members),
               [(20, 'primary'), (21, 'primary')])
    rec.expect('根值回到 7 笔原数据（退款对净额 0）', root.value, ROOT_FULL)

    rec.step(
        '改 t1 的标题 / 备注',
        "update_manual(conn, 1, title=' 午饭（改） ', note='备注一')",
        '人工字段可以后续更新（trim 之后再写）。category_path 不在这里改 —— 它是初始化那一刻的路径。',
        lambda: update_manual(conn, 1, title=' 午饭（改） ', note='备注一'),
        observe=lambda c: obs_full(c),
    )
    t1 = facts.load_txs(conn)[1]
    rec.expect('t1 的标题 / 备注更新了', (t1.title, t1.memo), ('午饭（改）', '备注一'), plain=True)
    rec.expect('t1 的分类路径没动', t1.category_path, ['餐饮'])
    rec.expect('操作日志最后一条是 update_manual', ops.history(conn)[-1]['name'], 'update_manual')
    rec.expect('更新人工字段不影响金额', read_ledger(conn).value, ROOT_FULL)

    rec.step(
        '把标题改成和现在一样 → 拒绝',
        "update_manual(conn, 1, title='午饭（改）')",
        '没有变化的更新被拒绝（NO_EFFECT），不记日志。',
        lambda: update_manual(conn, 1, title='午饭（改）'),
        observe=lambda c: row_counts(c),
        expect_error='NO_EFFECT',
    )

    def rename_and_move():
        ops.rename_node(conn, 'cat:餐饮', '吃饭')
        ops.move(conn, ['tx:1'], 'root')

    rec.step(
        '把分类节点「餐饮」改名成「吃饭」，再把 t1 移到根下',
        "rename_node(conn, 'cat:餐饮', '吃饭')\nmove(conn, ['tx:1'], 'root')",
        '分类节点是普通节点，可以改名 / 搬东西。但 `category_path` 是**初始化时写死的快照**：'
        '现在的实现**不会**因为改名 / 移动而悄悄刷新它（这是刻意的：刷新要有性能预算，后面再定）。'
        '所以 t1 的 category_path 仍是 ["餐饮"]，而树里它已经不在「吃饭」下了。',
        rename_and_move,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('树里按 id 还能找到 cat:餐饮，名字已经是「吃饭」',
               find_node(root, 'cat:餐饮').name, '吃饭')
    rec.expect('t1 现在在根下', tx_parent(root, 1), 'root')
    rec.expect('t1 的 category_path 仍是初始化那一刻的 ["餐饮"]',
               facts.load_txs(conn)[1].category_path, ['餐饮'])
    rec.expect('改名 / 移动不影响金额', root.value, ROOT_FULL)

    rec.step(
        '给节点补一条备注',
        "update_node(conn, 'cat:餐饮', note='外卖为主')",
        '节点备注不参与金额，也不影响 category_path。只给 note、不给 name 时名字不动；'
        '撤销 / 重做按原语逐列还原（用例里钉死了逐行快照）。',
        lambda: update_node(conn, 'cat:餐饮', note='外卖为主'),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('节点备注写进去了', find_node(root, 'cat:餐饮').note, '外卖为主')
    rec.expect('没给名字 → 名字不动', find_node(root, 'cat:餐饮').name, '吃饭')
    rec.expect('备注不影响金额', root.value, ROOT_FULL)

    rec.step(
        '卸载 t7',
        'uninst_txs(conn, [7])',
        '卸载之后这笔交易在所有读出里消失（包括时间范围投影），4 个人工字段整行注销；事实表不动。',
        lambda: uninst_txs(conn, [7]),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t7 的人工字段整行没了', manual_row(conn, 7), None, plain=True)
    rec.expect('t7 在读出里被过滤', 7 in facts.load_txs(conn), False)
    rec.expect('t7 的时间范围投影也查不到',
               7 in facts.scope_by_date(conn, '2026-01-01', '2026-12-31').tx_ids, False)
    rec.expect('t7 还在事实表里', raw_count(conn, 7), 1)
    rec.expect('根值少了 t7 的 200.00 支出', root.value, (17500, 805000))

    rec.step(
        '重新初始化 t7（plain init）',
        'init_txs(conn, [7])',
        '重新初始化会得到**全新**的人工字段：标题 / 备注为空、分类路径为空（放在根下）、初始化标记重新写上。'
        '这证明了卸载确实把 4 个字段都注销了，而不是只改了标记。',
        lambda: init_txs(conn, [7]),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t7 的新人工字段是空的',
               (manual_row(conn, 7)['title'], manual_row(conn, 7)['note'], manual_row(conn, 7)['category_path']),
               ('', '', '[]'), plain=True)
    rec.expect('t7 的分类路径变为空（plain init 放在根下）', facts.load_txs(conn)[7].category_path, [])
    rec.expect('根值回到全部 9 笔', root.value, ROOT_FULL)
    return finish(conn, tmp, outdir, init_dir, rec)


# ── 主入口 ──────────────────────────────────────────────────────────────────

def main() -> None:
    os.chdir(tempfile.gettempdir())        # Windows 上 cwd 会锁住输出目录
    clear_dir(OUT_ROOT)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    plan = [
        ('I_平台分类初始化', 'I · 平台分类初始化（预设）', scenario_I,
         '验证**初始化预设**：从「一笔都没初始化」的库出发，一次预设把每笔交易放进它的平台分类节点，'
         '同时写好 4 个人工字段；晚到的交易可以被补初始化；没有可初始化交易时被拒绝；'
         '整个预设是**一条操作**，可撤销 / 重做。'),
        ('J_自定义初始化', 'J · 自定义初始化 op', scenario_J,
         '验证 `[t1..tn; op]_init` 的 op 能力：建新节点 / 引用已有节点 / 嵌套路径；放置（默认根）；'
         '+2 组的**同父检查**（跨节点整批拒绝，不留痕）；分裂 t′（默认同路径 / 显式位置）；'
         '组成员可以是刚分出来的 t′；标题 / 备注；非法 op 整批原子拒绝；初始化可撤销 / 重做。'),
        ('K_卸载', 'K · 卸载（注销 4 个初始化字段）', scenario_K,
         '验证 `[t1..tn]_uninst`：人工字段整行注销、从树里拿走（但事实表不动）；'
         '连带撤销分裂、组不足 2 人自动解散（R3）；可撤销 / 重做；卸载之后可以重新初始化；'
         '非法输入（空 / 重复 / 没初始化 / 混合）整批拒绝。'),
        ('L_读出前提与人工字段', 'L · 读出前提与人工字段', scenario_L,
         '验证**读出必须以初始化前提**：未初始化的交易在所有读出里被过滤，但退款配对的口径分析仍看全部流水'
         '（原单没初始化也能认出退款）；`category_path` 初始化时写一次，之后改名 / 移动**不悄悄刷新**；'
         '`update_manual` 只改标题 / 备注、不动路径。'),
    ]
    key_points = {
        'I_平台分类初始化': '空账本 → 6 节点 / 7 归属 / 7 人工字段，1 条操作 19 条原语；t3 备注、t6 标题回退都对；'
                            '晚到的 t8 被补上、cat:餐饮 复用；重复跑 NO_EFFECT；撤销 / 重做逐行还原',
        'J_自定义初始化': '节点 / 放置 / 嵌套路径 / 标题备注都对；+2 组 ν 从 (7500,810000) 抵成 (5000,802500)；'
                          '跨节点组、单成员组、重复分裂、越界成员全部整批拒绝且逐行不变；t′ 可以进组；撤销 / 重做逐行还原',
        'K_卸载': '卸载 1 人 → 组 R3 解散；卸载分裂过的交易 → t′ 一起消失；事实表不动；可重新初始化；'
                  '整组卸载 / 撤销 / 重做逐行还原；四类非法输入整批拒绝',
        'L_读出前提与人工字段': '未初始化 = 读不出来（连时间范围也投影不到）；退款配对仍能认出没初始化的原单；'
                                'category_path 改名 / 移动后不刷新；update_manual 只动标题备注；卸载后重初始化字段归零',
    }

    for slug, title, fn, purpose in plan:
        outdir = OUT_ROOT / slug
        outdir.mkdir(parents=True, exist_ok=True)
        meta = fn(outdir)
        write_scenario_doc(outdir, meta, title, purpose, [], legend=INIT_LEGEND)
        print(f'[完成] {slug}  步骤 {len(meta["rec"].rows)}，断言 {len(meta["rec"].asserts)}')

    tests = collect_contract_tests()
    files = sorted({t['file'] for t in tests})

    index = ['# 账本代数 V2 · 初始化 / 卸载 / 人工字段（数据层）', '',
             '这份记录是**跑出来的**，不是写出来的：每一步都真的执行了 `backend.ledger2`，跑在真 schema'
             '（`backend.db.init_db`）的临时库上，CSV 是真的从 SQLite 里导出来的，关键数字由脚本里的 `assert` 钉死。', '',
             '## 这一轮改的是什么', '',
             '数据层新增了三样东西（引擎层 / 前端这一轮**没动**）：', '',
             '1. **初始化**：`[t1..tn; op]_init` —— 把交易放进树（已有或新建节点）、可选地建 +2 组 / 分裂、'
             '写 4 个人工字段；整体原子、可撤销；初始化标记防重复。',
             '2. **卸载**：`[t1..tn]_uninst` —— 注销这 4 个字段、把交易从树里拿走，连带撤销分裂、解散不足 2 人的组。',
             '3. **人工字段**：和归属信息合并进 `ledger2_tx(initialized, title, note, category_path, node_id, net_group_id)` —— '
             '**所有读出都以 `initialized=1` 的主行为前提**；`category_path` 在初始化时写一次完整路径，之后不悄悄刷新。'
             '节点的备注是 `ledger2_node.note`，可以在初始化 op 的 `nodes[].note` 里写，之后用 `update_node` 改。', '',
             '## 我具体测了什么', '',
             '四个场景（下面每个目录一个）—— 每一步的命令、含义、结果都记下来了，'
             '每张表的前后数据都在 CSV 里，文档里的「断言」表列出脚本钉死的数字：', '',
             '| 场景 | 验的是 | 关键结论 |', '|---|---|---|']
    index += [f'| [{title}]({slug}/00_操作说明.md) | {purpose.splitlines()[0].replace("验证 ", "")} '
              f'| {key_points[slug]} |' for slug, title, fn, purpose in plan]
    index += ['', '## 和 draft/04 的差别', '',
              '- **04 假设初始化已经跑完**（从「每笔交易都在树里」开始）；**06 从「一笔都没初始化」开始**，'
              '验的就是初始化和卸载本身。',
              '- 04 里操作日志在初始化之后被清空（初始化不算用户操作）；06 里初始化**就是**被测的操作，所以日志留着，'
              '用来验证「一条操作 = 一次撤销」。',
              '- 两个包用的是同一套记录件（`scripts/record_ledger2_scenarios.py` 里的 Recorder / CSV 导出等），'
              '所以格式、复现方式完全一致。', '',
              '### 每个场景目录里有什么', '', '```',
              'XX_场景名/',
              '├── 00_操作说明.md      ← 每一步：命令 / 含义 / 结果 / 观察 / 断言',
              '├── 初始/*.csv           ← 开跑前每张表的数据（7 张表都有，空表也出表头）',
              '├── 结果/*.csv           ← 跑完后**变了**的表的数据',
              '├── 结果/说明.md         ← 哪些表全程没变',
              '├── id对照.csv           ← node_id / group_id / 分类节点 id 到底是谁',
              '├── 读出树.txt           ← 全量读出来的账本（人看的）',
              '├── 读出树.json          ← 全量读出来的账本（机器看的 = HTTP/MCP 的形状）',
              '```', '',
              '## 怎么复现', '', '```bash',
              '# 在 V2 仓库根目录（ai-finance-assistant-v2）',
              f'{PY} -X utf8 scripts/record_ledger2_init_scenarios.py               # 生成本记录',
              f'{PY} -m pytest tests/test_ledger2_init.py -q                         # 初始化 / 卸载契约测试',
              f'{PY} -m pytest tests/test_ledger2*.py -q                            # 账本 V2 全部契约测试',
              '```', '',
              '> 所有数据都是**虚构的**（示例小店 / 示例公司），不碰真库 `db/bills.db`。', '',
              '## 契约测试清单', '',
              f'`tests/test_ledger2*.py` 里的 {len(tests)} 个用例（按文件、按源码顺序；清单由 `ast` 解析生成）：', '']
    for fname in files:
        group = [t for t in tests if t['file'] == fname]
        index += [f'### `tests/{fname}`（{len(group)} 个）', '',
                  '| 用例 | 所在小节 | 钉住什么 |', '|---|---|---|']
        for t in group:
            index.append(f"| `{t['name']}` | {t['section'] or '—'} | {t['doc'] or '—'} |")
        index.append('')
    index += ['## 对应的文档', '',
              '- 数据结构：`docs/设计/第二轮/05_账本数据结构.md`（含初始化 / 卸载 / 人工字段一节）',
              '- 验证记录（初始化前）：`../04_账本代数V2-验证记录/`、`../05_账本代数V2-边界场景/`', '']
    (OUT_ROOT / 'README.md').write_text('\n'.join(index), encoding='utf-8')
    print(f'\n输出目录：{OUT_ROOT}')


if __name__ == '__main__':
    main()
