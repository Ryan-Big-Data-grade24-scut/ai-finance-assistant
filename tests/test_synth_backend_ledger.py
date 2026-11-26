"""后端账本端到端：平台预设一本、答案层一本，两本都必须能重建。

只写 pytest 的 tmp_path，绝不打开仓库里的 db/bills.db。
"""
from __future__ import annotations

import csv
import sqlite3
from collections import Counter

import pytest

from scripts.synth import generate, write_all
from scripts.synth.backend_ledger import (
    NEUTRAL_ROLES,
    REAL_DB,
    _guard,
    build_report,
)


def _expected_by_class(truth_csv, imported_nos: set[str]) -> Counter:
    expected = Counter()
    with truth_csv.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if row["tx_no"] not in imported_nos:
                continue
            if row["flow_role"] in NEUTRAL_ROLES:
                continue
            expected[row["expense_class"]] += float(row["amount"])
    return expected


def test_backend_ledger_rebuilds_truth_from_imports(tmp_path):
    dataset_dir = tmp_path / "synth"
    dataset = generate(seed=777, months=2)
    write_all(dataset, dataset_dir)

    report = build_report(dataset_dir, tmp_path / "backend")
    noisy, truth = report["noisy"], report["truth"]

    inserted = sum(item["inserted"] for item in noisy["imports"])
    assert inserted > 0
    assert noisy["ledger"]["initialized"] == inserted
    assert truth["ledger"]["initialized"] == inserted

    # 平台分类必须系统性出错，而不是个别噪声。
    assert noisy["platform_mismatch"]["rate"] >= 0.5

    # 答案层按 tx_no 覆盖到了每一笔导入行，没有漏网的平台分类。
    assert truth["truth"]["matched"] == inserted
    assert truth["truth"]["unmapped_imported"] == 0
    assert truth["truth"]["neutral_exempted"] > 0

    # 账本里实际计入的金额（剔除镜像/划转）必须逐类等于答案层。
    truth_csv = dataset_dir / "ground_truth" / "ground_truth_postings.csv"
    with sqlite3.connect(truth["db"]) as conn:
        imported_nos = {row[0] for row in conn.execute(
            "SELECT tx_no FROM transactions_raw WHERE tx_no IS NOT NULL"
        )}
    expected = _expected_by_class(truth_csv, imported_nos)

    counted = truth["ledger"]["bucket_totals_counted"]
    assert set(counted) == set(expected)
    for cls, amount in expected.items():
        assert counted[cls] == pytest.approx(amount, abs=0.01), cls

    # 镜像行、划转行仍在树里，但不进统计口径。
    all_totals = truth["ledger"]["bucket_totals"]
    assert all_totals["中性"] > 0
    assert "中性" not in counted
    assert report["coverage"]["supported_source_rows"] >= inserted
    assert len(report["coverage"]["supported_files"]) == 6
    assert len(report["coverage"]["unsupported_files"]) == 13
    refunds = truth["refund_coverage"]
    originals = {p.event_id: p for p in dataset.postings if p.flow_role == "primary"}
    pairs = [p for p in dataset.postings if p.flow_role == "refund" and p.tx_no in imported_nos
             and originals[dataset.journal.by_id[p.event_id].original].tx_no in imported_nos]
    assert refunds["refund_relation_counts"]["paired"] == len(pairs)
    for cls, values in refunds["by_class"].items():
        returned = sum(p.amount for p in pairs if originals[dataset.journal.by_id[p.event_id].original].expense_class == cls)
        assert float(values["paired_refunds"]) == float(returned)
        assert float(values["net_of_paired"]) == pytest.approx(counted.get(cls, 0) - float(returned), abs=0.001)

    with pytest.raises(FileExistsError):
        build_report(dataset_dir, tmp_path / "backend")


def test_backend_ledger_refuses_real_database(tmp_path):
    with pytest.raises(RuntimeError):
        _guard(REAL_DB)
