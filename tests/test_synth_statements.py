"""虚构账单生成器：账务不变量 + 真实格式可导入性。"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from backend import db, importer
from scripts.synth import generate, write_all, truth


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # 文件名不能是 bills.db，否则 init_db 会装拟真种子数据。
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "iso.db")
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    db.init_db(db.DB_PATH)
    return tmp_path


@pytest.fixture(scope="module")
def dataset():
    return generate(seed=20261126, months=12)


def test_generation_is_deterministic_and_seed_sensitive():
    a = generate(seed=20261126, months=3)
    b = generate(seed=20261126, months=3)
    c = generate(seed=7, months=3)
    assert a.digest() == b.digest()
    assert a.digest() != c.digest()
    assert [(p.when, p.amount, p.tx_no) for p in a.postings] == \
           [(p.when, p.amount, p.tx_no) for p in b.postings]


def test_balances_never_negative_and_credit_within_limit(dataset):
    assert all(v >= 0 for v in dataset.balances.values()), dataset.balances
    assert dataset.balances["salary"] > 0
    assert dataset.credit_used >= 0
    assert set(dataset.credit_used_by) == {"credit", "credit2"}
    assert dataset.credit_used_by["credit"] <= Decimal("80000.00")
    assert dataset.credit_used_by["credit2"] <= Decimal("30000.00")
    assert dataset.credit_used == sum(dataset.credit_used_by.values())


def test_wallet_products_and_huabei_stay_healthy(dataset):
    """余额宝/零钱通不能为了付款被清零，花呗也不能长期顶满额度。"""
    products = dataset.wallet_products
    assert all(v >= 0 for v in products.values()), products
    assert products["alipay:余额宝"] >= Decimal("10000.00")
    assert products["wechat:零钱通"] >= Decimal("4000.00")
    assert products["alipay:理财"] > Decimal("10000.00")
    assert products["wechat:理财通"] > Decimal("10000.00")
    assert dataset.huabei_used <= Decimal("20000.00")


def test_monthly_volume_is_around_500_rows(dataset):
    """每月导出总行数 = 记账笔数 + 电商订单行（同一笔消费在两边各出现一次）。"""
    per_month: Counter[str] = Counter(p.when.strftime("%Y-%m") for p in dataset.postings)
    for p in dataset.postings:
        if p.order_channel and p.kind == "purchase":
            per_month[p.when.strftime("%Y-%m")] += 1
    assert len(per_month) == 12
    for month, count in per_month.items():
        assert 430 <= count <= 650, (month, count)
    average = sum(per_month.values()) / 12
    assert 480 <= average <= 590, average


def test_counterparties_are_stable_and_distinct(dataset):
    """同一个交易对方永远同一个（脱敏）账号，不同对方不能共用占位符。"""
    by_name: dict[str, str] = {}
    accounts = set()
    for p in dataset.by_account("alipay"):
        assert p.counterparty_account
        assert "example.com" not in p.counterparty_account
        assert by_name.setdefault(p.counterparty, p.counterparty_account) \
            == p.counterparty_account
        accounts.add(p.counterparty_account)

    assert len(accounts) >= 30
    assert any("@" in a for a in accounts if a != "/")      # 脱敏邮箱
    assert any("*" in a and "@" not in a for a in accounts)  # 脱敏手机号
    assert "/" in accounts                                   # 平台内部交易

    # 内部产品与平台另有自己的口径，不会被塞进随机商户账号。
    assert by_name["花呗"] == "/"
    assert by_name["南方薪金宝A"] == "/"
    assert by_name["淘宝"] == "/"


def test_platform_internal_products_are_visible(dataset):
    alipay = dataset.by_account("alipay")
    items = [p.item for p in alipay]
    assert sum("余额宝-自动转入" == x for x in items) == 12
    assert sum("收益发放" in x for x in items) >= 360
    assert sum(x.startswith("花呗主动还款-") for x in items) >= 10
    assert sum(x == "理财产品申购" for x in items) >= 4
    assert sum(x == "理财产品赎回" for x in items) >= 4

    pay_methods = {p.pay_method for p in alipay}
    assert {"余额宝", "花呗", "账户余额", "亲情卡(周母)",
            "支付宝小荷包(家庭开销)"} <= pay_methods
    assert any(p.pay_method.startswith("招商银行信用卡") for p in alipay)
    assert any(p.pay_method.startswith("广发银行信用卡") for p in alipay)

    # 用主账单里的「支付宝小荷包(家庭开销)」付款时，小荷包账单必须有同额同时间的支出。
    wallet_rows = {(p.when, p.amount) for p in dataset.by_account("wallet_family")}
    wallet_paid = [p for p in alipay if p.pay_method == "支付宝小荷包(家庭开销)"]
    assert wallet_paid
    assert all((p.when, p.amount) in wallet_rows for p in wallet_paid)

    wechat = dataset.by_account("wechat")
    types = {p.platform_type for p in wechat if p.platform_type}
    assert {"转入零钱通-来自零钱", "购买理财通", "理财通赎回", "零钱提现"} <= types
    assert {"零钱通", "零钱", "亲属卡(周小满)"} <= {p.pay_method for p in wechat}
    assert dataset.wechat_yield_rows
    assert {r["product"] for r in dataset.wechat_yield_rows} == {"零钱通", "理财通"}


def test_cross_account_transfers_are_paired(dataset):
    events = dataset.journal.by_id
    groups: dict[str, list] = defaultdict(list)
    for p in dataset.postings:
        if events[p.event_id].business.kind == "settlement" and not events[p.event_id].credit:
            groups[p.event_id].append(p)
    assert groups
    for event_id, rows in groups.items():
        outgoing, incoming = sorted(rows, key=lambda p: p.when)
        assert (outgoing.effect, incoming.effect) == ("out", "in")
        assert outgoing.amount == incoming.amount == events[event_id].amount
        assert 0 < (incoming.when - outgoing.when).total_seconds() <= 60
        assert outgoing.account != incoming.account
        assert outgoing.tx_no != incoming.tx_no
        assert not outgoing.merchant_no and not incoming.merchant_no


def test_salary_bonus_and_fixed_obligations(dataset):
    salaries = [p for p in dataset.postings
                if p.account == "salary" and p.item == "工资"]
    assert len(salaries) == 12
    assert {p.amount for p in salaries} == {Decimal("28000.00"), Decimal("29400.00")}
    assert sum(1 for p in salaries if p.amount == Decimal("28000.00")) == 3   # 1—3 月
    assert sum(1 for p in salaries if p.amount == Decimal("29400.00")) == 9   # 4 月起调薪

    bonus = [p for p in dataset.postings if p.item == "年终奖金"]
    assert len(bonus) == 1
    assert bonus[0].amount == Decimal("84000.00")
    assert (bonus[0].when.month, bonus[0].when.day) == (1, 20)

    def count(item: str, account: str | None = None) -> int:
        return sum(1 for p in dataset.postings
                   if p.item == item and (account is None or p.account == account))

    assert count("个人住房贷款还款", "mortgage_card") == 12
    assert count("汽车贷款月供", "salary") == 12
    assert count("消费贷分期扣款", "salary") == 12
    assert count("信用贷分期扣款", "salary") == 12
    assert count("信用卡分期还款", "credit") == 12
    assert count("物业费", "wallet_family") == 4
    assert count("停车位租金", "alipay") == 4
    assert count("学杂费", "salary") == 4
    assert count("基金定投扣款", "salary") == 12

    # 年缴保险：五种各一次，全部走主信用卡。
    policies = {p.item for p in dataset.by_account("credit") if p.category == "保险"}
    assert {"重疾险年缴保费", "百万医疗险保费", "家庭财产险保费",
            "定期寿险保费", "车辆保险保费"} <= policies

    # 生活缴费卡每月承担水电气与宽带话费。
    utilities = [p for p in dataset.by_account("living_card")
                 if p.category == "生活缴费"]
    assert len(utilities) == 48
    assert {p.item for p in utilities} == {"电费", "水费", "燃气费", "宽带及话费"}


def test_investment_accounts_are_financially_sane(dataset):
    """收益曲线要在真实量级：不能出现 -90% 的「事故」，也不能只赚不亏。"""
    pcts = [float(s["pnl_pct"]) for s in dataset.snapshots]
    assert -40 <= min(pcts) and max(pcts) <= 60, (min(pcts), max(pcts))

    assert any(p > 0 for p in pcts) and any(p < 0 for p in pcts)       # 有赚有亏

    broker = [r for r in dataset.broker_rows]
    assert {r["side"] for r in broker} == {"买入", "卖出"}
    assert all(5 <= float(r["price"]) <= 400 for r in broker), \
        [float(r["price"]) for r in broker]

    assert Decimal("30000") < dataset.balances["broker"] < Decimal("80000")
    assert Decimal("80000") < dataset.balances["fund"] < Decimal("160000")
    assert dataset.balances["wealth"] > Decimal("120000")


def test_wallet_export_keeps_real_world_false_dimension(tmp_path: Path, dataset):
    write_all(dataset, tmp_path)
    path = tmp_path / "支付宝小荷包" / "小荷包收支明细_家庭开销.xlsx"
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert '<dimension ref="A1"/>' in xml


def test_wechat_columns_and_yield_detail(tmp_path: Path, dataset):
    write_all(dataset, tmp_path)
    # 真实导出没有这张辅助表，数据集也不该多出来源文件。
    assert not (tmp_path / "交易对方对照表.csv").exists()

    wb = openpyxl.load_workbook(tmp_path / "微信" / "微信支付账单(20260101-20261231).xlsx",
                                read_only=True)
    rows = list(wb.active.iter_rows(values_only=True))
    header = next(r for r in rows if r and r[0] == "交易时间")
    assert header == ("交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)",
                      "支付方式", "当前状态", "交易单号", "商户单号", "备注")
    assert "对方账号" not in header       # 真实微信账单没有这一列

    yield_path = tmp_path / "投资" / "微信零钱通理财通收益明细.csv"
    assert yield_path.exists()
    assert len(yield_path.read_text(encoding="utf-8-sig").splitlines()) == \
        len(dataset.wechat_yield_rows) + 2


def test_verified_formats_roundtrip_through_importer(tmp_path: Path, isolated_db: Path):
    # 全年往返样例必须已结算，未来交易另有异常行测试。
    ds = generate(seed=20261126, start=date(2025, 1, 1), months=12)
    dataset_dir = tmp_path / "synth"
    write_all(ds, dataset_dir)

    cases = [
        ("alipay", dataset_dir / "支付宝" / "支付宝交易明细(20250101-20251231).csv"),
        ("wechat", dataset_dir / "微信" / "微信支付账单(20250101-20251231).xlsx"),
        ("wallet_family", dataset_dir / "支付宝小荷包" / "小荷包收支明细_家庭开销.xlsx"),
        ("wallet_travel", dataset_dir / "支付宝小荷包" / "小荷包收支明细_旅行基金.xlsx"),
        ("wallet_auto", dataset_dir / "支付宝小荷包" / "小荷包收支明细_自动攒.xlsx"),
        ("wallet_education", dataset_dir / "支付宝小荷包" / "小荷包收支明细_教育金.xlsx"),
    ]
    total_parsed = 0
    for account, path in cases:
        assert path.exists(), path
        parsed = importer.parse_file(path)
        expected_rows = [p for p in ds.by_account(account) if p.status != "交易关闭"]
        expected_sum = float(sum((p.amount for p in expected_rows), Decimal("0")))
        assert len(parsed) == len(expected_rows), (account, len(parsed), len(expected_rows))
        assert abs(sum(r.amount for r in parsed) - expected_sum) < 0.005
        assert len({r.dedupe_key for r in parsed}) == len(parsed)

        first = importer.import_file(path)
        assert (first.inserted, first.failed) == (len(parsed), 0)
        second = importer.import_file(path)
        assert (second.inserted, second.skipped) == (0, len(parsed))
        total_parsed += len(parsed)

    conn = db.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0] == total_parsed
        assert conn.execute("SELECT COUNT(DISTINCT platform_id) FROM transactions_raw").fetchone()[0] == 3
    finally:
        conn.close()


def test_alipay_footer_stats_match_exported_rows(tmp_path: Path, dataset):
    write_all(dataset, tmp_path)
    path = tmp_path / "支付宝" / "支付宝交易明细(20260101-20261231).csv"
    text = path.read_bytes().decode("gb18030")
    assert f"共{len(dataset.by_account('alipay'))}笔记录" in text

    lines = text.splitlines()
    header_idx = next(i for i, ln in enumerate(lines) if ln.startswith("交易时间,"))
    data_rows = [ln for ln in lines[header_idx + 1:] if ln.strip()]
    assert len(data_rows) == len(dataset.by_account("alipay"))


def test_platform_classification_is_deliberately_biased(dataset):
    stats = truth.platform_bias_stats(dataset)
    assert stats["classified"] > 1000
    # 用户要求：初始平台分类至少一半是错的，而且个体户不能保留真实品类。
    assert stats["rate"] >= 0.5, stats
    assert stats["individual_purchases"] > 100
    assert stats["individual_collapsed_to_merchant"] == stats["individual_purchases"]

    individuals = [p for p in dataset.postings
                   if p.counterparty_type == "individual"
                   and p.kind == "purchase"
                   and p.account in {"alipay", "wechat"}]
    assert individuals
    assert all(p.platform_category in {"其他", "商户消费", "扫二维码付款"}
               for p in individuals)


def test_ground_truth_fields_cover_every_posting(dataset):
    assert {p.expense_class for p in dataset.postings} <= set(truth.EXPENSE_CLASSES)
    assert {p.cadence for p in dataset.postings} <= set(truth.CADENCES)
    assert {p.flow_role for p in dataset.postings} <= set(truth.FLOW_ROLES)
    ids = [p.truth_id for p in dataset.postings]
    assert len(ids) == len(set(ids)) and all(ids)
    for p in dataset.postings:
        if p.flow_role != "income":
            assert p.expense_class in {"义务支出", "刚性支出", "弹性支出", "中性", "收入"}
    # 收入口径不能把退款、赎回本金、账户间搬运算进去。
    assert not [p for p in dataset.postings
                if p.flow_role == "income" and p.kind == "refund"]
    assert all(p.expense_class == "中性" for p in dataset.postings
               if p.kind == "refund")


def test_mirror_rows_link_back_and_do_not_double_count(dataset):
    by_id = {p.truth_id: p for p in dataset.postings}
    mirrors = [p for p in dataset.postings if p.flow_role == "mirror"]
    assert mirrors
    for p in mirrors:
        source = by_id[p.mirror_of]
        assert source.flow_role != "mirror"
        assert (p.when, p.amount, p.item, p.counterparty) == \
               (source.when, source.amount, source.item, source.counterparty)
        assert source.account in {"alipay", "wechat"}
    # 镜像行只出现在信用卡/小荷包自己的账单里，不能计入收支汇总。
    yearly = {r["period"]: r for r in truth.period_summary(dataset)
              if r["granularity"] == "year"}
    assert len(yearly) == 1
    row = next(iter(yearly.values()))
    income = sum((p.amount for p in dataset.postings if p.flow_role == "income"),
                 Decimal("0")) + sum(
        (r["amount"] for r in dataset.wechat_yield_rows), Decimal("0")) + sum(
        (r["amount"] for r in dataset.wealth_rows), Decimal("0"))
    outflow = sum((p.amount for p in dataset.postings
                   if p.flow_role == "primary"
                   and p.expense_class in {"义务支出", "刚性支出", "弹性支出"}),
                  Decimal("0"))
    assert row["income"] == income
    refunds = sum((p.amount for p in dataset.postings if p.flow_role == "refund"), Decimal("0"))
    assert row["outflow"] == outflow
    assert row["refunds"] == refunds > 0
    assert row["net_outflow"] == outflow - refunds
    assert row["net"] == income - outflow + refunds


def test_ground_truth_reconstructs_every_export_row(tmp_path: Path, dataset):
    write_all(dataset, tmp_path)
    payload = json.loads((tmp_path / "reconciliation.json").read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["files"] >= 19
    assert payload["export_rows"] == payload["matched"]
    assert all(row["result"] == "PASS" for row in payload["rows"])
    # 每个原始导出文件都必须被对账覆盖，不能漏掉平台。
    files = {row["file"] for row in payload["rows"]}
    assert any(f.endswith(".csv") and "支付宝" in f for f in files)
    assert any(f.endswith(".xlsx") and "微信" in f for f in files)
    assert any("信用卡" in f for f in files)
    assert any("证券_成交明细" in f for f in files)


def test_answer_document_is_a_plain_language_ledger(tmp_path: Path, dataset):
    write_all(dataset, tmp_path)
    answer = (tmp_path / "ground_truth" / "ANSWER.md").read_text(encoding="utf-8")
    for heading in ("真实收入", "义务支出", "刚性支出", "弹性支出", "平台分类偏差"):
        assert heading in answer
    for item in ("工资", "个人住房贷款还款", "日常餐食", "网购"):
        assert item in answer
    for view in ("### 年", "### 半年", "### 季", "### 月", "### 日"):
        assert view in answer

    periods = (tmp_path / "ground_truth" / "ground_truth_periods.csv").read_text(
        encoding="utf-8-sig")
    header, *lines = periods.splitlines()
    assert header == ",".join(["granularity", "period", *truth.PERIOD_TOTALS])
    assert sum(1 for ln in lines if ln.startswith("day,")) == 365
    assert sum(1 for ln in lines if ln.startswith("month,")) == 12
    assert sum(1 for ln in lines if ln.startswith("quarter,")) == 4
    assert sum(1 for ln in lines if ln.startswith("half,")) == 2
    assert sum(1 for ln in lines if ln.startswith("year,")) == 1
    rows = list(csv.DictReader(io.StringIO(periods)))
    groups = {kind: {r["period"]: r for r in rows if r["granularity"] == kind}
              for kind in ("day", "month", "quarter", "half", "year")}
    month_for = lambda r: r["period"][:7]
    quarter_for = lambda r: f"2026-Q{(int(r['period'][5:7]) - 1) // 3 + 1}"
    half_for = lambda r: f"2026-H{1 if r['period'][-1] in '12' else 2}"
    for children, parents, key_of in (("day", "month", month_for), ("month", "quarter", quarter_for),
                                      ("quarter", "half", half_for), ("half", "year", lambda r: "2026")):
        for key, parent in groups[parents].items():
            picked = [r for r in groups[children].values() if key_of(r) == key]
            assert picked
            for col in truth.PERIOD_TOTALS:
                assert sum(Decimal(r[col]) for r in picked) == Decimal(parent[col]), (children, key, col)


def test_no_unmasked_personal_identifiers(tmp_path: Path, dataset):
    write_all(dataset, tmp_path)
    alipay = (tmp_path / "支付宝" / "支付宝交易明细(20260101-20261231).csv").read_bytes().decode("gb18030")
    assert not re.search(r"\b\d{17}[\dXx]\b", alipay)          # 身份证
    assert not re.search(r"\b1[3-9]\d{9}\b", alipay)           # 完整手机号
    assert "138****0000" in alipay                             # 只有掩码
    # 卡号只允许以掩码形式出现在付款方式里；订单号列里的 16 位数字不是卡号。
    lines = alipay.splitlines()
    header = next(i for i, ln in enumerate(lines) if ln.startswith("交易时间,"))
    for row in csv.DictReader(io.StringIO("\n".join(lines[header:]))):
        assert not re.search(r"\b(?:6225|4392)\d{12,15}\b", row["收/付款方式"])
