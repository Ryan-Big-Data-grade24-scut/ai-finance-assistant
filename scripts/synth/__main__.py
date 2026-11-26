"""命令行入口：

    python -m scripts.synth --months 12 --seed 20261126
    python -m scripts.synth --months 3 --out ../synthetic-data/last3

输出目录默认仓库上一级的 synthetic-data/<seed>-v3/，拒绝覆盖旧输出。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

from .engine import generate
from .writers import write_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成虚构的多平台账单数据")
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--start", type=str, default="2026-01-01")
    parser.add_argument("--out", type=str, default="")
    parser.add_argument("--backend", action="store_true",
                        help="再用真实导入器和 V2 账本在隔离库里跑一本平台账 + 一本答案账")
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    output_root = Path(__file__).resolve().parents[3] / "synthetic-data"
    out = Path(args.out) if args.out else output_root / f"{args.seed}-v3"
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        parser.error(f"输出目录非空或不是目录：{out}")

    dataset = generate(seed=args.seed, start=start, months=args.months)
    counts = write_all(dataset, out)

    total = sum(counts.values())
    print(f"seed={args.seed} 区间={dataset.start}~{dataset.end}")
    print(f"输出目录：{out.resolve()}")
    for name, n in counts.items():
        print(f"  {name:<16} {n:>6} 行")
    print(f"  {'合计':<16} {total:>6} 行")
    print("月度笔数（记账 / 各平台导出行）：")
    for month, n in dataset.monthly_counts().items():
        print(f"  {month}  {n:>4} / {dataset.monthly_export_rows()[month]:>4}")
    if any(v < 0 for v in dataset.balances.values()):
        print("错误：存在负余额", file=sys.stderr)
        return 1

    if args.backend:
        from .backend_ledger import build_report

        report = build_report(out, out / "backend")
        noisy, truth = report["noisy"], report["truth"]
        print("后端账本（隔离 SQLite，不碰 db/bills.db）：")
        print(f"  平台账：{noisy['ledger']['initialized']} 笔，"
              f"{noisy['ledger']['nodes']} 个节点，"
              f"平台分类错误率 {noisy['platform_mismatch']['rate']:.2%}")
        print(f"  答案账：{truth['ledger']['initialized']} 笔，"
              f"{truth['ledger']['nodes']} 个节点")
        for bucket, amount in truth["ledger"]["bucket_totals_counted"].items():
            print(f"    {bucket}: {amount:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
