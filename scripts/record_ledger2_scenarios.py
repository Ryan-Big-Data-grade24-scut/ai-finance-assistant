"""把账本代数 V2（backend.ledger2，真 schema）的四个场景跑一遍，把「每一步输入了什么、
命令是什么、结果是什么」落到 proj/draft/04_账本代数V2-验证记录/。

用法（在 V2 仓库根目录）：
    ../ai-finance-assistant/.venv/Scripts/python.exe -X utf8 scripts/record_ledger2_scenarios.py

产出结构：
    04_账本代数V2-验证记录/
        README.md                     总索引：场景表、结论、复现命令、契约测试清单
        A_基础账本/
            00_操作说明.md             每一步：命令 / 含义 / 结果 / 观察 / 断言
            初始/*.csv                 开跑前每张表的数据（7 张表，空表也出表头）
            结果/*.csv                 跑完后**变了**的表（没变的表不重复出）
            id对照.csv                 UUID / 分类节点 id 到底是谁
            读出树.txt / .json         读出来的账本
        B_建树与净额组/
        C_时间范围/
        D_分裂与撤销/

原则：
* 所有数据都是**虚构的**（示例小店 / 示例公司），不碰真库 db/bills.db，每次跑都新建临时库。
* 跑在**真 schema** 上：`backend.db.init_db` 建库，交易方向是 '支出'/'收入'、金额是元、
  分类是 `tx_type`；账本表是 `ledger2_*`。
* 关键数字用 assert 钉死（见 `Recorder.expect`），文档里的「断言」表就是这些 assert 的清单 ——
  文档说的和脚本验的是同一件事，改了行为脚本会直接失败，文档不会悄悄说谎。
* 新建 id 用固定种子的 UUID（见 `seed_uuids`）：每次跑出来的 CSV 逐字节一致，唯一会变的是
  `ledger2_op_log.created_at`（数据库自己打的墙上时钟）。
"""
from __future__ import annotations

import ast
import csv
import filecmp
import json
import os
import random
import shutil
import sqlite3
import stat
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import db as real_schema_db  # noqa: E402
from backend.ledger2 import facts, ops, presets, service, store  # noqa: E402
from backend.ledger2.algebra import yuan  # noqa: E402
from backend.ledger2.errors import LedgerError  # noqa: E402
from backend.ledger2.model import NetGroup, Node, Tx  # noqa: E402
from backend.ledger2.ops import (  # noqa: E402
    add_group_member, create_net_group, create_node, history, move, redo,
    remove_group_member, split_tx, undo,
)
from backend.ledger2.reader import read_ledger, read_scope, render, to_json  # noqa: E402

# ── 输出位置 ────────────────────────────────────────────────────────────────
REPO = Path(__file__).resolve().parents[1]
PROJ = REPO.parent
OUT_ROOT = PROJ / 'draft' / '04_账本代数V2-验证记录'

TABLES = [
    'transactions_raw', 'ledger2_node', 'ledger2_tx',
    'ledger2_net_group', 'ledger2_split_tx', 'ledger2_op_log',
]
LEDGER2_TABLES = TABLES[1:]
BUSINESS_TABLES = ['ledger2_node', 'ledger2_tx', 'ledger2_net_group', 'ledger2_split_tx']

PLATFORM_ID = 'demo'
PLATFORM_NAME = '示例支付'

PY = '../ai-finance-assistant/.venv/Scripts/python.exe'


# ── 虚构数据（真 schema：方向是文字，金额是元，分类 = tx_type）──────────────

def tx(**kw) -> dict:
    """造一笔虚构交易（金额单位：元）。"""
    d = dict(tx_time='2026-01-01 00:00:00', direction='支出', amount=1, tx_type='其他',
             counterparty='示例商家', item='示例')
    d.update(kw)
    return d


DATA = [
    tx(id=1, tx_time='2026-01-05 10:12:00', direction='支出', amount=30, tx_type='餐饮',
       counterparty='示例小店', item='午饭'),
    tx(id=2, tx_time='2026-01-08 09:00:00', direction='收入', amount=8000, tx_type='工资',
       counterparty='示例公司', item='月薪'),
    tx(id=3, tx_time='2026-01-09 19:30:00', direction='支出', amount=20, tx_type='餐饮',
       counterparty='示例小店', item='晚饭'),
    tx(id=4, tx_time='2026-02-02 12:05:00', direction='支出', amount=25, tx_type='餐饮',
       counterparty='示例小店', item='外卖'),
    tx(id=5, tx_time='2026-02-03 08:10:00', direction='支出', amount=100, tx_type='交通',
       counterparty='示例地铁', item='月票'),
    tx(id=6, tx_time='2026-01-20 21:00:00', direction='收入', amount=50, tx_type='其他收入',
       counterparty='示例商家', item='红包'),
    tx(id=7, tx_time='2026-03-05 15:00:00', direction='支出', amount=200, tx_type='购物',
       counterparty='示例网店', item='日用品'),
]
# 说明：原型里 t6 的分类叫「退款」，在真口径下（V1 的退款配对）会被当成退款流水归零，
# 所以改成「其他收入 / 红包」。分类名只是虚构数据的标签，不影响账本代数。

ROOT_FULL = (37500, 805000)   # DATA 的全量根值：支出 375.00 / 收入 8050.00（整数分）


def add_tx(conn, *, id, tx_time, direction, amount, tx_type, counterparty, item,
           remark=None, tx_no=None) -> None:
    dt = datetime.strptime(tx_time, '%Y-%m-%d %H:%M:%S')
    conn.execute(
        'INSERT INTO transactions_raw(id, source, platform_id, tx_time, direction, amount, counterparty, item, '
        'tx_type, status, tx_no, merchant_no, remark, date, month, year, weekday, hour, dedupe_key, raw_json) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (id, PLATFORM_NAME, PLATFORM_ID, tx_time, direction, amount, counterparty, item, tx_type,
         None, tx_no, None, remark,
         dt.strftime('%Y-%m-%d'), dt.strftime('%Y-%m'), dt.strftime('%Y'), dt.weekday(), dt.hour,
         f'record-ledger2-{id}-{tx_time}-{amount}', '{}'),
    )


def make_db(path, rows, init: bool = True):
    """建库（真 schema）→ 插虚构交易 → 建 ledger2 表（含根节点）→（默认）按平台分类初始化 → 清空日志。

    init=False：交易全部保持「未初始化」，给 draft/06 初始化证据包当初始状态。
    """
    path = Path(path)
    real_schema_db.init_db(path)
    conn = store.connect(path)
    conn.execute('INSERT OR IGNORE INTO platforms(id, name, kind, adapter) VALUES(?,?,?,?)',
                 (PLATFORM_ID, PLATFORM_NAME, 'wallet', 'generic_csv'))
    for row in rows:
        add_tx(conn, **row)
    conn.commit()
    store.ensure_schema(conn)
    if rows and init:
        presets.init_by_platform_category(conn)
        conn.execute('DELETE FROM ledger2_op_log')
        conn.execute("DELETE FROM sqlite_sequence WHERE name='ledger2_op_log'")
        conn.commit()
    return conn


def seed_uuids(tag: str) -> None:
    """让 ops 里新建 id 用的 uuid4 变成「固定种子的 uuid4」：同一个场景每次跑出来的 id 都一样。

    只替换脚本进程里 ops 模块的名字，不改 backend 的任何文件。
    """
    rng = random.Random(f'ledger2-evidence-{tag}')
    ops.uuid4 = lambda: uuid.UUID(int=rng.getrandbits(128), version=4)


# ── 读出树里找东西 ──────────────────────────────────────────────────────────

def find_node(node: Node, nid: str) -> Node | None:
    if node.id == nid:
        return node
    for c in node.children:
        if isinstance(c, Node):
            hit = find_node(c, nid)
            if hit is not None:
                return hit
    return None


def node_ids(node: Node) -> set[str]:
    out = {node.id}
    for c in node.children:
        if isinstance(c, Node):
            out |= node_ids(c)
    return out


def tx_parent(node: Node, tx_id: int, role: str = 'primary') -> str | None:
    """这笔交易在读出树里的父节点 id（在组里的交易，父节点 = 组所在的节点）。"""
    for c in node.children:
        if isinstance(c, Tx) and c.id == tx_id and c.role == role:
            return node.id
        if isinstance(c, NetGroup) and any(m.id == tx_id and m.role == role for m in c.members):
            return c.node_id
        if isinstance(c, Node):
            hit = tx_parent(c, tx_id, role)
            if hit is not None:
                return hit
    return None


def name_of(root: Node, nid: str | None) -> str | None:
    """节点 id → 名字（断言表里显示名字，不显示 UUID）。"""
    n = find_node(root, nid) if nid else None
    return n.name if n else None


def find_group(node: Node, name: str) -> NetGroup | None:
    for c in node.children:
        if isinstance(c, NetGroup) and c.name == name:
            return c
        if isinstance(c, Node):
            hit = find_group(c, name)
            if hit is not None:
                return hit
    return None


def direct_tx_ids(node: Node, role: str | None = None) -> list[int]:
    return sorted(c.id for c in node.children if isinstance(c, Tx) and (role is None or c.role == role))


def table_count(conn, table: str) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]


def snapshot(conn) -> dict:
    """四张业务表的逐行快照：撤销必须逐行还原，不是「看起来差不多」。"""
    out = {}
    for t in BUSINESS_TABLES:
        rows = conn.execute(f'SELECT * FROM {t}').fetchall()
        out[t] = sorted(tuple(dict(r).values()) for r in rows)
    return out


def undo_all(conn) -> int:
    """连撤到头，返回撤销了几次。"""
    n = 0
    while True:
        try:
            undo(conn)
            n += 1
        except LedgerError as e:
            assert e.code == 'NOTHING_TO_UNDO', e.code
            return n


# ── CSV 落盘 ────────────────────────────────────────────────────────────────

def dump_tables(conn, outdir: Path) -> dict[str, Path]:
    """每张表导一份 CSV（utf-8-sig）。空表也会出（只有表头），这样「零写入」看得见。"""
    outdir.mkdir(parents=True, exist_ok=True)
    files = {}
    for t in TABLES:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info({t})')]
        rows = conn.execute(f'SELECT * FROM {t}').fetchall()
        path = outdir / f'{t}.csv'
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow(['' if v is None else v for v in r])
        files[t] = path
    return files


def row_counts(conn) -> str:
    return '  '.join(f'{t}={table_count(conn, t)}' for t in TABLES)


def obs_full(conn) -> str:
    """观察 = 各表行数 + 读出树（一步看全变化）。"""
    return row_counts(conn) + '\n\n' + render(read_ledger(conn))


def write_tree(conn, outdir: Path) -> str:
    """读出树：文本 + JSON。"""
    outdir.mkdir(parents=True, exist_ok=True)
    tree = read_ledger(conn)
    text = render(tree)
    (outdir / '读出树.txt').write_text(text + '\n', encoding='utf-8')
    (outdir / '读出树.json').write_text(
        json.dumps(to_json(tree), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return text


def fmt(v, plain: bool = False) -> str:
    """断言表里的值：向量 (e, i) 同时给出整数分和元（plain=True 的普通计数二元组不做这个换算）。"""
    if not plain and isinstance(v, tuple) and len(v) == 2 and all(isinstance(x, int) for x in v):
        return f'({v[0]}, {v[1]}) 分 = 支出 {yuan(v[0])} / 收入 {yuan(v[1])}'
    return repr(v)


# ── 步骤记录器 ──────────────────────────────────────────────────────────────

class Recorder:
    """每记一步：命令、含义、执行结果；以及脚本里钉死的断言。"""

    def __init__(self, conn) -> None:
        self.conn = conn
        self.rows: list[dict] = []
        self.asserts: list[dict] = []

    def step(self, title: str, cmd: str, meaning: str, fn, observe=None, expect_error: str | None = None):
        """fn() 真正执行；observe(conn) -> str 拿这一步之后的观察结果。

        expect_error：这一步**应该**被拒绝，值是期望的错误码；不一致直接 AssertionError。
        没写 expect_error 的步骤被拒绝了，也是 AssertionError —— 不允许「悄悄失败还写进文档」。
        """
        output = ''
        try:
            fn()
            status = '成功'
            if expect_error is not None:
                raise AssertionError(f'步骤「{title}」应当被拒绝（{expect_error}），实际成功了')
        except LedgerError as e:
            if e.code != expect_error:
                raise AssertionError(f'步骤「{title}」意外被拒绝：{e.code} {e.message}') from e
            status = f'被拒绝（{e.code}）'
            output = e.message
        obs = observe(self.conn) if observe else ''
        self.rows.append({
            'no': len(self.rows) + 1, 'title': title, 'cmd': cmd, 'meaning': meaning,
            'status': status, 'output': output, 'observe': obs,
        })
        return self.rows[-1]

    def note(self, title: str, cmd: str, meaning: str, output: str):
        """纯观察，不改变状态。"""
        self.rows.append({
            'no': len(self.rows) + 1, 'title': title, 'cmd': cmd, 'meaning': meaning,
            'status': '观察', 'output': output, 'observe': '',
        })
        return self.rows[-1]

    def expect(self, desc: str, actual, expected, plain: bool = False) -> None:
        """钉死一个数字 / 事实：不相等直接 AssertionError，相等就记进文档的「断言」表。"""
        assert actual == expected, f'[步骤 {len(self.rows)}] {desc}：期望 {fmt(expected, plain)}，实际 {fmt(actual, plain)}'
        self.asserts.append({'no': len(self.rows), 'desc': desc, 'value': fmt(expected, plain)})


def begin(outdir: Path, tag: str, rows: list[dict], meaning: str):
    """每个场景的开头：建临时库、播种、记第 1 步、导「初始」CSV。"""
    seed_uuids(tag)
    tmp = Path(tempfile.mkdtemp(prefix=f'ledger2_{tag}_'))
    conn = make_db(tmp / f'{tag}.db', rows)
    rec = Recorder(conn)
    rec.step(
        f'建库 + 播种 {len(rows)} 笔虚构交易',
        f"conn = make_db(tmp/'{tag}.db', DATA)\n"
        '# = backend.db.init_db（真 schema）+ 插入 transactions_raw + store.ensure_schema（建 ledger2_* 表和根节点）',
        meaning,
        lambda: None,
        observe=lambda c: row_counts(c),
    )
    init_dir = outdir / '初始'
    dump_tables(conn, init_dir)
    return tmp, conn, rec, init_dir


def finish(conn, tmp: Path, outdir: Path, init_dir: Path, rec: Recorder) -> dict:
    """每个场景的结尾：导「结果」CSV、读出树、id 对照，关库、清临时目录。"""
    final_dir = outdir / '结果'
    final_dir.mkdir(parents=True, exist_ok=True)
    unchanged = compare_dirs(init_dir, final_dir, conn)
    write_tree(conn, outdir)
    write_id_map(conn, outdir)
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return {'rec': rec, 'init_dir': init_dir, 'final_dir': final_dir, 'unchanged': unchanged}


# ── 场景 ────────────────────────────────────────────────────────────────────

def scenario_A(outdir: Path) -> dict:
    """A 基础账本：按平台分类初始化之后，没有 M、没有 +2 —— 读一万遍也一行都不写。"""
    tmp, conn, rec, init_dir = begin(
        outdir, 'A', DATA,
        '原始表 `transactions_raw` 是「事实」，账本层只读它、不改它。播种之后跑一次**按平台分类初始化**'
        '（`presets.init_by_platform_category`，引擎层的「初始化预设」在数据层的实现）：每笔交易一行归属、'
        '每个平台分类一个节点、每笔交易一行人工字段。初始化本身不算用户操作，操作日志已清空。')
    rec.expect('初始化后 ledger2_node = 根 + 5 个分类节点', table_count(conn, 'ledger2_node'), 6)
    rec.expect('初始化后 ledger2_tx = 每笔交易一行（归属 + 人工字段同在一行）',
               table_count(conn, 'ledger2_tx'), 7)
    for t in ('ledger2_net_group', 'ledger2_split_tx', 'ledger2_op_log'):
        rec.expect(f'初始化后 {t} 是空表', table_count(conn, t), 0)

    rec.step(
        '读账本（不写任何东西）',
        'root = read_ledger(conn)',
        '把扁平行装配成树。根下面挂着 5 个分类节点 `cat:餐饮 / cat:工资 / cat:交通 / cat:其他收入 / cat:购物`：'
        '它们是初始化时**真落库的普通节点**，`cat:` 只是命名约定、读出时用它打 kind=category 的标签；'
        '可以改名、解散、搬动（后面的场景里会验证）。根节点 = 全部交易 +1（N5）。'
        '用户自己建的 M 在后面场景里叫 `M_...`。',
        lambda: None,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('根值 = 全部交易的向量之和 +1', root.value, ROOT_FULL)
    rec.expect('根下面只有分类节点，没有交易直接挂在根上',
               sorted(c.id for c in root.children),
               sorted(['cat:餐饮', 'cat:工资', 'cat:交通', 'cat:其他收入', 'cat:购物']))
    rec.expect('每个分类节点 kind=category、父亲是根',
               sorted({(c.kind, c.parent_id) for c in root.children}), [('category', 'root')])
    rec.expect('餐饮分类节点的值 = t1+t3+t4', find_node(root, 'cat:餐饮').value, (7500, 0))
    rec.expect('工资分类节点的值 = t2', find_node(root, 'cat:工资').value, (0, 800000))
    rec.expect('分类节点各自装着自己分类的交易',
               {c.name: direct_tx_ids(c) for c in root.children},
               {'餐饮': [1, 3, 4], '工资': [2], '交通': [5], '其他收入': [6], '购物': [7]})

    rec.note(
        '看根值',
        'root.value',
        '根值 = 所有交易的向量之和 +1。分量约定：(e, i) = (支出, 收入)，引擎内部是整数分。',
        f'e = {root.value[0]/100:.2f}（支出）    i = {root.value[1]/100:.2f}（收入）',
    )

    def facade_reads():
        service.get_tree(conn)
        service.get_tree(conn, '2026-01-01', '2026-01-31')

    rec.step(
        '再用门面读两次（全量 + 一月范围）',
        "service.get_tree(conn)\nservice.get_tree(conn, '2026-01-01', '2026-01-31')",
        'HTTP / MCP 都只经过 `service.get_tree`。读一次全量、读一次带日期范围的投影（范围是读时参数，不落表）。',
        facade_reads,
        observe=lambda c: row_counts(c),
    )
    jan = service.get_tree(conn, '2026-01-01', '2026-01-31')
    rec.expect('一月范围命中 4 笔（t1,t2,t3,t6），总共 7 笔',
               (jan['scope']['tx_in_scope'], jan['scope']['tx_total']), (4, 7), plain=True)

    rec.note(
        '检查读不写',
        'SELECT COUNT(*) FROM ledger2_node / ledger2_tx / ledger2_net_group / ledger2_split_tx / ledger2_op_log',
        '初始化之后，读出是**纯只读**：读了好几遍（含按范围投影）之后，各表行数与初始化完一模一样，'
        '操作日志仍然是 0（读不写日志，也不改 category_path）。',
        row_counts(conn),
    )
    rec.expect('读了多遍之后 ledger2_op_log 仍是空表', table_count(conn, 'ledger2_op_log'), 0)
    rec.expect('读了多遍之后 ledger2_node 仍是 6 行', table_count(conn, 'ledger2_node'), 6)
    rec.expect('读之前读之后，所有 ledger2 表逐字节相同（结果目录里一张 csv 都不用出）',
               compare_snapshot_equals_initial(conn, init_dir), True)
    return finish(conn, tmp, outdir, init_dir, rec)


def compare_snapshot_equals_initial(conn, init_dir: Path) -> bool:
    """当前 7 张表导出来的 CSV 与「初始」逐字节相同吗？（用临时目录导一份来比）"""
    probe = Path(tempfile.mkdtemp(prefix='ledger2_probe_'))
    try:
        dump_tables(conn, probe)
        return all(filecmp.cmp(init_dir / f'{t}.csv', probe / f'{t}.csv', shallow=False) for t in TABLES)
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def scenario_B(outdir: Path) -> dict:
    """B 建树 + 移动 + +2 净额组：LCA、显式归属、同父约束、净额效果。"""
    tmp, conn, rec, init_dir = begin(
        outdir, 'B', DATA, '同 A：播种 + 平台分类初始化完成，每笔交易都已经在树里（根 + 5 个分类节点 + 7 行归属）。')
    ids: dict = {}

    rec.step(
        '建 M_工作餐：把 t1、t3（都是餐饮）收进去',
        "m_meal = create_node(conn, 'M_工作餐', ['tx:1', 'tx:3'])",
        'N16：被选中的交易求 LCA，新 M 挂在 LCA 下。t1、t3 都在 `cat:餐饮` 下，所以 M_工作餐挂在**分类节点餐饮**下。'
        '分类节点是初始化时就落库的真节点，建 M 只多 1 行（M_工作餐）；t1、t3 的归属行原地改成指向 M。',
        lambda: ids.__setitem__('meal', create_node(conn, 'M_工作餐', ['tx:1', 'tx:3'])),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_工作餐 的父亲是分类节点 cat:餐饮', find_node(root, ids['meal']).parent_id, 'cat:餐饮')
    rec.expect('cat:餐饮 在初始化时就落了库，父亲是根',
               tuple(conn.execute("SELECT parent_id, name FROM ledger2_node WHERE id='cat:餐饮'").fetchone()),
               ('root', '餐饮'))
    rec.expect('ledger2_node 共 7 行（根 + 5 个分类节点 + M_工作餐）',
               table_count(conn, 'ledger2_node'), 7)
    rec.expect('ledger2_tx 仍是 7 行（每笔交易一行，t1/t3 只是改指向）',
               table_count(conn, 'ledger2_tx'), 7)
    rec.expect('M_工作餐 的值 = t1+t3', find_node(root, ids['meal']).value, (5000, 0))
    rec.expect('cat:餐饮 的值不变（M 也算 +1，t4 还直接挂在它下面）', find_node(root, 'cat:餐饮').value, (7500, 0))
    rec.expect('t4 还没动：仍直接挂在 cat:餐饮 下、归属行指向 cat:餐饮',
               (direct_tx_ids(find_node(root, 'cat:餐饮')),
                conn.execute('SELECT node_id FROM ledger2_tx WHERE tx_id=4').fetchone()[0]),
               ([4], 'cat:餐饮'))

    rec.step(
        '建 M_杂项：把 t5（交通）和 t7（购物）收进去',
        "m_misc = create_node(conn, 'M_杂项', ['tx:5', 'tx:7'])",
        'LCA 规则的另一半：t5 在 `cat:交通`、t7 在 `cat:购物`，两个分类节点的公共祖先只有根 → M_杂项挂在**总账根**下。'
        '同分类通常挂分类节点下，**跨分类才升到根**。搬空之后 `cat:交通` / `cat:购物` 仍是真节点：'
        '全量树里还在、值 0.00（范围投影里才会因「范围内无交易」而消失）。',
        lambda: ids.__setitem__('misc', create_node(conn, 'M_杂项', ['tx:5', 'tx:7'])),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('M_杂项 的父亲是根', find_node(root, ids['misc']).parent_id, 'root')
    rec.expect('ledger2_node 共 8 行（搬家不增删节点）', table_count(conn, 'ledger2_node'), 8)
    rec.expect('cat:交通 搬空后仍在全量树里，值 0.00、没有孩子',
               (find_node(root, 'cat:交通').value, find_node(root, 'cat:交通').children), ((0, 0), []))

    before = snapshot(conn)
    row = rec.step(
        '试着把「M_工作餐 里的 t1」和「工资分类下的 t2」放进同一个 +2 组',
        "create_net_group(conn, ['tx:1', 'tx:2'], '跨组净额')",
        'N18 + Q1：+2 组成员必须**同父**，不能放宽。t1 的父亲是 M_工作餐、t2 的父亲是分类节点「工资」，跨父直接拒绝，'
        '并且要告诉人话：各自在哪。被拒绝的操作**不写任何东西**（整个事务回滚、日志也不记）。',
        lambda: create_net_group(conn, ['tx:1', 'tx:2'], '跨组净额'),
        observe=lambda c: obs_full(c),
        expect_error='SAME_PARENT_REQUIRED',
    )
    rec.expect('拒绝信息指出了各自在哪（M_工作餐、工资）',
               ('M_工作餐' in row['output'] and '工资' in row['output']), True)
    rec.expect('拒绝后四张业务表逐行未变', snapshot(conn) == before, True)
    rec.expect('拒绝后操作日志仍是 2 条（建 M_工作餐、建 M_杂项；失败的操作不进日志）', table_count(conn, 'ledger2_op_log'), 2)

    rec.step(
        '把 t6（其他收入 50.00）挪进 M_工作餐',
        "move(conn, ['tx:6'], m_meal)",
        'N17：移动的是 top(被选项) 这一个集合，各元素保持独立、不合并。t6 原本在 `cat:其他收入`，挪完和 t1、t3 同父，才有资格组 +2。'
        '`cat:其他收入` 被搬空，同样是个空的真节点。',
        lambda: move(conn, ['tx:6'], ids['meal']),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t6 现在的父亲是 M_工作餐', name_of(root, tx_parent(root, 6)), 'M_工作餐')
    rec.expect('M_工作餐 = t1+t3+t6（+1 相加，还没成组）', find_node(root, ids['meal']).value, (5000, 5000))
    rec.expect('ledger2_tx 仍是 7 行（每笔交易一行）', table_count(conn, 'ledger2_tx'), 7)

    rec.step(
        '建 +2 组：t1（支出 30.00）+ t6（收入 50.00）',
        "g = create_net_group(conn, ['tx:1', 'tx:6'], '餐饮-内部对冲')",
        'N7/N8：给成员打标记；读出时组作为一个整体 +2，'
        'ν = 先 +1 相加、再两个分量同时减去较小者 → 支出 30 与收入 50 抵成净收入 20.00。'
        '组落在成员共同的父亲（M_工作餐）下，`ledger2_net_group.node_id` 显式存了这个位置。',
        lambda: ids.__setitem__('g', create_net_group(conn, ['tx:1', 'tx:6'], '餐饮-内部对冲')),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    g = find_group(root, '餐饮-内部对冲')
    rec.expect('组 ν(支出30, 收入50) = 净收入 20.00', g.value, (0, 2000))
    rec.expect('组的位置 = M_工作餐', name_of(root, g.node_id), 'M_工作餐')
    rec.expect('M_工作餐 = 组 (0,20.00) + t3 (20.00,0)', find_node(root, ids['meal']).value, (2000, 2000))
    rec.expect('全量根值：支出少了 30.00、收入少了 30.00', root.value, (34500, 802000))

    rec.step(
        '加成员：把 t3（支出 20.00）也加进组',
        "add_group_member(conn, g, 'tx:3')",
        'N10：+2 组只有「新建」和「增删成员」两种操作。t3 已经在 M_工作餐 下，和组同父，允许加入。'
        '加进来后组里支出 30+20=50 与收入 50 完全抵消 → ν = (0, 0)。',
        lambda: add_group_member(conn, ids['g'], 'tx:3'),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('三个成员 ν(支出30+20, 收入50) 完全抵消', find_group(root, '餐饮-内部对冲').value, (0, 0))
    rec.expect('全量根值：支出少了 50.00、收入少了 50.00（相对不成组）', root.value, (32500, 800000))

    rec.step(
        '减成员：把 t3 移出组',
        "remove_group_member(conn, g, 'tx:3')",
        'N10：减到只剩 1 个成员时组会自动解散（R3）。这里还剩 2 个，不解散，ν 又变回净收入 20.00。'
        't3 离开组之后仍在 M_工作餐 下（归属行保留，只是 `net_group_id` 变成空）。',
        lambda: remove_group_member(conn, ids['g'], 'tx:3'),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('减员后组回到净收入 20.00', find_group(root, '餐饮-内部对冲').value, (0, 2000))
    rec.expect('减员后全量根值回到成组 [t1,t6] 时的值', root.value, (34500, 802000))
    rec.expect('t3 的归属行还在、只是不再属于任何组',
               tuple(conn.execute('SELECT node_id, net_group_id FROM ledger2_tx WHERE tx_id=3').fetchone()),
               (ids['meal'], None))
    rec.expect('组还有 2 个成员',
               conn.execute('SELECT COUNT(*) FROM ledger2_tx WHERE net_group_id=?', (ids['g'],)).fetchone()[0], 2)
    return finish(conn, tmp, outdir, init_dir, rec)


def scenario_C(outdir: Path) -> dict:
    """C 时间范围：范围内没有交易的分类 / M，整个不出现。"""
    tmp, conn, rec, init_dir = begin(outdir, 'C', DATA, '同 A。')
    ids: dict = {}

    rec.step(
        '建 M_工作餐（t1、t3，都在一月）',
        "m_meal = create_node(conn, 'M_工作餐', ['tx:1', 'tx:3'])",
        '有了树才能看出「投影」的效果：范围过滤后，哪些分类和 M 该消失。M_工作餐 挂在 `cat:餐饮` 下，'
        '而 t4（二月）还直接挂在 `cat:餐饮` 下 —— 同一个分类节点里有一月的、也有二月的东西。',
        lambda: ids.__setitem__('meal', create_node(conn, 'M_工作餐', ['tx:1', 'tx:3'])),
        observe=lambda c: obs_full(c),
    )
    full = read_ledger(conn)
    rec.expect('全量根值', full.value, ROOT_FULL)
    stored = snapshot(conn)

    def scope_rec(title, a, b, meaning, label):
        scope = facts.scope_by_date(conn, a, b)
        proj = read_scope(read_ledger(conn), scope)
        rec.note(title, f"scope = scope_by_date(conn, '{a}', '{b}')\nread_scope(read_ledger(conn), scope)",
                 meaning, f'命中交易 {sorted(scope.tx_ids)}\n\n' + render(proj))
        (outdir / f'读出树_{label}.txt').write_text(render(proj) + '\n', encoding='utf-8')
        (outdir / f'读出树_{label}.json').write_text(
            json.dumps(to_json(proj), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return scope, proj

    scope1, jan = scope_rec(
        '投影成一月', '2026-01-01', '2026-01-31',
        'N14：时间范围是**读时参数**，不落表。一月命中 t1、t2、t3、t6。'
        'N11：范围内没有交易的分类**整个不出现**（不是显示成 0），根永远保留 —— 交通（二月）、购物（三月）消失；'
        '`cat:餐饮` 只剩一月的 M_工作餐（t4 是二月，被切掉）。',
        '一月')
    rec.expect('一月命中的交易', sorted(scope1.tx_ids), [1, 2, 3, 6])
    rec.expect('一月树里的节点',
               node_ids(jan), {'root', 'cat:餐饮', ids['meal'], 'cat:工资', 'cat:其他收入'})
    rec.expect('一月根值：支出 t1+t3 = 50.00、收入 t2+t6 = 8050.00', jan.value, (5000, 805000))
    rec.expect('一月的 cat:餐饮 只剩 M_工作餐（t4 被切掉）',
               (find_node(jan, 'cat:餐饮').value, direct_tx_ids(find_node(jan, 'cat:餐饮'))), ((5000, 0), []))

    scope2, feb = scope_rec(
        '投影成二月', '2026-02-01', '2026-02-28',
        '二月只有 t4、t5 命中。`cat:餐饮` 只剩 t4 —— M_工作餐 里的交易都在一月，所以 **M_工作餐 整个不出现**；'
        '工资（一月）也不出现。',
        '二月')
    rec.expect('二月命中的交易', sorted(scope2.tx_ids), [4, 5])
    rec.expect('二月树里的节点', node_ids(feb), {'root', 'cat:餐饮', 'cat:交通'})
    rec.expect('二月根值：支出 t4+t5 = 125.00', feb.value, (12500, 0))
    rec.expect('二月的 cat:餐饮 = t4', (find_node(feb, 'cat:餐饮').value, direct_tx_ids(find_node(feb, 'cat:餐饮'))),
               ((2500, 0), [4]))

    scope3, mar = scope_rec(
        '投影成三月', '2026-03-01', '2026-03-31',
        '三月只有 t7（购物）命中，其余分类和 M 全部不出现。',
        '三月')
    rec.expect('三月命中的交易', sorted(scope3.tx_ids), [7])
    rec.expect('三月树里的节点', node_ids(mar), {'root', 'cat:购物'})
    rec.expect('三月根值', mar.value, (20000, 0))

    scope4, apr = scope_rec(
        '投影成四月（一笔交易都没有）', '2026-04-01', '2026-04-30',
        '空范围：只剩根，值 (0, 0)；根永远保留。',
        '四月')
    rec.expect('四月树只有根，值 (0,0)', (node_ids(apr), apr.value, apr.children), ({'root'}, (0, 0), []))

    rec.expect('一月 + 二月 + 三月 的根值之和 = 全量根值（这批数据里没有 +2 组，范围刚好把交易分成三份）',
               tuple(sum(x) for x in zip(jan.value, feb.value, mar.value)), ROOT_FULL)
    rec.expect('读了四个范围之后四张业务表逐行未变（范围只是读时参数，不落表）', snapshot(conn) == stored, True)
    return finish(conn, tmp, outdir, init_dir, rec)


def scenario_D(outdir: Path) -> dict:
    """D 分裂 + 撤销/重做：t′ 的向量反过来；所有操作可逆。"""
    tmp, conn, rec, init_dir = begin(outdir, 'D', DATA, '同 A。')
    ids: dict = {}
    initial = snapshot(conn)

    def build():
        ids['meal'] = create_node(conn, 'M_午晚餐', ['tx:1', 'tx:3', 'tx:4'])
        ids['g'] = create_net_group(conn, ['tx:1', 'tx:3'], '餐饮-内部对冲')

    rec.step(
        '建 M + 建 +2 组（前置）',
        "m_meal = create_node(conn, 'M_午晚餐', ['tx:1', 'tx:3', 'tx:4'])\n"
        "g = create_net_group(conn, ['tx:1', 'tx:3'], '餐饮-内部对冲')",
        '前置状态：有树、有组、有归属行。这样撤销才有东西可撤。t1、t3 同向（都是支出），所以组 ν 原样相加，不抵消。',
        build,
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('组 [t1,t3] 同向不抵消 = 支出 50.00', find_group(root, '餐饮-内部对冲').value, (5000, 0))
    rec.expect('根值（组内同向，不影响）', root.value, ROOT_FULL)
    rec.expect('前置之后 ledger2_op_log 有 2 条（建 M、建组）', table_count(conn, 'ledger2_op_log'), 2)

    rec.step(
        '分裂 t3（支出 20.00）',
        'split_tx(conn, 3)',
        'N19：分裂出 t′，向量「刚好反过来」——t3 是 (2000,0)，t′3 是 (0,2000)，'
        '即支出 20.00 变成收入 20.00。t′ **继承 t 此刻的路径**（同在 M_午晚餐 下），'
        '但**不继承** +2 关系（t′ 是独立的一项，不在组里）。'
        '`ledger2_split_tx` 多 1 行；t′ 在 M_午晚餐 下需要一行 role=mirror 的归属行。',
        lambda: split_tx(conn, 3),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    mirror = next(c for c in find_node(root, ids['meal']).children if isinstance(c, Tx) and c.role == 'mirror')
    rec.expect('t′3 的向量 = 把 t3 的 (2000,0) 反过来', mirror.value(), (0, 2000))
    rec.expect('t′3 继承 t3 的路径：父亲是 M_午晚餐', name_of(root, tx_parent(root, 3, 'mirror')), 'M_午晚餐')
    rec.expect('t′3 不继承组：组成员仍是 [t1, t3]',
               sorted((m.id, m.role) for m in find_group(root, '餐饮-内部对冲').members), [(1, 'primary'), (3, 'primary')])
    rec.expect('ledger2_split_tx 1 行；归属行 8 行（7 笔 + t′3）',
               (table_count(conn, 'ledger2_split_tx'), table_count(conn, 'ledger2_tx')), (1, 8), plain=True)
    rec.expect('根值：多了一笔 20.00 收入（t3 原样保留）', root.value, (37500, 807000))

    rec.step(
        '撤销这次分裂',
        'undo(conn)',
        'Q4：撤销 = 执行这次操作的**逆原语序列**（不是快照回滚）。结果：t′3 消失，t3 原样保留。',
        lambda: undo(conn),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('撤销后 split 表空、归属行回到 7 行', (table_count(conn, 'ledger2_split_tx'), table_count(conn, 'ledger2_tx')), (0, 7), plain=True)
    rec.expect('撤销后根值回到全量值', root.value, ROOT_FULL)

    rec.step(
        '重做',
        'redo(conn)',
        '重做 = 再正着执行一遍原语序列。t′3 回来。',
        lambda: redo(conn),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('重做后 t′3 回来了（父亲仍是 M_午晚餐）', name_of(root, tx_parent(root, 3, 'mirror')), 'M_午晚餐')
    rec.expect('重做后根值', root.value, (37500, 807000))

    rec.step(
        '再分裂 t3 一次 → 拒绝',
        'split_tx(conn, 3)',
        'N19：同一个 t 只能分裂一次（`ledger2_split_tx` 主键保证）。被拒绝，不写任何东西。',
        lambda: split_tx(conn, 3),
        observe=lambda c: row_counts(c),
        expect_error='ALREADY_SPLIT',
    )
    rec.expect('拒绝后日志仍是 3 条（建 M、建组、分裂）', table_count(conn, 'ledger2_op_log'), 3)

    rec.step(
        '分裂一笔「没被动过」的交易 t5（交通 100.00）',
        'split_tx(conn, 5)',
        't5 挂在 `cat:交通` 下（初始化时写的归属行）。分裂后 t′5 继承它此刻的路径 = 也在 `cat:交通` 下；'
        't′ 和 t 一样必须有显式归属行，所以 `ledger2_tx` 也多 1 行。',
        lambda: split_tx(conn, 5),
        observe=lambda c: obs_full(c),
    )
    root = read_ledger(conn)
    rec.expect('t′5 在分类节点「交通」下', tx_parent(root, 5, 'mirror'), 'cat:交通')
    rec.expect('cat:交通 = t5 (100.00 支出) + t′5 (100.00 收入)', find_node(root, 'cat:交通').value, (10000, 10000))
    rec.expect('split 表 2 行、归属行 9 行（t′5 也有行）、节点行数没变',
               (table_count(conn, 'ledger2_split_tx'), table_count(conn, 'ledger2_tx'),
                table_count(conn, 'ledger2_node')), (2, 9, 7))
    rec.expect('根值', root.value, (37500, 817000))

    rec.step(
        '连续撤销到头',
        "while True:\n    try: undo(conn)\n    except LedgerError: break",
        'Q4 的验收：把所有操作依次撤销，账本应该**逐行**回到最初的样子（= 初始化刚跑完的样子：根 + 5 个分类节点 + 7 行归属）。'
        '注意 `ledger2_op_log` 会留下一批 state=undone 的记录 —— 日志本身也是账本状态的一部分，'
        '所以它是唯一「撤不回去」的表（这是对的：撤销历史是事实，不该被撤销）。',
        lambda: ids.__setitem__('undone', undo_all(conn)),
        observe=lambda c: obs_full(c),
    )
    rec.expect('一共撤销了 4 次（建 M、建组、分裂 t3、分裂 t5）', ids['undone'], 4)
    rec.expect('撤到头之后四张业务表逐行等于最初', snapshot(conn) == initial, True)
    rec.expect('ledger2_node 回到初始化后的 6 行（根 + 5 个分类节点）', table_count(conn, 'ledger2_node'), 6)
    rec.expect('根值回到全量值', read_ledger(conn).value, ROOT_FULL)
    rec.expect('操作日志 4 条，全部 undone', [h['state'] for h in history(conn)], ['undone'] * 4)

    rec.note(
        '看操作日志',
        'history(conn)',
        '每次操作的原语条数。撤销把 state 改成 undone。',
        '\n'.join(f"#{h['seq']}  {h['name']:<20} {h['state']:<9} 原语 {h['prims']} 条" for h in history(conn)),
    )
    return finish(conn, tmp, outdir, init_dir, rec)


# ── 初始 / 结果 的对比 ──────────────────────────────────────────────────────

def write_id_map(conn, outdir: Path) -> None:
    """id 对照表：node_id / group_id 这些 UUID 和 `cat:<分类>` 到底是谁。

    从**当前表** + **整条操作日志**里收集：
    连「建了又撤掉」的 id 也在，这样前后两份 CSV 里的 id 都能对上名字。
    分类节点是初始化时落库的真节点，就在当前表里。
    """
    nodes: dict[str, dict] = {}
    groups: dict[str, dict] = {}
    state: dict[str, str] = {}

    for r in conn.execute('SELECT id, parent_id, name FROM ledger2_node'):
        nodes[r['id']] = dict(r)
        state[r['id']] = '在表里'
    for r in conn.execute('SELECT id, name, node_id FROM ledger2_net_group'):
        groups[r['id']] = dict(r)
        state[r['id']] = '在表里'

    # 操作日志里出现过、但已经被撤掉的
    for r in conn.execute('SELECT op_json FROM ledger2_op_log'):
        try:
            op = json.loads(r['op_json'])
        except (ValueError, TypeError):
            continue
        for p in op.get('prims', []):
            if p.get('op') == 'node_create' and p['node_id'] not in nodes:
                nodes[p['node_id']] = {'id': p['node_id'], 'parent_id': p.get('parent_id'), 'name': p.get('name')}
                state[p['node_id']] = '不在表里（日志里出现过，已撤销）'
            elif p.get('op') == 'group_create' and p['group_id'] not in groups:
                groups[p['group_id']] = {'id': p['group_id'], 'name': p.get('name'), 'node_id': p.get('node_id')}
                state[p['group_id']] = '不在表里（日志里出现过，已撤销）'


    def node_label(nid):
        if nid is None:
            return '（总账根节点）'
        n = nodes.get(nid)
        return n['name'] or '（未命名）' if n else '（未知节点）'

    rows = []
    for nid, n in nodes.items():
        kind = '总账根' if n['parent_id'] is None else ('分类节点' if facts.is_category_node(nid) else '节点(M)')
        rows.append([nid, kind, n['name'] or '', state[nid], n['parent_id'] or '', node_label(n['parent_id'])])
    for gid, g in groups.items():
        rows.append([gid, '净额组', g['name'] or '', state[gid], g['node_id'] or '', node_label(g['node_id'])])
    rows.sort(key=lambda r: (r[1], r[2], r[0]))

    with open(outdir / 'id对照.csv', 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['id', '类型', '名字', '状态', '所在节点id', '所在节点名'])
        for r in rows:
            w.writerow(r)


def compare_dirs(init_dir: Path, final_dir: Path, conn) -> list[str]:
    """结果目录：只写**变了**的表；没变的表沿用初始那份（一张 csv 就够）。"""
    unchanged = []
    dump_tables(conn, final_dir)          # 一次性全导，别在循环里导
    for t in TABLES:
        ip = init_dir / f'{t}.csv'
        fp = final_dir / f'{t}.csv'
        if filecmp.cmp(ip, fp, shallow=False):
            fp.unlink()                   # 没变 → 删掉结果那份，初始那份就是结果
            unchanged.append(t)
    lines = ['# 结果目录说明', '',
             '这里只放**跑完之后变了**的表。没变的表在 `../初始/` 里那一份就是最终结果'
             '（内容逐字节相同），不再重复出。', '',
             'CSV 里 `node_id` / `group_id` 是 UUID，分类节点 id 形如 `cat:餐饮`，查它是谁 → `../id对照.csv`。', '']
    if unchanged:
        lines += ['## 全程未变的表（见 `../初始/`）', '']
        lines += [f'- `{t}`' for t in unchanged]
        lines.append('')
    else:
        lines += ['（每张表都变了。）', '']
    (final_dir / '说明.md').write_text('\n'.join(lines), encoding='utf-8')
    return unchanged


# ── 文档 ────────────────────────────────────────────────────────────────────

LEGEND = [
    '开跑前先跑一次**按平台分类初始化**（`presets.init_by_platform_category`）：每笔交易一行归属、'
    '每个平台分类一个节点（id `cat:<分类名>`，来自 `transactions_raw.tx_type`）、每笔交易一行人工字段。'
    '用户自己建的 M 在这些场景里一律叫 `M_...`。',
    '**每笔已初始化的交易（包括分裂出的 t′）都有一行显式归属行**；没有归属行的交易在读出里直接被过滤。',
    '分类节点是初始化时落库的**普通节点**：`cat:` 只是命名约定的标签，可以改名、解散、搬动。',
    '金额：库里是元，引擎里是整数分；`(e, i)` = (支出, 收入)。树里的 `e=30.00` 是元。',
    '`ledger2_op_log.created_at` 是数据库自己打的墙上时钟，每次跑都不同；其余 CSV 逐字节可复现（id 用固定种子）。',
]


def write_scenario_doc(outdir: Path, meta: dict, title: str, purpose: str, notes: list[str],
                       legend: list[str] | None = None) -> None:
    rec = meta['rec']
    lines = [f'# {title}', '', '## 这个场景在验什么', '', purpose, '']
    lines += ['## 前置说明', '']
    for n in (LEGEND if legend is None else legend) + list(notes):
        lines.append(f'- {n}')
    lines.append('')

    lines += ['## 操作步骤', '',
              '| # | 操作 | 命令 | 含义 | 结果 |', '|---|---|---|---|---|']
    for r in rec.rows:
        cmd = r['cmd'].replace('\n', '<br>').replace('|', '\\|')
        meaning = r['meaning'].replace('\n', '<br>').replace('|', '\\|')
        output = (r['output'] or '').replace('\n', '<br>').replace('|', '\\|')
        lines.append(f"| {r['no']} | {r['title']} | `{cmd}` | {meaning} | {r['status']}"
                     + (f'<br>{output}' if output else '') + ' |')
    lines.append('')

    lines += ['## 每一步之后的观察', '']
    for r in rec.rows:
        if not r['observe']:
            continue
        lines += [f"**{r['no']}. {r['title']}**", '', '```text', r['observe'], '```', '']

    lines += ['## 脚本里钉死的数字（断言）', '',
              f'下面 {len(rec.asserts)} 条都是脚本里的 `assert`：跑到这里不相等脚本就失败、文档不会生成 —— '
              '所以这张表不是「我觉得」，是机器核过的。', '',
              '| 步骤 | 断言 | 期望值（= 实际值） |', '|---|---|---|']
    for a in rec.asserts:
        desc = a['desc'].replace('|', '\\|')
        value = a['value'].replace('|', '\\|')
        lines.append(f"| {a['no']} | {desc} | `{value}` |")
    lines.append('')

    lines += ['## 初始数据 vs 最终数据', '',
              '| 表 | 初始 | 结果 | 变化 |', '|---|---|---|---|']
    for t in TABLES:
        changed = t not in meta['unchanged']
        if changed:
            lines.append(f'| `{t}` | [初始/{t}.csv](初始/{t}.csv) | [结果/{t}.csv](结果/{t}.csv) | 变了 |')
        else:
            lines.append(f'| `{t}` | [初始/{t}.csv](初始/{t}.csv) | 同左（全程未变） | 没变 |')
    lines.append('')
    if meta['unchanged']:
        lines.append('> 没变的表只出一份 CSV（见「初始」列）—— 这就是「一张 csv 就够」的地方。')
        lines.append('')

    extra = sorted(p.name for p in outdir.glob('读出树_*.txt'))
    lines += ['## 目录里有什么', '',
              '```',
              f'{outdir.name}/',
              '├── 00_操作说明.md      ← 本文件',
              '├── 初始/               ← 开跑前每张表的数据（CSV，7 张表，空表也出表头）',
              '├── 结果/               ← 跑完后每张表的数据（CSV，只出变了的）+ 说明.md',
              '├── id对照.csv          ← node_id / group_id / 分类节点 id 到底是谁',
              '├── 读出树.txt          ← 全量读出来的账本（人看的）',
              '├── 读出树.json         ← 全量读出来的账本（机器看的 = HTTP/MCP 的形状）']
    for name in extra:
        lines.append(f'├── {name}   ← 某个时间范围下的读出（另有同名 .json）')
    lines += ['```', '']

    (outdir / '00_操作说明.md').write_text('\n'.join(lines), encoding='utf-8')


def collect_contract_tests() -> list[dict]:
    """把 tests/test_ledger2*.py 里的契约测试清单抽出来（用 ast 解析，不猜；文件增减自动跟随）。

    返回 [{'file', 'name', 'doc', 'section'}]，按文件名、源码顺序。
    """
    out = []
    for path in sorted((REPO / 'tests').glob('test_ledger2*.py')):
        text = path.read_text(encoding='utf-8')
        tree = ast.parse(text)

        # 小节标题：`# ── N2：基础账本零写入 ──` 或夹在两条 `# ═══` 之间的 `# 标题`
        sections: list[tuple[int, str]] = []
        prev_bar = False
        for i, ln in enumerate(text.splitlines(), 1):
            s = ln.strip()
            if s.startswith('# ──'):
                sections.append((i, s.lstrip('#').strip().strip('─').strip()))
                prev_bar = False
            elif s.startswith('# ═'):
                prev_bar = not prev_bar if False else True
            elif prev_bar and s.startswith('# '):
                sections.append((i, s[2:].strip()))
                prev_bar = False
            elif s:
                prev_bar = False

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'):
                sec = ''
                for ln_no, title in sections:
                    if ln_no < node.lineno:
                        sec = title
                    else:
                        break
                doc = (ast.get_docstring(node) or '').strip()
                out.append({'file': path.name, 'name': node.name, 'lineno': node.lineno,
                            'doc': doc.splitlines()[0] if doc else '', 'section': sec})
    out.sort(key=lambda d: (d['file'], d['lineno']))
    return out


def clear_dir(path: Path) -> None:
    """清空目录内容，但**保留目录本身**。

    Windows 上：如果某个进程（哪怕是打开着这个目录的终端）的 cwd 就在这个目录里，
    `shutil.rmtree` 会失败（WinError 32）。所以这里只删里面的东西，不删目录本身。
    旧场景目录、旧文件都在这里被清掉，不会有陈旧文件残留。
    """
    if not path.exists():
        return

    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    for entry in path.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, onerror=onerror)
        else:
            try:
                entry.unlink()
            except OSError:
                pass


def main() -> None:
    # 先离开输出目录（Windows 上 cwd 会锁住目录）
    os.chdir(tempfile.gettempdir())
    clear_dir(OUT_ROOT)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    plan = [
        ('A_基础账本', 'A · 基础账本：初始化之后的只读', scenario_A,
         '验证**初始化后的基础账本**：每笔交易都在树里，读出是纯只读。\n\n'
         '原始表 `transactions_raw` 是「事实」，账本层只读；账本结构全部存在 `ledger2_*` 表里。'
         '初始化（`presets.init_by_platform_category`）写入 6 行节点、7 行归属、7 行人工字段，操作日志清空；'
         '此后无论读多少遍（含范围投影），各表逐字节不变。'),
        ('B_建树与净额组', 'B · 建树、移动、+2 净额组', scenario_B,
         '验证 **N16/N17**（建与移动，LCA 落点）、**N7/N8/N10**（+2 组的标记与读出）、'
         '**N18 + Q1**（同父约束不能放宽）。\n\n'
         '重点看三件事：\n'
         '1. 同分类的交易建 M → 挂在分类节点下；跨分类 → 挂在根下。\n'
         '2. 跨父建组被**拒绝**，而且拒绝信息是给人看的（说得出各自在哪），被拒绝的操作不留痕。\n'
         '3. ν 的真实语义：**只有组里同时有收入和支出时才抵消**；同向的两笔原样相加。'),
        ('C_时间范围', 'C · 时间范围：范围外的分类和 M 整个不出现', scenario_C,
         '验证 **N14**（范围 → 最小覆盖树）和 **N11**（范围内没有交易的分类 / M **整个不出现**，'
         '不是显示成 0）。\n\n'
         '时间范围是**读时参数**，不落表 —— 同一个库可以随便换范围读，读完四张业务表逐行不变。'),
        ('D_分裂与撤销', 'D · 分裂 t′ 与撤销/重做', scenario_D,
         '验证 **N19**（分裂：向量反过来、继承路径、不继承组、只能分裂一次）和 '
         '**Q4**（所有操作可逆）。\n\n'
         '撤销不是「恢复快照」，而是**执行这次操作的逆原语序列**；'
         '所以连撤到头之后，账本逐行回到初始化刚跑完的样子。'),
    ]

    key_points = {
        'A_基础账本': '初始化后 = 根 + 5 个分类节点 + 7 行归属 + 7 行人工字段；之后读了多遍，各表逐字节不变 → '
                      '**读不写**；根值 (37500, 805000) 分',
        'B_建树与净额组': '同分类建 M 挂分类节点下，跨分类挂根；跨父建组被拒（N18/Q1）且不留痕；'
                          '组 ν = 净收入 20.00，加第三笔后完全抵消 (0,0)',
        'C_时间范围': '一月/二月/三月三个范围的根值之和 = 全量根值；范围外的分类和 M **整个不出现**（N11），读不写库',
        'D_分裂与撤销': 't′3 向量反过来 (0,2000)、继承 M 但不进组；再分裂被拒；连撤 4 次后四张业务表逐行回到最初',
    }

    for slug, title, fn, purpose in plan:
        outdir = OUT_ROOT / slug
        outdir.mkdir(parents=True, exist_ok=True)
        meta = fn(outdir)
        write_scenario_doc(outdir, meta, title, purpose, [])
        print(f'[完成] {slug}  步骤 {len(meta["rec"].rows)}，断言 {len(meta["rec"].asserts)}')

    tests = collect_contract_tests()
    files = sorted({t['file'] for t in tests})

    index = ['# 账本代数 V2 · 验证记录', '',
             '这份记录是**跑出来的**，不是写出来的：每一步都是真的执行了 `backend.ledger2`（生产包，不是原型），'
             '跑在真 schema（`backend.db.init_db`）的临时库上，CSV 是真的从 SQLite 里导出来的，'
             '关键数字由脚本里的 `assert` 钉死。', '',
             '## 我具体测了什么', '',
             '两层：', '',
             '1. **四个场景**（下面每个目录一个）—— 每一步的命令、含义、结果都记下来了，'
             '每张表的前后数据都在 CSV 里，文档里的「断言」表列出脚本钉死的数字。',
             f'2. **契约测试**（`tests/test_ledger2*.py`，现有 {len(files)} 个文件、{len(tests)} 个用例）—— '
             '逐条钉住 N1–N21 与 Q1–Q4 的行为，清单见最后一节。', '',
             '## 和原型版记录的差别', '',
             '- 跑在**真 schema** 上：交易方向是 `支出/收入` 文字、金额是元、分类是 `tx_type`；表名是 `ledger2_*`。',
             '- 树里多了**分类节点**（`cat:<分类名>`），由初始化预设建出来，是普通的真节点（可改名 / 解散 / 搬动）。'
             '同分类建 M 挂在分类节点下，跨分类才挂根；分类节点被搬空后仍留在全量树里（值 0.00，范围投影里不出现）。',
             '- 交易表 `ledger2_tx` 只有一张：**归属行和人工字段是同一行**（`node_id` / `net_group_id` + '
             '`initialized` / `title` / `note` / `category_path`）；没有行的交易在读出里直接被过滤。'
             '节点多了 `note` 列，初始化时可以写，之后用 `update_node` 改。',
             '- 操作层是 `backend.ledger2.ops`（带写后自检、撤销/重做、日志）；读层是 `reader` / `service.get_tree`。', '',
             '## 四个场景', '',
             '| 场景 | 验的是 | 关键结论 |', '|---|---|---|']
    index += [f'| [{title}]({slug}/00_操作说明.md) | {purpose.splitlines()[0].replace("验证 ", "")} '
              f'| {key_points[slug]} |' for slug, title, fn, purpose in plan]
    index += ['', '### 每个场景目录里有什么', '', '```',
              'XX_场景名/',
              '├── 00_操作说明.md      ← 每一步：命令 / 含义 / 结果 / 观察 / 断言',
              '├── 初始/*.csv           ← 开跑前每张表的数据（7 张表都有，空表也出表头）',
              '├── 结果/*.csv           ← 跑完后**变了**的表的数据',
              '├── 结果/说明.md         ← 哪些表全程没变（它们的结果 = 初始那份，一张 csv 就够）',
              '├── id对照.csv           ← node_id / group_id / 分类节点 id 到底是谁',
              '├── 读出树.txt           ← 全量读出来的账本（人看的）',
              '├── 读出树.json          ← 全量读出来的账本（机器看的 = HTTP/MCP 的形状）',
              '└── 读出树_<范围>.txt/.json ← （仅 C）某个时间范围下的读出',
              '```', '',
              '## 怎么复现', '', '```bash',
              '# 在 V2 仓库根目录（ai-finance-assistant-v2）',
              f'{PY} -m pytest tests/test_ledger2*.py -q                          # 契约测试',
              f'{PY} -X utf8 scripts/record_ledger2_scenarios.py                 # 生成本记录',
              f'{PY} -X utf8 scripts/record_ledger2_edge_scenarios.py            # 生成 05 边界场景记录',
              '```', '',
              '本记录由 `scripts/record_ledger2_scenarios.py` 生成，每次跑都新建临时库、先清掉旧目录 → '
              '无陈旧文件、可复现、可 diff（仅 `ledger2_op_log.created_at` 是墙上时钟）。', '',
              '## 跑完之后账本长什么样（B 场景结束时）', '',
              '```text']
    index.append((OUT_ROOT / 'B_建树与净额组' / '读出树.txt').read_text(encoding='utf-8').rstrip())
    index += ['```', '']

    index += ['## 契约测试清单', '',
              f'`tests/test_ledger2*.py` 里的 {len(tests)} 个用例（按文件、按源码顺序；清单由 `ast` 解析生成，文件增减会自动跟随）：', '']
    for fname in files:
        group = [t for t in tests if t['file'] == fname]
        index += [f'### `tests/{fname}`（{len(group)} 个）', '',
                  '| 用例 | 所在小节 | 钉住什么 |', '|---|---|---|']
        for t in group:
            index.append(f"| `{t['name']}` | {t['section'] or '—'} | {t['doc'] or '—'} |")
        index.append('')
    index += ['## 对应的文档', '',
              '- 结构设计：`docs/设计/第二轮/05_账本数据结构.md`（表/类/函数/Q1–Q5 的落法）',
              '- 需求锚点：`docs/设计/第二轮/账本需求原文.md`（N1–N21）', '',
              '> 所有数据都是**虚构的**（示例小店/示例公司），不碰真库 `db/bills.db`。', '']
    (OUT_ROOT / 'README.md').write_text('\n'.join(index), encoding='utf-8')
    print(f'\n输出目录：{OUT_ROOT}')


if __name__ == '__main__':
    main()
