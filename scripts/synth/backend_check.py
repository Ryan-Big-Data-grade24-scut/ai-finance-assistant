"""用真实导入器 + 真实账本引擎，复算答案层的账，并与答案对账。

这是「数据本身可靠」的机器证明：不是我们自己写脚本对答案，
而是把原始账单喂给产品里那套导入器和账本引擎，再看它算出来的
收入 / 义务 / 刚性 / 弹性是不是和答案层一致。

用法（仓库根目录）::

    python -m scripts.synth.backend_check \
        --dataset ../synthetic-data/20261126-v3-through-20261125 \
        --out ../synthetic-data/20261126-v3-through-20261125/verification/backend_ledger_totals.csv
"""
from __future__ import annotations

import argparse
import csv
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

from scripts.synth.backend_ledger import build_report

# 答案层口径 → 账本树口径
BUCKETS = (
    ("收入", "income"),
    ("义务支出", "obligation"),
    ("刚性支出", "rigid"),
    ("弹性支出", "flexible"),
)


def _answer_totals(dataset: Path) -> dict[str, Decimal]:
    """答案层：月度口径累加到数据集截止月。"""
    path = dataset / "ground_truth" / "ground_truth_periods.csv"
    totals = {key: Decimal("0") for _label, key in BUCKETS}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["granularity"] != "month":
                continue
            for _label, key in BUCKETS:
                totals[key] += Decimal(row[key] or "0")
    return totals


def _ledger_totals(report: dict) -> dict[str, Decimal]:
    buckets = report["truth"]["ledger"]["bucket_totals_counted"]
    totals = {key: Decimal("0") for _label, key in BUCKETS}
    for label, key in BUCKETS:
        # 账本汇总是浮点累加，落到分位再比，避免 -0.00 这种假差异。
        totals[key] += Decimal(str(buckets.get(label, 0))).quantize(Decimal("0.01"))
    return totals


def check(dataset: Path, out_csv: Path | None = None) -> dict:
    dataset = Path(dataset).resolve()
    answer = _answer_totals(dataset)
    workdir = Path(tempfile.mkdtemp(prefix="backend-check-"))
    try:
        report = build_report(dataset, workdir / "out")
        ledger = _ledger_totals(report)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    rows = []
    ok = True
    for label, key in BUCKETS:
        expected, actual = answer[key], ledger[key]
        diff = actual - expected
        status = "PASS" if diff == 0 else "FAIL"
        ok = ok and status == "PASS"
        rows.append({
            "bucket": label,
            "expected": f"{expected:.2f}",
            "actual": f"{actual:.2f}",
            "diff": f"{diff:.2f}",
            "status": status,
        })

    imported = report["truth"]["imports"] if "imports" in report["truth"] else []
    summary = {
        "dataset": str(dataset),
        "ok": ok,
        "rows": rows,
        "imported_rows": report["truth"]["truth"]["imported"],
        "ledger_initialized": report["truth"]["ledger"]["initialized"],
        "coverage": report["coverage"],
        "platform_mismatch": report["noisy"]["platform_mismatch"],
    }
    if out_csv:
        out_csv = Path(out_csv)
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["bucket", "expected", "actual", "diff", "status"])
            writer.writeheader()
            writer.writerows(rows)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--out", default=None, help="结果 CSV 路径")
    args = parser.parse_args()
    summary = check(Path(args.dataset), Path(args.out) if args.out else None)
    for row in summary["rows"]:
        print(f"{row['bucket']}: 答案 {row['expected']} / 账本 {row['actual']} "
              f"（差 {row['diff']}） {row['status']}")
    print(f"导入 {summary['imported_rows']} 笔，初始化 {summary['ledger_initialized']} 笔")
    print("平台分类错误率：{:.2%}".format(summary["platform_mismatch"]["rate"]))
    if not summary["ok"]:
        raise SystemExit("账本复算与答案层不一致")


if __name__ == "__main__":
    main()
