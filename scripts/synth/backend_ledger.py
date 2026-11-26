"""用真实导入器和 V2 账本引擎，在隔离 SQLite 里跑一本合成账。

产出两本账：

* ``noisy.db``：按平台分类初始化（平台看到什么就记什么）；
* ``truth.db``：按答案层 ``ground_truth_postings.csv`` 写入人工分类后再初始化。

两本账都只读写 ``--out`` 目录下的库文件。仓库里的 ``db/bills.db`` 是用户真实账本，
本脚本在执行前后各断言一次路径，绝不允许打开它。

用法（仓库根目录）::

    python -m scripts.synth.backend_ledger \
        --dataset ../.runtime/synthetic/20261126 \
        --out ../.runtime/synthetic/20261126/backend
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from backend import db
from backend.agentcore import store as agentcore_store
from backend.importer import detect, import_rows, parse_file
from backend.importer.base import ParseIssues
from backend.ledger2 import ops as ledger_ops
from backend.ledger2 import presets as ledger_presets
from backend.ledger2 import store as ledger_store

REPO = Path(__file__).resolve().parents[2]
REAL_DB = (REPO / "db" / "bills.db").resolve()

# 现有导入器支持的三个平台导出。银行、信用卡、基金等文件不在导入器覆盖面内，
# 它们只进入答案层，不在这本后端账里。
IMPORT_GLOBS = (
    ("支付宝", "*.csv"),
    ("微信", "*.xlsx"),
    ("支付宝小荷包", "*.xlsx"),
)

NEUTRAL_ROLES = {"mirror", "settlement", "asset_move", "refund", "closed", "external_inflow"}
NEUTRAL_REASON = {
    "mirror": "镜像行：同一笔消费已在主流水记账，避免重复",
    "settlement": "账户间划转/还款，不计收支",
    "asset_move": "投资本金搬运，不计收支",
    "refund": "退款冲减，不计收入",
    "closed": "关闭/取消订单，不计支出",
    "external_inflow": "家庭外部转入，不计个人收入",
}


def _guard(path: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == REAL_DB:
        raise RuntimeError("拒绝操作真实账本 db/bills.db")
    return resolved


def _source_files(dataset: Path) -> list[Path]:
    files: list[Path] = []
    for folder, pattern in IMPORT_GLOBS:
        files.extend(sorted((dataset / folder).glob(pattern)))
    return files


def _import_all(conn: sqlite3.Connection, files: list[Path]) -> list[dict]:
    results = []
    for path in files:
        platform_id, confidence = detect(path)
        issues = ParseIssues()
        rows = parse_file(path, platform_id, issues=issues)
        if not rows:
            # 短周期里某个钱包可能整期没有流水；导入器本身拒绝空批次。
            results.append({
                "file": path.name, "platform_id": platform_id,
                "confidence": round(confidence, 2),
                "total": 0, "inserted": 0, "skipped": 0, "failed": issues.failed,
                "date_from": None, "date_to": None, "empty": True,
            })
            continue
        with db.transaction(conn) as c:
            result = import_rows(
                rows, platform_id, path.name, issues, conn=c
            )
        results.append({
            "file": path.name,
            "platform_id": platform_id,
            "confidence": round(confidence, 2),
            "total": result.total,
            "inserted": result.inserted,
            "skipped": result.skipped,
            "failed": result.failed,
            "date_from": result.date_from,
            "date_to": result.date_to,
        })
    return results


def _truth_rows(truth_csv: Path) -> list[dict]:
    with truth_csv.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _truth_category(row: dict) -> str:
    """答案层分类 → 账本叶子分类。义务支出按具体合同项拆开。"""
    cls = row["expense_class"]
    item = row["item"]
    true_cat = row["true_category"]
    if row["flow_role"] in NEUTRAL_ROLES:
        if row["flow_role"] == "mirror":
            return "中性/镜像行"
        return f"中性/{true_cat}"
    if cls == "收入":
        return "收入/投资理财收益" if "收益" in item else "收入/工资性收入"
    if cls == "义务支出":
        return f"义务支出/{item}"
    if cls in ("刚性支出", "弹性支出"):
        return f"{cls}/{true_cat}"
    return f"中性/{true_cat}"


def _category_path(category: str) -> tuple[str, str]:
    top, _, leaf = category.partition("/")
    return top, leaf or top


def _apply_truth_overrides(conn: sqlite3.Connection, truth_csv: Path) -> dict:
    """把答案层写进 tx_overrides；按 tx_no 对应导入器落库的交易。"""
    by_no: dict[str, list[int]] = defaultdict(list)
    for row in conn.execute("SELECT id, tx_no FROM transactions_raw WHERE tx_no IS NOT NULL"):
        by_no[row["tx_no"]].append(row["id"])

    matched = unmatched = skipped_neutral = 0
    seen: set[int] = set()
    for row in _truth_rows(truth_csv):
        tx_no = row["tx_no"].strip()
        ids = by_no.get(tx_no)
        if not ids:
            unmatched += 1
            continue
        for tx_id in ids:
            if tx_id in seen:
                continue
            seen.add(tx_id)
            matched += 1
            category = _truth_category(row)
            neutral = row["flow_role"] in NEUTRAL_ROLES
            if neutral:
                skipped_neutral += 1
            conn.execute(
                "INSERT INTO tx_overrides"
                "(tx_id, manual_category, is_exempt, exempt_reason, updated_by) "
                "VALUES (?,?,?,?, 'synth') "
                "ON CONFLICT(tx_id) DO UPDATE SET "
                "manual_category=excluded.manual_category, "
                "is_exempt=excluded.is_exempt, exempt_reason=excluded.exempt_reason",
                (tx_id, category, 1 if neutral else 0,
                 NEUTRAL_REASON.get(row["flow_role"], "") if neutral else None),
            )
    conn.commit()
    total_imported = conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0]
    return {
        "imported": total_imported,
        "matched": matched,
        "unmatched_truth_rows": unmatched,
        "neutral_exempted": skipped_neutral,
        "unmapped_imported": total_imported - matched,
    }


def _init_truth_tree(conn: sqlite3.Connection) -> None:
    """按答案分类建两级树：5 个收支大类 + 叶子分类，交易放叶子。"""
    rows = conn.execute(
        "SELECT t.id, t.item, t.counterparty, "
        "COALESCE(NULLIF(TRIM(o.manual_category), ''), '中性/其他') AS category "
        "FROM transactions_raw t LEFT JOIN tx_overrides o ON o.tx_id=t.id "
        "WHERE NOT EXISTS (SELECT 1 FROM ledger2_tx lt WHERE lt.tx_id=t.id)"
    ).fetchall()

    by_category: dict[str, list[int]] = defaultdict(list)
    meta: dict[int, dict] = {}
    for row in rows:
        by_category[row["category"]].append(row["id"])
        meta[row["id"]] = {
            "title": (row["item"] or row["counterparty"] or "").strip(),
            "note": "",
        }

    tops = []
    nodes = []
    seen_tops = set()
    for category in sorted(by_category):
        top, _leaf = _category_path(category)
        if top not in seen_tops:
            seen_tops.add(top)
            tops.append(top)
            nodes.append({"ref": f"top:{top}", "id": f"cat:{top}",
                          "name": top, "parent": "root"})
        nodes.append({
            "ref": f"leaf:{category}",
            "id": f"cat:{category}",
            "name": category,
            "parent": f"top:{top}",
        })

    place = [
        {"node": f"leaf:{category}", "txs": ids}
        for category, ids in sorted(by_category.items())
    ]
    ledger_ops.init_txs(
        conn,
        [tx_id for ids in by_category.values() for tx_id in ids],
        {"nodes": nodes, "place": place, "meta": meta},
    )


def _tree_report(conn: sqlite3.Connection, hierarchical: bool) -> dict:
    if hierarchical:
        sql = """
            SELECT parent.name AS bucket, leaf.name AS leaf,
                   COUNT(*) AS n, ROUND(SUM(t.amount), 2) AS amount
            FROM ledger2_tx lt
            JOIN ledger2_node leaf ON leaf.id = lt.node_id
            JOIN ledger2_node parent ON parent.id = leaf.parent_id
            JOIN transactions_raw t ON t.id = lt.tx_id
            WHERE lt.role='primary'
            GROUP BY parent.name, leaf.name
            ORDER BY amount DESC
        """
    else:
        sql = """
            SELECT n.name AS bucket, n.name AS leaf,
                   COUNT(*) AS n, ROUND(SUM(t.amount), 2) AS amount
            FROM ledger2_tx lt
            JOIN ledger2_node n ON n.id = lt.node_id
            JOIN transactions_raw t ON t.id = lt.tx_id
            WHERE lt.role='primary'
            GROUP BY n.name
            ORDER BY amount DESC
        """
    groups = [
        {"bucket": row["bucket"], "leaf": row["leaf"],
         "count": row["n"], "amount": row["amount"]}
        for row in conn.execute(sql)
    ]
    totals = Counter()
    for group in groups:
        totals[group["bucket"]] += group["amount"]

    # 账本树里镜像/划转行仍占节点（它们确实在平台流水里），
    # 但统计口径要把 is_exempt=1 的行剔掉才是真实收支。
    counted_sql = sql.replace(
        "WHERE lt.role='primary'",
        "LEFT JOIN tx_overrides o ON o.tx_id = lt.tx_id "
        "WHERE lt.role='primary' AND COALESCE(o.is_exempt, 0) = 0",
    )
    counted = Counter()
    for row in conn.execute(counted_sql):
        counted[row["bucket"]] += row["amount"]
    return {
        "initialized": conn.execute(
            "SELECT COUNT(*) FROM ledger2_tx WHERE role='primary'"
        ).fetchone()[0],
        "nodes": conn.execute("SELECT COUNT(*) FROM ledger2_node").fetchone()[0],
        "groups": groups,
        "bucket_totals": dict(totals.most_common()),
        "bucket_totals_counted": dict(counted.most_common()),
    }


def _mismatch(conn: sqlite3.Connection, truth_csv: Path) -> dict:
    """用答案层的平台分类 vs 真实分类算平台预设的错误率。"""
    truth = {}
    for row in _truth_rows(truth_csv):
        truth[row["tx_no"]] = row
    classified = wrong = 0
    for row in conn.execute(
        "SELECT t.tx_no, COALESCE(NULLIF(TRIM(o.manual_category), ''), "
        "NULLIF(TRIM(t.tx_type), ''), '其他') AS category "
        "FROM transactions_raw t LEFT JOIN tx_overrides o ON o.tx_id=t.id"
    ):
        gt = truth.get(row["tx_no"])
        if gt is None:
            continue
        classified += 1
        if gt["platform_mismatch"] == "1":
            wrong += 1
    return {
        "classified": classified,
        "mismatched": wrong,
        "rate": round(wrong / classified, 4) if classified else 0.0,
    }


def _build(dataset: Path, out_dir: Path, name: str,
           truth_csv: Path | None, mismatch_csv: Path | None = None) -> dict:
    db_path = _guard(out_dir / f"{name}.db")
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    db.init_db(db_path)
    conn = db.get_conn(db_path)
    try:
        agentcore_store.ensure_schema(conn)
        ledger_store.ensure_schema(conn)
        imports = _import_all(conn, _source_files(dataset))
        truth_info = None
        if truth_csv is not None:
            truth_info = _apply_truth_overrides(conn, truth_csv)
            _init_truth_tree(conn)
        else:
            ledger_presets.init_by_platform_category(conn)
        return {
            "db": str(db_path),
            "imports": imports,
            "truth": truth_info,
            "ledger": _tree_report(conn, hierarchical=truth_csv is not None),
            "platform_mismatch": _mismatch(conn, mismatch_csv) if mismatch_csv else None,
        }
    finally:
        conn.close()


def build_report(dataset: Path, out_dir: Path,
                 truth_csv: Path | None = None) -> dict:
    dataset = Path(dataset).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    _guard(out_dir)
    truth_csv = Path(truth_csv) if truth_csv else (
        dataset / "ground_truth" / "ground_truth_postings.csv"
    )
    if not truth_csv.is_file():
        raise FileNotFoundError(f"找不到答案层：{truth_csv}")

    noisy = _build(dataset, out_dir, "noisy", None, mismatch_csv=truth_csv)
    truth = _build(dataset, out_dir, "truth", truth_csv)
    report = {"dataset": str(dataset), "noisy": noisy, "truth": truth}
    (out_dir / "ledger_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--truth", default=None,
                        help="默认取 <dataset>/ground_truth/ground_truth_postings.csv")
    args = parser.parse_args()

    report = build_report(Path(args.dataset), Path(args.out), args.truth)
    noisy, truth = report["noisy"], report["truth"]
    out_dir = Path(args.out).resolve()

    print(f"noisy: {noisy['ledger']['initialized']} 笔，"
          f"{noisy['ledger']['nodes']} 个节点，"
          f"平台分类错误率 {noisy['platform_mismatch']['rate']:.2%}")
    print(f"truth: {truth['ledger']['initialized']} 笔，"
          f"{truth['ledger']['nodes']} 个节点")
    for bucket, amount in truth["ledger"]["bucket_totals_counted"].items():
        print(f"  {bucket}: {amount:,.2f}")
    print(f"报告：{out_dir / 'ledger_report.json'}")


if __name__ == "__main__":
    main()
