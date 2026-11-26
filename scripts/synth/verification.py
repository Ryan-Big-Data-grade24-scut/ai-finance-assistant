"""Independent replay and explicit raw-evidence limits for the synthetic dataset."""
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal

from . import actors as A, data as D
from .journal import ZERO, money


def _require(predicate, event, message):
    if not predicate:
        raise AssertionError(f"{event.id} {event.business.item}: {message}")


def replay(dataset):
    j = dataset.journal
    cash = {key: a.opening for key, a in j.accounts.items()}
    state = {key: a.opening for key, a in j.states.items()}
    inflow, outflow, refunds = Counter(), Counter(), Counter()
    views, funds, stocks, snapshots, yields = (defaultdict(list) for _ in range(5))
    for p in dataset.postings:
        views[p.event_id].append(p)
    for rows, index in ((dataset.fund_rows, funds), (dataset.broker_rows, stocks)):
        for row in rows:
            index[row["event_id"]].append(row)
    for row in dataset.snapshots:
        snapshots[row["event_id"]].append(row)
    for row in dataset.wechat_yield_rows + dataset.wealth_rows:
        yields[row["event_id"]].append(row)
    returns = []
    evt_by_id = {}
    previous = None
    for e in j.events:
        kind = e.business.kind
        _require(previous is None or e.at >= previous, e, "event time regressed")
        previous = e.at
        _require(e.id not in evt_by_id, e, "duplicate business event id")
        evt_by_id[e.id] = e
        projections = views.pop(e.id, [])
        _require(e.payer in j.actors and e.payee in j.actors and e.initiator in j.actors, e, "unknown actor")
        if e.route:
            supplier_cash = A.CREDIT_ISSUER[e.credit] if e.credit else None
            expected_from = supplier_cash if e.credit and kind in {"purchase", "contract", "loan"} else None
            expected_to = supplier_cash if e.credit and kind == "refund" else None
            _require((e.route[0] == expected_from if expected_from else j.accounts[e.route[0]].owner == e.payer)
                     and (e.route[-1] == expected_to if expected_to else j.accounts[e.route[-1]].owner == e.payee),
                     e, "cash endpoints disagree with economic actors")
        if e.status != "settled":
            _require(not (e.legs or e.changes), e, "non-settled event has financial legs")
            _require(not projections or (e.status == "closed" and all(
                p.status == "交易关闭" and p.effect == "none" for p in projections)), e,
                "failed event has successful source projection")
            continue
        before = {k: state[k] for k in {c.key for c in e.changes}}
        cash_delta, changes = Counter(), Counter()
        _require(all(j.accounts[k].owner in j.actors for k in e.route), e, "unregistered route owner")
        if kind in {"purchase", "gift", "contract", "payroll", "contribution", "tax_refund", "refund", "loan"}:
            _require(sum(not p.secondary for p in projections) == 1 and len(projections) <= 2, e, "payment projection count")
        for leg in e.legs:
            _require(leg.amount > 0 and money(leg.amount) == leg.amount, e, "invalid cash amount")
            _require(leg.source in cash and leg.destination in cash and leg.source != leg.destination,
                     e, "unknown or identical cash endpoints")
            cash[leg.source] -= leg.amount
            cash[leg.destination] += leg.amount
            outflow[leg.source] += leg.amount
            inflow[leg.destination] += leg.amount
            cash_delta[leg.source] -= leg.amount
            cash_delta[leg.destination] += leg.amount
            _require(cash[leg.source] >= 0, e, f"overdraft at {leg.source}")
        for change in e.changes:
            changes[change.key] += change.delta
            state[change.key] += change.delta
        for key in changes:
            spec = j.states[key]
            _require((spec.floor is None or state[key] >= spec.floor) and
                     (spec.ceiling is None or state[key] <= spec.ceiling), e, f"out-of-range state at {key}")
        for credit in (e.credit,) if e.credit else ():
            debt = state[f"credit:{credit}"]
            _require(cash[f"prepaid:{credit}"] == max(ZERO, -debt) and state[f"bill:{credit}"] <= max(ZERO, debt),
                     e, "card credit or statement bridge")
        for key in cash_delta:
            if j.accounts[key].role in {"clearing", "marketplace"}:
                _require(cash[key] == j.accounts[key].opening, e, f"stranded settlement in {key}")
        for p in projections:
            _require((p.expense_class, p.cadence, p.category) ==
                     (e.business.expense_class, e.business.cadence, e.business.category), e, "source label differs from business")
            _require(p.when.date() == e.at.date(), e, "projection on wrong day")
            if p.secondary:
                _require(any(not main.secondary and p.mirror_of == main.truth_id and p.amount == main.amount
                             for main in projections), e, "mirror is not linked to its primary")
            if e.business.item != "基金赎回款":
                _require(p.amount == e.amount, e, "projection amount differs from event")
            if p.amount and p.effect != "none" and p.account not in D.CREDIT_ACCOUNTS:
                key = f"{p.account}:{p.funding}" if p.account in D.PAYABLE_FUNDING else p.account
                signed = p.amount if p.effect == "in" else -p.amount
                _require(cash_delta[key] == signed, e, f"source has no matching cash effect at {key}")
        if kind in {"purchase", "gift", "contract", "payroll", "contribution", "tax_refund", "yield", "refund"}:
            expected = list(zip(e.route, e.route[1:]))
            actual = [(leg.source, leg.destination) for leg in e.legs
                      if leg.purpose not in {"apply_card_credit", "card_credit"}]
            _require(actual == expected and all(leg.amount == e.amount for leg in e.legs
                                                if leg.purpose not in {"apply_card_credit", "card_credit"}),
                     e, "cash legs disagree with business route/amount")
        if kind in {"contract", "loan", "payroll"}:
            key = f"due:{e.business.reference}"
            _require(before.get(key) == e.amount and changes[key] == -e.amount and state[key] == ZERO,
                     e, "missing or incorrect payable settlement")
        if kind == "due":
            key = f"due:{e.business.reference}"
            _require(not e.legs and changes == {key: e.amount} and before[key] == ZERO, e, "invalid new payable")
        if kind == "settlement":
            _require(len(e.route) == 2 and [(leg.source, leg.destination, leg.amount) for leg in e.legs]
                     == [(e.route[0], e.route[1], e.amount)], e, "transfer cash route")
            if e.credit:
                key, bill = f"credit:{e.credit}", f"bill:{e.credit}"
                _require(changes[key] == -e.amount and changes[bill] == -min(e.amount, before[bill]),
                         e, "payment did not reduce credit and statement correctly")
        if kind == "refund":
            original = evt_by_id.get(e.original)
            _require(original is not None and original.status == "settled" and original.business.kind == "purchase",
                     e, "refund without a prior successful purchase")
            _require(e.route == original.route[::-1] and e.credit == original.credit, e, "refund route differs from original")
            _require((e.payer, e.payee) == (original.payee, original.payer), e, "wrong refund actors")
            refunds[e.original] += e.amount
            _require(refunds[e.original] <= original.amount, e, "excess refund")
            returns.append([e.id, original.id, original.at.date(), e.at.date(), original.business.expense_class,
                            original.amount, e.amount, e.route[-1], "PASS"])
        elif kind == "loan":
            loan = A.LOANS[e.business.item]
            key = f"loan:{loan.key}"
            principal = -changes[key]
            charge = money(before[key] * loan.rate)
            _require(principal + charge == e.amount and principal > 0, e, "loan principal/interest split")
            if not e.credit:
                _require(sum((leg.amount for leg in e.legs if leg.purpose == "loan_principal"), ZERO) == principal
                         and sum((leg.amount for leg in e.legs if leg.purpose == "loan_interest"), ZERO) == charge,
                         e, "missing loan cash split")
        elif kind == "statement":
            _require(state[f"bill:{e.credit}"] == max(ZERO, state[f"credit:{e.credit}"]), e,
                     "statement differs from debt at cutoff")
        if kind == "yield":
            dest = e.route[-1]
            _require(e.route[0] == A.YIELD_PROVIDER[dest], e, "wrong yield provider")
            rate = (Decimal("0.0026") if dest == "wealth" else
                    D.WALLET_PRODUCTS[dest].annual_yield / (365 if e.business.cadence == "每日" else 12))
            _require(e.amount == money((cash[dest] - e.amount) * rate), e, "yield amount")
            detail = yields.pop(e.id, [])
            if dest == "wealth" or dest.startswith("wechat:"):
                _require(not projections and len(detail) == 1, e, "yield detail coverage")
                row = detail[0]
                _require((row["date"], row["amount"], row["balance"]) == (e.at.date().isoformat(), e.amount, cash[dest])
                         and (row.get("note") == e.business.item if dest == "wealth" else row.get("product") == dest.split(":")[1]),
                         e, "incorrect yield source row")
            else:
                _require(len(projections) == 1 and not detail, e, "Alipay yield projection count")
        if e.id in funds:
            gross, fees = ZERO, ZERO
            for row in funds.pop(e.id):
                base = f"fund:{row['code']}"
                units, nav, fee, amount = (row[k] for k in ("shares", "nav", "fee", "amount"))
                sale = row["business"] == "赎回确认"
                signed = -units if sale else units
                gross += amount
                fees += fee
                _require(units > 0 and units == money(units) and row["date"] == e.at.date().isoformat()
                         and row["rounding"] == (amount if sale else amount - fee) - units * nav,
                         e, "fund confirmation precision or residual")
                _require(nav == state[f"{base}:mark"] and changes[f"{base}:qty"] == signed, e,
                         "fund quantity differs from confirmation")
                _require(fee == money(amount * Decimal("0.005" if sale else "0.0015")), e, "fund fee")
                if sale:
                    released = money(before[f"{base}:cost"] * units / before[f"{base}:qty"])
                    _require(amount == money(units * nav) and cash_delta[base] == -amount
                             and cash_delta["salary"] == amount - fee and len(projections) == 1
                             and projections[0].amount == amount - fee
                             and changes[f"{base}:cost"] == -released
                             and changes[f"{base}:realized"] == amount - fee - released,
                             e, "fund redemption: confirm gross units, bank receives net")
                else:
                    _require(units == money((amount - fee) / nav) and cash_delta[base] == amount - fee
                             and changes[f"{base}:cost"] == amount, e, "fund subscription split")
            _require(gross == e.amount and cash_delta["fund_app"] == fees, e, "fund order gross or fee recipient")
            if e.business.item == "基金定投扣款":
                _require(cash_delta["salary"] == -gross and len(projections) == 1, e, "fund subscription bank debit")
        elif e.business.item in {"基金定投扣款", "基金赎回款"}:
            _require(False, e, "missing fund confirmation")
        if e.id in stocks:
            row, = stocks.pop(e.id)
            base = f"stock:{row['code']}"
            qty, fee = row["qty"], row["fee"]
            gross = qty * row["price"]
            sale = row["side"] == "卖出"
            signed = -qty if sale else qty
            cost_change = -money(before[f"{base}:cost"] * qty / before[f"{base}:qty"]) if sale else gross + fee
            commission = money(max(Decimal(5), gross * Decimal("0.00025")))
            stamp = money(gross * Decimal("0.0005")) if sale else ZERO
            realized = gross - fee + cost_change if sale else ZERO
            _require(qty > 0 and qty % 100 == 0 and row["date"] == e.at.date().isoformat()
                     and fee == commission + stamp and cash_delta["broker"] == commission
                     and cash_delta["tax"] == stamp and cash_delta["market"] == (-gross if sale else gross)
                     and changes[f"{base}:realized"] == realized, e, "broker fees or realized cash bridge")
            _require(gross == row["amount"] == e.amount and row["price"] == state[f"{base}:mark"]
                     and changes[f"{base}:qty"] == signed and changes[f"market:{row['code']}:qty"] == -signed
                     and changes[f"{base}:cost"] == cost_change
                     and cash_delta["broker_cash"] == (gross - fee if sale else -gross - fee)
                     and (not sale or Decimal(row["pnl"]) == gross - fee + cost_change), e,
                     "broker cash, quantity or realized P&L mismatch")
        elif kind == "asset_move" and e.business.reference.startswith("stock:"):
            _require(False, e, "missing stock confirmation")
        if kind == "snapshot":
            _verify_snapshot(e, snapshots.pop(e.id, []), state, cash)
    if cash != j.cash or state != j.values or views or funds or stocks or snapshots or yields:
        raise AssertionError("replay did not reproduce ending balances/state or found orphaned projections")
    for key, balance in dataset.balances.items():
        if key in D.PAYABLE_FUNDING:
            expected = sum(cash[k] for k, item in D.WALLET_PRODUCTS.items() if item.account == key)
        elif key in D.CREDIT_ACCOUNTS:
            expected = ZERO  # Debt is reported separately.
        elif key == "fund":
            expected = sum(money(state[f"fund:{c}:qty"] * state[f"fund:{c}:mark"]) for c, _ in D.FUNDS)
        elif key == "broker":
            expected = cash["broker_cash"] + sum(money(state[f"stock:{c}:qty"] * state[f"stock:{c}:mark"])
                                                  for c, _, _ in D.BROKER_STOCKS)
        else:
            expected = cash[key]
        if balance != expected:
            raise AssertionError(f"incorrect reported closing balance: {key}")
    if (dataset.credit_used_by != {k: max(ZERO, state[f"credit:{k}"]) for k in D.CREDIT_ACCOUNTS}
            or dataset.credit_used != sum(dataset.credit_used_by.values())
            or dataset.huabei_used != max(ZERO, state["credit:huabei"])):
        raise AssertionError("incorrect reported credit usage")
    for key, balance in dataset.wallet_products.items():
        if balance != cash[key]:
            raise AssertionError(f"incorrect wallet closing balance {key}")
    for p in dataset.postings:
        if p.kind == "purchase" and not p.secondary and p.refunded != refunds[p.event_id]:
            raise AssertionError(f"{p.event_id} refund total differs from source order")
    return {
        "cash_bridge.csv": (["account", "actor", "opening", "inflow", "outflow", "replayed", "actual", "status"],
                            [[k, a.owner, a.opening, inflow[k], outflow[k], cash[k], j.cash[k], "PASS"]
                             for k, a in sorted(j.accounts.items())]),
        "state_bridge.csv": (["state", "unit", "opening", "change", "replayed", "actual", "status"],
                             [[k, spec.unit, spec.opening, state[k] - spec.opening, state[k], j.values[k], "PASS"]
                              for k, spec in sorted(j.states.items())]),
        "refund_routes.csv": (["refund", "original", "purchase_date", "settlement_date", "expense_class",
                               "original_amount", "refund_amount", "funding_returned_to", "status"], returns),
    }


def _verify_snapshot(e, rows, state, cash):
    def get(group, code):
        base = f"{group}:{code}"
        return (money(state[f"{base}:qty"] * state[f"{base}:mark"]), state[f"{base}:cost"], state[f"{base}:realized"])

    expected = {name: (*get("fund", code), ZERO) for code, name in D.FUNDS}
    expected["华泰证券账户"] = (*map(sum, zip(*(get("stock", code) for code, _, _ in D.BROKER_STOCKS))), cash["broker_cash"])
    principal = D.ACCOUNTS["wealth"].opening
    expected["招行理财"] = (principal, principal, cash["wealth"] - principal, cash["wealth"] - principal)
    _require(len(rows) == len(expected) and {r["name"] for r in rows} == set(expected), e, "snapshot coverage")
    for r in rows:
        value, cost, realized, available = expected[r["name"]]
        _require(r["date"] == e.at.date().isoformat() and
                 r["pnl_pct"] == (money((value - cost) / cost * 100) if cost else ZERO), e, "snapshot date or return rate")
        _require((r["market_value"], r["cost"], r["realized"], r["cash"], r["pnl"], r["total_pnl"]) ==
                 (value, cost, realized, available, value - cost, value - cost + realized), e, "snapshot P&L mismatch")


def evidence(dataset):
    """Use only visible source fields for candidate counts. Never disclose event IDs in raw files."""
    from .engine import WALLET_NAME
    from .truth import _raw_posting_fields

    by_event, by_account = defaultdict(list), defaultdict(list)
    fields = {}
    for p in dataset.postings:
        by_event[p.event_id].append(p)
        by_account[p.account].append(p)
        fields[p.tx_no] = _raw_posting_fields(p)

    def transfer_matches(src, dst):
        # Callers search the entire visible account index, not the true destination.
        left, right = fields[src.tx_no], fields[dst.tx_no]
        out_label = left.get("对方户名") or left.get("交易对方")
        in_label = right.get("对方户名") or right.get("交易对方")
        if in_label != D.ACCOUNTS[src.account].name:
            return False
        target = D.ACCOUNTS[dst.account]
        if out_label not in {target.name, target.name.removesuffix("储蓄卡"),
                             f"{target.name}({target.mask[-4:]})"}:
            return False
        if "交易日期" in right:
            return right["交易类型"] == "转账" and bool(right["收入金额"])
        return right.get("商品说明", right.get("商品")) in {"充值", "零钱充值"}

    result = []
    for e in dataset.journal.events:
        kind = e.business.kind
        rows = by_event[e.id]
        if e.status != "settled" or not rows:
            continue
        if e.business.kind == "contribution":
            result.append([e.id, "contributor", "missing", "小荷包操作人不是付款方", "", "no"])
        if e.business.kind == "refund":
            main = next(p for p in rows if not p.secondary)
            name = main.item.removeprefix("退款-")
            original = dataset.journal.by_id[e.original]
            candidates = []
            for p in by_account[main.account]:
                f, refund = fields[p.tx_no], fields[main.tx_no]
                if p.amount < main.amount:
                    continue
                if main.account in {"alipay", "wechat"}:
                    item = "商品说明" if main.account == "alipay" else "商品"
                    method = "收/付款方式" if main.account == "alipay" else "支付方式"
                    matches = (p.when < main.when and f["收/支"] == "支出"
                               and f[item] == name and f["交易对方"] == refund["交易对方"] and f[method] == refund[method])
                elif main.account in D.CREDIT_ACCOUNTS:
                    matches = (p.when.date() <= main.when.date() and f["交易类型"] == "消费"
                               and f["交易描述"] == name and f["商户名称"] == refund["商户名称"])
                else:
                    matches = False
                if matches:
                    candidates.append(p)
            unique = len(candidates) == 1 and candidates[0].event_id == original.id
            result.append([e.id, "original_purchase", "unique_combination" if unique else "ambiguous" if candidates else "missing",
                           "无原交易号；仅用可见商户、描述、金额及付款渠道找候选", len(candidates), "yes" if unique else "no"])
        if e.business.kind == "settlement" and len(rows) == 2:
            outgoing, incoming = sorted(rows, key=lambda p: p.when)
            visible = not ({incoming.account, outgoing.account} & {*D.CREDIT_ACCOUNTS, *WALLET_NAME})
            candidates = [p for p in dataset.postings if visible and p.account != outgoing.account
                          and p.amount == outgoing.amount and 0 <= (p.when - outgoing.when).total_seconds() <= 60
                          and transfer_matches(outgoing, p)]
            unique = len(candidates) == 1 and incoming in candidates
            result.append([e.id, "transfer_pair", "unique_combination" if unique else "ambiguous" if visible else "missing",
                           "两边单号独立；仅用可见账户、金额与时间" if visible else "至少一侧缺对方账户或时分秒",
                           len(candidates) if visible else "", "yes" if unique else "no"])
        for p in rows:
            if p.order_channel and p.account in {"alipay", "wechat"}:
                result.append([e.id, "marketplace_order", "direct", "付款商户单号与订单号一致", 1, "yes"])
            elif p.order_channel:
                result.append([e.id, "marketplace_order", "missing", "卡账没有电商订单号", "", "no"])
            if p.secondary:
                main = next(q for q in rows if not q.secondary)
                shown = fields[main.tx_no]
                method = shown.get("收/付款方式", shown.get("支付方式", ""))
                if p.account in D.CREDIT_ACCOUNTS:
                    target = next((key for key, label in D.CREDIT_LABEL.items() if label == method), None)
                    merchant = shown["交易对方"]
                    item = shown.get("商品说明", shown.get("商品"))
                    desc = "退款" if kind == "refund" else "消费"
                    candidates = [q for q in by_account[target] if q.amount == main.amount
                                  and q.when.date() == main.when.date()
                                  and fields[q.tx_no]["交易类型"] == desc
                                  and fields[q.tx_no]["交易描述"] == item and fields[q.tx_no]["商户名称"] == merchant]
                else:
                    target = next((k for k, name in WALLET_NAME.items() if method == f"支付宝小荷包({name})"), None)
                    desc = "退款" if kind == "refund" else "消费"
                    column = "收入金额" if kind == "refund" else "支出金额"
                    candidates = [q for q in by_account[target] if q.amount == main.amount and q.when == main.when
                                  and fields[q.tx_no]["交易说明"] == desc and fields[q.tx_no][column]]
                unique = len(candidates) == 1 and p in candidates
                result.append([e.id, "payment_mirror", "unique_combination" if unique else "ambiguous",
                               "无共享号；按来源可见精度核对时间、金额及有值的商户/商品字段",
                               len(candidates), "yes" if unique else "no"])
    return result
