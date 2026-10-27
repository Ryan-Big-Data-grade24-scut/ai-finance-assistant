"""
scripts/ledger_golden_real.py —— 真实数据的差分验证（**不进 CI**）

拿上一轮原型（Temp/md/20261015_ai-finance-impl/model/compute_v2.py）算出来的
黄金基线，用**本引擎**重算一遍，必须得到同一个数：

    消费 608.47 / 收入 2385.89 / 净增 1777.42
    区间 2026-09-30 ~ 2026-10-16，196 笔原子

为什么要单独一个脚本：
    这个基线依赖**人工标注**（哪些是账户搬运、哪些是亲情卡额度），
    这些标注不在数据库里 —— 它们来自 5 轮 onboarding 的 CSV。
    所以它进不了 CI，只能在本地跑。

怎么跑：
    LEDGER_GOLDEN_REAL=1 python scripts/ledger_golden_real.py

    产物（spec_v2.json / onboarding CSV）默认在 Temp 目录，
    路径可用环境变量 LEDGER_GOLDEN_ARTIFACTS 覆盖。

⚠️ **不污染真实库**：脚本把 bills.db 复制到临时目录再跑，
    所有标注（funding）只写进那份副本。

退出码：
    0  全部对上
    1  对不上（实现缺陷 or 口径分歧 —— 看打印的差异）
    2  环境缺失（产物找不到 / 开关没开）—— **明确报错，不静默跳过**
"""
from __future__ import annotations

import csv
import glob
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_ARTIFACTS = Path(
    r"E:\Ufolder\Current\ActionSys\Hgclass\CSSE\Temp\md\20261015_ai-finance-impl"
)

EXPECT_CONSUME = 608.47
EXPECT_INCOME = 2385.89
EXPECT_NET = 1777.42


def _fail(msg: str) -> int:
    print(f"[FAIL] {msg}")
    return 2


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def collect_internal_txnos(onboarding: Path) -> set[str]:
    """账户搬运的标注集合 —— 逐条对照原型的集合定义转写。

    原型 compute_v2.py：
        INTERNAL = 小荷包搬入/搬出（round1 四个 CSV）
                ∪ 账户搬运噪音（round4/10_账户搬运_噪音.csv）
    """
    out: set[str] = set()
    for p in sorted(glob.glob(str(onboarding / "round1_统计四个小荷包" / "*.csv"))):
        for row in _read_csv(Path(p)):
            if row.get("★资金性质") in ("搬入", "搬出"):
                out.add(row["交易单号"])
    noise = onboarding / "round4_剩余分类" / "10_账户搬运_噪音.csv"
    if noise.exists():
        out.update(r["交易单号"] for r in _read_csv(noise))
    return out


def main() -> int:
    if os.environ.get("LEDGER_GOLDEN_REAL") != "1":
        print("[SKIP] 需要显式开关：LEDGER_GOLDEN_REAL=1 python scripts/ledger_golden_real.py")
        print("       （这个脚本会读真实账单库，不该在 CI 里跑）")
        return 2

    artifacts = Path(os.environ.get("LEDGER_GOLDEN_ARTIFACTS", DEFAULT_ARTIFACTS))
    spec_path = artifacts / "model" / "spec_v2.json"
    onboarding = artifacts / "onboarding"
    db_path = ROOT / "db" / "bills.db"
    if not spec_path.exists():
        return _fail(f"找不到原型产物 {spec_path} —— 用 LEDGER_GOLDEN_ARTIFACTS 指定路径")
    if not onboarding.exists():
        return _fail(f"找不到 onboarding 目录 {onboarding}")
    if not db_path.exists():
        return _fail(f"找不到真实库 {db_path}")

    from backend import db as db_mod
    from backend.ledger import service

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    lo, hi = spec["meta"]["区间"]
    nodes = {n["id"]: n for n in spec["nodes"]}

    # ---- 复制库，标注只写进副本 ----
    tmpdir = Path(tempfile.mkdtemp(prefix="ledger_golden_"))
    tmp_db = tmpdir / "golden.db"
    shutil.copy(db_path, tmp_db)
    db_mod.init_db(tmp_db)
    db_mod.DB_PATH = tmp_db

    conn = db_mod.get_conn(tmp_db)
    try:
        # 1) 账户搬运 → funding='账户搬运'
        internal = collect_internal_txnos(onboarding)
        marked_internal = 0
        for tx_no in internal:
            row = conn.execute(
                "SELECT id FROM transactions_raw WHERE tx_no = ?", (tx_no,)
            ).fetchone()
            if row is None:
                continue
            conn.execute(
                "INSERT INTO tx_overrides (tx_id, funding, updated_by) VALUES (?, '账户搬运', 'etl') "
                "ON CONFLICT(tx_id) DO UPDATE SET funding='账户搬运', updated_by='etl'",
                (row["id"],),
            )
            marked_internal += 1

        # 2) 亲情卡 → funding='亲情卡'（原型谓词：pay_method 含「亲情卡」）
        quota_rows = conn.execute(
            "SELECT id FROM transactions_raw WHERE date BETWEEN ? AND ? "
            "AND pay_method LIKE '%亲情卡%'",
            (lo, hi),
        ).fetchall()
        for row in quota_rows:
            conn.execute(
                "INSERT INTO tx_overrides (tx_id, funding, updated_by) VALUES (?, '亲情卡', 'etl') "
                "ON CONFLICT(tx_id) DO UPDATE SET funding='亲情卡', updated_by='etl'",
                (row["id"],),
            )
        conn.commit()

        # 3) tx_no → tx_id
        txid_by_no: dict[str, int] = {}
        for row in conn.execute(
            "SELECT id, tx_no FROM transactions_raw WHERE date BETWEEN ? AND ?", (lo, hi)
        ):
            txid_by_no[row["tx_no"]] = row["id"]
    finally:
        conn.close()

    print(f"标注：账户搬运 {marked_internal} 笔，亲情卡 {len(quota_rows)} 笔")

    # ---- 建账（M₀）----
    book = service.create_book("黄金基线（真实数据）", lo, hi)
    bid = book["book_id"]
    print(f"账本 {bid}：{len(book['legs'])} 条腿，根 {book['root_e']} / {book['root_i']}")

    # ---- 按 spec 的结构建树 ----
    def base(no: str) -> str:
        return no.split("#", 1)[0]

    def ids_of(members: list[str], *, drop_mirror: bool = True) -> list[int]:
        """成员 → tx_id 列表。

        ⚠️ **镜像腿（#1）不进节点成员**：
           原型里 #0 和 #1 是同一条交易的两条腿，可以分别落在不同节点；
           引擎里同一笔交易只有「主腿 + 镜像腿」，而镜像腿按代数 I4
           **只能**待在「外部收入节点」—— 那是引擎替原型 L-额度收入 做的角色。
        """
        out = []
        for m in members:
            if drop_mirror and m.endswith("#1"):
                continue
            tx_no = base(m)
            if tx_no not in txid_by_no:
                raise SystemExit(f"spec 里的交易单号在库里找不到：{tx_no}")
            out.append(txid_by_no[tx_no])
        return sorted(set(out))

    rounds = [n for n in spec["nodes"] if n.get("level") == "round"]
    categories = [n for n in spec["nodes"] if n.get("level") == "category"]

    # 先建轮次（把它的分类成员一次性挂上去）
    round_ids: dict[str, str] = {}
    for rnd in rounds:
        member_txs: list[int] = []
        for cid in rnd["members"]:
            member_txs.extend(ids_of(nodes[cid]["members"]))
        out = service.apply_op(bid, {
            "op": "create_node",
            "parent_id": book["root_id"],
            "name": rnd["name"],
            "op_value": "ADD",
            "tx_ids": sorted(set(member_txs)),
        })
        round_ids[rnd["id"]] = next(
            n["node_id"] for n in out["nodes"] if n["name"] == rnd["name"]
        )

    # 再建分类
    cat_ids: dict[str, str] = {}
    quota_txs: list[int] = []
    for cat in categories:
        members = cat["members"]
        if members and all(m.endswith("#1") for m in members):
            # 全部是镜像腿 —— 引擎里对应「外部收入节点」，靠 split 生成
            quota_txs = ids_of(members, drop_mirror=False)
            continue
        parent_round = next(
            (r["id"] for r in rounds if cat["id"] in r["members"]), None
        )
        if parent_round is None:
            continue
        out = service.apply_op(bid, {
            "op": "create_node",
            "parent_id": round_ids[parent_round],
            "name": cat["name"],
            "op_value": "NET" if cat["op"] == "NET" else "ADD",
            "tx_ids": ids_of(members),
        })
        cat_ids[cat["id"]] = next(
            n["node_id"] for n in out["nodes"] if n["name"] == cat["name"]
        )

    # 分裂额度笔 → 镜像腿进「外部收入节点」
    for tx_id in quota_txs:
        service.apply_op(bid, {"op": "split", "tx_id": tx_id})

    # ---- 投影：按 counts_as 汇总 ----
    tree = service.get_tree(bid)
    by_id = {n["node_id"]: n for n in tree["nodes"]}
    ext = next((n for n in tree["nodes"] if n["kind"] == "external_income"), None)

    consume = 0.0
    income = 0.0
    for cat in categories:
        counts = cat.get("counts_as") or {}
        if cat["id"] == "L-额度收入":
            if ext is None:
                print("[FAIL] 没有外部收入节点 —— 分裂没生效")
                return 1
            node = by_id[ext["node_id"]]
        else:
            nid = cat_ids.get(cat["id"])
            if nid is None:
                continue
            node = by_id[nid]
        if counts.get("消费"):
            consume += node["e"]
        if counts.get("收入"):
            income += node["i"]

    consume = round(consume, 2)
    income = round(income, 2)
    net = round(income - consume, 2)

    print("\n【差分结果】")
    print(f"  消费 {consume:>10.2f}   期望 {EXPECT_CONSUME:>10.2f}   "
          f"{'OK' if abs(consume - EXPECT_CONSUME) < 0.005 else 'DIFF'}")
    print(f"  收入 {income:>10.2f}   期望 {EXPECT_INCOME:>10.2f}   "
          f"{'OK' if abs(income - EXPECT_INCOME) < 0.005 else 'DIFF'}")
    print(f"  净增 {net:>10.2f}   期望 {EXPECT_NET:>10.2f}   "
          f"{'OK' if abs(net - EXPECT_NET) < 0.005 else 'DIFF'}")

    status = service.get_status(bid)
    print(f"  不变量 {status['invariants_ok']}  "
          f"已归位 {status['placed_tx']}/{status['total_tx']}")
    if not status["invariants_ok"]:
        for inv in status["invariants"]:
            if not inv["ok"]:
                print(f"    [FAIL] {inv['name']}: {inv['message']}")

    ok = (abs(consume - EXPECT_CONSUME) < 0.005
          and abs(income - EXPECT_INCOME) < 0.005
          and abs(net - EXPECT_NET) < 0.005
          and status["invariants_ok"])
    shutil.rmtree(tmpdir, ignore_errors=True)
    print("\n[OK] 真实数据基线一致" if ok else "\n[FAIL] 真实数据基线不一致")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
