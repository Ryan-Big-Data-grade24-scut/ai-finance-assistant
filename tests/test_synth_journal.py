"""Hand-calculated v3 business cases. CI needs no private files."""
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal as D
import os
from pathlib import Path

import pytest

from scripts.synth import truth, write_all
from scripts.synth.engine import Ledger
from scripts.synth.verification import evidence, replay


def at(day):
    return datetime.fromisoformat(f"2026-{day} 12:00:00")


@pytest.fixture
def ledger():
    return Ledger(20261126, date(2026, 1, 1), date(2026, 12, 31))


@pytest.fixture
def check(request):
    rows = []

    def compare(label, expected, actual):
        rows.append([label, expected, actual, "PASS" if actual == expected else "FAIL"])
        assert actual == expected, label

    yield compare
    if root := os.environ.get("SYNTH_SCENARIO_EVIDENCE"):
        truth._write_csv(Path(root) / request.node.name / "checks.csv",
                         ["check", "expected", "actual", "status"], rows)


def test_original_product_and_partial_refunds(ledger, check):
    first = ledger._wallet_pay("alipay", "余额宝", at("01-30"), D("120"), "教育培训", "校车服务", "校车")
    second = ledger._wallet_pay("alipay", "账户余额", at("02-01"), D("120"), "教育培训", "校车服务", "校车")
    events = ledger.journal.by_id
    sale = events[first.event_id]
    seller = sale.payee
    check("payment path", ("alipay:余额宝", "alipay_clearing", seller), sale.route)
    check("seller receipts before refund", D("5240"), ledger.bal[seller])
    check("investment balance after first purchase", D("25880"), ledger.bal["alipay:余额宝"])
    check("ordinary wallet after second purchase", D("2680"), ledger.bal["alipay:余额"])

    partial = ledger.refund(at("02-02"), first.event_id, D("35"))
    check("refund path", (seller, "alipay_clearing", "alipay:余额宝"), partial.route)
    check("partial credit to correct product", D("25915"), ledger.bal["alipay:余额宝"])
    ledger.refund(at("02-03"), first.event_id, D("85"))
    rejection = ledger.refund(at("02-04"), first.event_id, D("0.01"))
    check("over-limit refund rejected atomically", ("failed", (), ()),
          (rejection.status, rejection.legs, rejection.changes))
    check("original product fully restored", D("26000"), ledger.bal["alipay:余额宝"])
    check("same-priced payment in other product untouched", (D("2680"), D("0")),
          (ledger.bal["alipay:余额"], second.refunded))
    check("seller net receipts and zero clearing", (D("5120"), D("0")),
          (ledger.bal[seller], ledger.bal["alipay_clearing"]))

    ds = ledger.dataset()
    truth.annotate(ds)
    check("both refunds independently replayed", 2, len(replay(ds)["refund_routes.csv"][1]))
    periods = {r["period"]: r for r in truth.period_summary(ds)}
    check("January gross/refund/net", (D("120"), D("0"), D("120")),
          tuple(periods["2026-01"][k] for k in ("outflow", "refunds", "net_outflow")))
    check("February gross/refund/net", (D("120"), D("120"), D("0")),
          tuple(periods["2026-02"][k] for k in ("outflow", "refunds", "net_outflow")))
    cls = truth.SPEND_COLUMNS[sale.business.expense_class]
    check("refund inherits original spending class in summary", D("120"), periods["2026-02"][f"{cls}_refund"])


def test_direct_sale_marketplace_collection_and_wallet_mirror(ledger, check):
    direct = ledger.spend("salary", at("01-02"), D("20"), "餐饮美食", "虾饺店", "点心")
    collected = ledger._wallet_pay("alipay", "支付宝小荷包(家庭开销)", at("01-03"), D("30"),
                                   "餐饮美食", "美团", "点心", merchant="虾饺店", order_channel="meituan")
    events = ledger.journal.by_id
    a, b = events[direct.event_id], events[collected.event_id]
    check("direct merchant funding", ("salary", a.payee), a.route)
    check("seller is same business in both sales", a.payee, b.payee)
    check("marketplace route length and endpoints", (4, "wallet_family", "alipay_clearing", a.payee),
          (len(b.route), b.route[0], b.route[1], b.route[-1]))
    check("seller receives both payments", D("5050"), ledger.bal[a.payee])
    check("marketplace clears to merchant", D("0"), ledger.bal[b.route[2]])
    check("both payment views backed by one debit", D("2970"), ledger.bal["wallet_family"])
    ds = ledger.dataset()
    truth.annotate(ds)
    main, mirror = [p for p in ds.postings if p.event_id == b.id]
    check("wallet mirror retains label and event", (main.truth_id, main.expense_class, main.event_id),
          (mirror.mirror_of, mirror.expense_class, mirror.event_id))
    check("only one counted purchase per sale", D("50"), truth.period_summary(ds)[-1]["outflow"])
    replay(ds)


def test_credit_cutoff_refund_after_payoff_and_reuse(ledger, check):
    sale = ledger._wallet_pay("alipay", "招商银行信用卡(6620)", at("01-04"), D("100"),
                              "餐饮美食", "虾饺店", "点心")
    ledger._statement(at("01-05"), "credit")
    ledger.spend("credit", at("01-06"), D("30"), "餐饮美食", "虾饺店", "面条")
    check("statement excludes later purchase", (D("100"), D("130")),
          (ledger.journal.values["bill:credit"], ledger.journal.values["credit:credit"]))
    ledger._repay(at("01-23"), "credit")
    check("repayment only pays billed amount", (D("44900"), D("30"), D("0")),
          (ledger.bal["salary"], ledger.journal.values["credit:credit"], ledger.journal.values["bill:credit"]))
    ledger.refund(at("01-24"), sale.event_id, D("100"))
    check("refund after paying statement creates backed credit", (D("-70"), D("70")),
          (ledger.journal.values["credit:credit"], ledger.bal["prepaid:credit"]))
    ledger.spend("credit", at("01-25"), D("90"), "餐饮美食", "虾饺店", "聚餐")
    check("new purchase consumes credit before new debt", (D("20"), D("0")),
          (ledger.journal.values["credit:credit"], ledger.bal["prepaid:credit"]))
    check("issuer cash tracks net amount advanced", D("999980"), ledger.bal["cmb"])
    ds = ledger.dataset()
    truth.annotate(ds)
    check("card mirror points to wallet payment", sale.truth_id, ds.postings[1].mirror_of)
    replay(ds)


def test_payroll_unpaid_contract_tax_and_cancelled_order(ledger, check):
    salary = ledger.receipt("salary", at("01-10"), D("1"), "收入", "启明科技有限公司", "工资", action="salary")
    check("employment net salary, not arbitrary input", D("28000"), salary.amount)
    check("employer pays household from finite opening cash", (D("2472000"), D("73000")),
          (ledger.bal["employer"], ledger.bal["salary"]))
    paid = ledger.spend("mortgage_card", at("01-15"), D("7800"), "信用借还", "中国工商银行", "个人住房贷款还款")
    e = ledger.journal.by_id[paid.event_id]
    check("first installment principal and interest", [("loan_principal", D("5000")), ("loan_interest", D("2800"))],
          [(leg.purpose, leg.amount) for leg in e.legs])
    ledger.spend("mortgage_card", at("02-15"), D("7800"), "信用借还", "中国工商银行", "个人住房贷款还款")
    failed = ledger.journal.events[-1]
    check("unfunded installment leaves amount due", ("unpaid", D("7800")),
          (failed.status, ledger.journal.values["due:loan:mortgage:2026-02"]))
    check("failed debit leaves cash and loan principal intact", (D("200"), D("1795000")),
          (ledger.bal["mortgage_card"], ledger.journal.values["loan:mortgage"]))
    ledger.receipt("salary", at("04-15"), D("1800"), "收入", "国家税务总局", "个税汇算退税", action="tax_refund")
    check("tax return refunds only historical withholding", (D("1200"), D("74800"), D("998200")),
          (ledger.journal.values["tax:2025:refundable_withholding"], ledger.bal["salary"], ledger.bal["tax"]))
    ledger._closed(at("04-16"), "alipay", D("99"), "服饰装扮", "淘宝", "取消的衣服", "taobao")
    check("cancelled order has no cash legs", (), ledger.journal.events[-1].legs)
    ds = ledger.dataset()
    truth.annotate(ds)
    yearly = truth.period_summary(ds)[-1]
    check("tax not counted as income or cancelled order as spend", (D("28000"), D("1800"), D("7800")),
          (yearly["income"], yearly["tax_refund"], yearly["outflow"]))
    replay(ds)


def test_employer_cannot_pay_when_cash_is_insufficient(ledger, check):
    journal = ledger.journal
    journal.accounts["employer"] = replace(journal.accounts["employer"], opening=D("27000"))
    journal.cash["employer"] = D("27000")
    ledger.receipt("salary", at("01-10"), D("28000"), "收入", "启明科技有限公司", "工资", action="salary")
    check("no successful payment view", [], ledger.postings)
    check("unpaid payroll stays due", ("unpaid", D("28000")),
          (journal.events[-1].status, journal.values["due:employment:2026-01:salary"]))
    check("neither balance was changed", (D("27000"), D("45000")),
          (journal.cash["employer"], journal.cash["salary"]))
    replay(ledger.dataset())


def test_redeem_500_units_at_three_yuan(ledger, tmp_path, check):
    code = next(iter(ledger.invest.funds))
    base = f"fund:{code}"
    journal = ledger.journal
    for k, amount in (("mark", D("3.00")), ("qty", D("800.00")), ("cost", D("2000.00"))):
        key = f"{base}:{k}"
        journal.states[key] = replace(journal.states[key], opening=amount)
        journal.values[key] = amount
    e = ledger.invest.redeem(at("03-25"), ledger, code=code, units=D("500"))
    row, = ledger.invest.fund_rows
    check("confirmation gross / units / fee", (D("1500"), D("500"), D("7.50")),
          (e.amount, row["shares"], row["fee"]))
    check("bank net / issuer gross / fee collector", (D("46492.50"), D("248500"), D("7.50")),
          (journal.cash["salary"], journal.cash[base], journal.cash["fund_app"]))
    check("remaining units, cost and realized P&L", (D("300"), D("750"), D("242.50")),
          tuple(journal.values[f"{base}:{k}"] for k in ("qty", "cost", "realized")))
    ds = ledger.dataset()
    truth.annotate(ds)
    replay(ds)

    write_all(ds, tmp_path)
    confirmation = tmp_path / "投资" / "天天基金_交易确认.csv"
    raw = confirmation.read_text(encoding="utf-8-sig")
    assert ",1500.00," in raw
    confirmation.write_text(raw.replace(",1500.00,", ",1500.001,"), encoding="utf-8-sig")
    result = truth.verify_ground_truth(ds, tmp_path)
    check("sub-cent error rejected with unchanged confirmation ID", (False, 1),
          (result["ok"], result["export_rows"] - result["matched"]))


def test_broker_partial_sale_separates_realized_and_floating(ledger, check):
    code = next(c for c in ledger.invest.stocks if ledger.journal.values[f"stock:{c}:qty"] > 0)
    base = f"stock:{code}"
    journal = ledger.journal
    for key, amount in ((f"{base}:mark", D("20")), (f"{base}:qty", D("300")),
                        (f"{base}:cost", D("4500"))):
        journal.states[key] = replace(journal.states[key], opening=amount)
        journal.values[key] = amount
    bank_before = journal.cash["broker_cash"]
    ledger.invest.trade(at("01-24"), code=code, side="卖出", qty=D("100"))
    row, = ledger.invest.broker_rows
    check("gross / fee / realized", (D("2000"), D("6"), "494.00"), (row["amount"], row["fee"], row["pnl"]))
    check("broker cash increment / remaining positions", (D("1994"), D("200"), D("3000")),
          (journal.cash["broker_cash"] - bank_before, journal.values[f"{base}:qty"], journal.values[f"{base}:cost"]))
    ledger.invest.snapshot(at("01-31"))
    snap = next(r for r in ledger.invest.snapshots if r["account"] == "证券")
    check("account floating versus total", (D("1000"), D("494"), D("1494")),
          (snap["pnl"], snap["realized"], snap["total_pnl"]))
    replay(ledger.dataset())


def test_same_channel_same_price_refund_is_not_scoreable(ledger, check):
    first = ledger._wallet_pay("wechat", "零钱通", at("01-31"), D("4.58"), "交通出行", "手机地铁乘车码", "地铁")
    ledger._wallet_pay("wechat", "零钱通", at("02-01"), D("4.58"), "交通出行", "手机地铁乘车码", "地铁")
    refund = ledger.refund(at("02-02"), first.event_id, D("4.58"))
    ds = ledger.dataset()
    truth.annotate(ds)
    hint, = [r for r in evidence(ds) if r[0] == refund.id]
    check("two visible candidates: skip blind scoring", ("ambiguous", 2, "no"), (hint[2], hint[4], hint[5]))
    check("engine keeps actual original", first.event_id, refund.original)


def test_replay_rejects_money_route_and_label_corruption(ledger, monkeypatch, check):
    p = ledger._wallet_pay("alipay", "余额宝", at("01-03"), D("100"), "餐饮美食", "虾饺店", "点心")
    refund = ledger.refund(at("01-04"), p.event_id, D("25"))
    ds = ledger.dataset()
    truth.annotate(ds)
    replay(ds)
    j = ds.journal
    sale = j.events[0]
    for idx, bad, message in (
        (0, replace(sale, legs=(replace(sale.legs[0], amount=D("101")), sale.legs[1])), "stranded settlement"),
        (1, replace(refund, route=(*refund.route[:-1], "alipay:余额"),
                    legs=(*refund.legs[:-1], replace(refund.legs[-1], destination="alipay:余额"))), "cash effect"),
    ):
        with monkeypatch.context() as patch:
            changed = j.events.copy()
            changed[idx] = bad
            patch.setattr(j, "events", changed)
            with pytest.raises(AssertionError, match=message) as exc:
                replay(ds)
            check(f"reject {message}", True, bad.id in str(exc.value))
    monkeypatch.setattr(p, "expense_class", "义务支出")
    with pytest.raises(AssertionError, match="label differs") as exc:
        replay(ds)
    check("reject wrong business label", True, p.event_id in str(exc.value))


def test_verify_rejects_changed_amount_under_same_source_id(ledger, tmp_path, check):
    ledger._wallet_pay("alipay", "余额宝", at("01-03"), D("123.45"), "餐饮美食", "虾饺店", "点心")
    ds = ledger.dataset()
    write_all(ds, tmp_path)
    path, = (tmp_path / "支付宝").glob("*.csv")
    raw = path.read_bytes().decode("gb18030")
    header, data = raw.rsplit("\n", maxsplit=2)[:2]
    assert ",123.45," in data
    path.write_bytes(f"{header}\n{data.replace(',123.45,', ',123.46,')}\n".encode("gb18030"))
    result = truth.verify_ground_truth(ds, tmp_path)
    check("one altered source row detected at same ID", (False, 1), (result["ok"], result["export_rows"] - result["matched"]))


def test_existing_output_is_not_overwritten(ledger, tmp_path, check):
    marker = tmp_path / "summary.json"
    marker.write_text("existing dataset", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_all(ledger.dataset(), tmp_path)
    check("existing output remains unchanged", "existing dataset", marker.read_text(encoding="utf-8"))
