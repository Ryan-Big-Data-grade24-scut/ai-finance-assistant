"""Fund and brokerage confirmations backed by cash, quantity and cost changes."""
from datetime import datetime, time, timedelta
from decimal import Decimal, ROUND_DOWN

from . import actors as A, data as D
from .journal import Business, Change, Leg, ZERO, money


class Investments:
    def __init__(self, rng, journal):
        self.rng = rng
        self.journal = journal
        self.funds = dict(D.FUNDS)
        self.stocks = {code: name for code, name, _ in D.BROKER_STOCKS}
        self.fund_rows, self.broker_rows, self.wealth_rows, self.snapshots = [], [], [], []
        self.bias = {}
        for code in self.funds:
            nav = money(rng.uniform(1.2, 3.6))
            units = money(Decimal("17200") / nav)
            self._position("fund", code, nav, units)
            self.bias[code] = rng.uniform(-0.004, 0.006)
            provider = f"fund:{code}"
            journal.actor(provider, self.funds[code], "fund_issuer")
            journal.account(provider, provider, "fund_cash", money("250000"))
        prices = {c: money(base * Decimal(str(rng.uniform(0.97, 1.03))))
                  for c, _, base in D.BROKER_STOCKS}
        opening_codes = set(sorted(prices, key=prices.get)[:3])
        cash = D.ACCOUNTS["broker"].opening
        for code, price in prices.items():
            qty = Decimal(int(Decimal("14000") / price // 100) * 100) if code in opening_codes else ZERO
            self._position("stock", code, price, qty)
            cash -= money(price * qty)
            journal.state(f"market:{code}:qty", Decimal(100000), unit="shares")
        journal.account("broker_cash", A.OWNER, "broker_cash", cash)

    def _position(self, group, code, mark, qty):
        base = f"{group}:{code}"
        self.journal.state(f"{base}:mark", mark, unit="CNY/unit")
        self.journal.state(f"{base}:qty", qty, unit="units" if group == "fund" else "shares")
        self.journal.state(f"{base}:cost", money(mark * qty))
        self.journal.state(f"{base}:realized", floor=None)

    def plan(self, d, end, intents):
        def add(hour, minute, name, **params):
            intents.append((datetime.combine(d, time(hour, minute)), name, params))

        if d.weekday() < 5:
            add(9, 30, "market")
        if d.weekday() < 5 and 15 <= d.day <= 17 and (d.day == 15 or d.weekday() == 0):
            add(9, 40, "sip")
        if d.month in (3, 6, 9, 12) and d.weekday() < 5 and 25 <= d.day <= 27 and (d.day == 25 or d.weekday() == 0):
            add(14, 30, "redeem")
        if d.weekday() < 5 and (d.day in (4, 11, 18, 24) or
                              (d.weekday() == 0 and any(d.day - day in (1, 2) for day in (4, 11, 18, 24)))):
            add(13, 30, "trade")
        if d.day == 1 and d.month != 1:
            add(5, 0, "yield", account="wealth", funding="", item="理财收益", divisor=12)
        if d == end or (d + timedelta(days=1)).month != d.month:
            intents.append((datetime.combine(d, time(23, 59, 59)), "snapshot", {}))

    def mark(self, when):
        changes = []
        for group, instruments in (("fund", self.funds), ("stock", self.stocks)):
            for code in instruments:
                key = f"{group}:{code}:mark"
                prev = self.journal.values[key]
                drift = (self.bias[code] / 30 + self.rng.gauss(0, 0.006) if group == "fund"
                         else self.rng.gauss(0.0002, 0.015))
                price = max(Decimal("0.50") if group == "fund" else Decimal("1"),
                            money(prev * Decimal(str(1 + drift))))
                changes.append(Change(key, price - prev))
        self.journal.record(when, Business("valuation", "更新市场价格", "投资理财", "中性", "每日"),
                            ZERO, payer="market", payee=A.OWNER, changes=tuple(changes))

    def sip(self, when, ledger):
        picks = self.rng.sample(list(self.funds), 2)
        flows, changes, rows = [], [], []
        for code in picks:
            gross = D.FUND_SIP / 2
            fee = money(gross * Decimal("0.0015"))
            base = f"fund:{code}"
            nav = self.journal.values[f"{base}:mark"]
            units = money((gross - fee) / nav)
            flows.extend((Leg("salary", base, gross - fee, "fund_subscription"),
                          Leg("salary", "fund_app", fee, "fund_fee")))
            changes.extend((Change(f"{base}:qty", units), Change(f"{base}:cost", gross)))
            rows.append(self._fund_row(when, code, "申购确认", gross, units, nav, fee))
        e = self.journal.record(when, Business("asset_move", "基金定投扣款", "投资理财", "中性", "每月",
                                                f"sip:{when:%Y-%m}"), D.FUND_SIP,
                                payer=A.OWNER, payee="fund_app", legs=tuple(flows), changes=tuple(changes))
        if e.status == "settled":
            ledger._project(e, "salary", "支出", "天天基金", kind="investment")
            self.fund_rows.extend(r | {"event_id": e.id} for r in rows)

    def _fund_row(self, when, code, business, amount, units, nav, fee):
        cash_for_units = amount if business == "赎回确认" else amount - fee
        return {"date": when.date().isoformat(), "code": code, "name": self.funds[code],
                "business": business, "amount": amount, "shares": units, "nav": nav, "fee": fee,
                "rounding": cash_for_units - units * nav}

    def redeem(self, when, ledger, *, code=None, units=None):
        v = self.journal.values
        code = code or max(self.funds, key=lambda c: v[f"fund:{c}:qty"] * v[f"fund:{c}:mark"])
        base = f"fund:{code}"
        nav, held, basis = v[f"{base}:mark"], v[f"{base}:qty"], v[f"{base}:cost"]
        units = units if units is not None else min(held, (Decimal("1500") / nav).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        if units <= 0 or units > held or units != money(units):
            return None
        gross = money(units * nav)
        fee = money(gross * Decimal("0.005"))
        net = gross - fee
        cost = money(basis * units / held)
        flows = (Leg(base, "salary", net, "fund_redemption"),)
        if fee:
            flows += (Leg(base, "fund_app", fee, "fund_fee"),)
        e = self.journal.record(when, Business("asset_move", "基金赎回款", "投资理财", "中性", "每季", base),
                                gross, payer=base, payee=A.OWNER, initiator=A.OWNER, legs=flows,
                                changes=(Change(f"{base}:qty", -units), Change(f"{base}:cost", -cost),
                                         Change(f"{base}:realized", net - cost)))
        if e.status == "settled":
            ledger._project(e, "salary", "收入", "天天基金", kind="investment", amount=net)
            self.fund_rows.append(self._fund_row(when, code, "赎回确认", gross, units, nav, fee) | {"event_id": e.id})
        return e

    def trade(self, when, *, code=None, side=None, qty=None):
        if code is None and self.rng.random() >= 0.8:
            return None
        code = code or self.rng.choice(list(self.stocks))
        base = f"stock:{code}"
        v = self.journal.values
        price, held, basis = v[f"{base}:mark"], v[f"{base}:qty"], v[f"{base}:cost"]
        side = side or ("买入" if self.rng.random() < 0.6 else "卖出")
        buy = side == "买入"
        if qty is None:
            desired = money(self.rng.uniform(3000, 15000)) / price
            qty = min(desired, self.journal.cash["broker_cash"] / price if buy else held) // 100 * 100
        if qty <= 0:
            return None
        gross = money(qty * price)
        fee = money(max(Decimal(5), gross * Decimal("0.00025")))
        stamp = money(gross * Decimal("0.0005")) if not buy else ZERO
        fees = fee + stamp
        if buy:
            flows = (Leg("broker_cash", "market", gross, "stock_purchase"),
                     Leg("broker_cash", "broker", fee, "broker_fee"))
            basis_delta, realized, signed = gross + fees, ZERO, qty
        else:
            if qty > held:
                return None
            flows = (Leg("market", "broker_cash", gross, "stock_sale"),
                     Leg("broker_cash", "broker", fee, "broker_fee"))
            if stamp:
                flows += (Leg("broker_cash", "tax", stamp, "stamp_duty"),)
            basis_delta = -money(basis * qty / held)
            realized, signed = gross - fees + basis_delta, -qty
        e = self.journal.record(when, Business("asset_move", side, "投资理财", "中性", "事件", base),
                                gross, payer=A.OWNER if buy else "market", payee="market" if buy else A.OWNER,
                                initiator=A.OWNER, legs=flows,
                                changes=(Change(f"{base}:qty", signed), Change(f"market:{code}:qty", -signed),
                                         Change(f"{base}:cost", basis_delta), Change(f"{base}:realized", realized)))
        if e.status == "settled":
            self.broker_rows.append({"date": when.date().isoformat(), "code": code, "name": self.stocks[code],
                                     "side": side, "qty": int(qty), "price": price, "amount": gross,
                                     "fee": fees, "pnl": "" if buy else f"{realized:.2f}", "event_id": e.id})
        return e

    def _market_value(self, group, code):
        v = self.journal.values
        return money(v[f"{group}:{code}:qty"] * v[f"{group}:{code}:mark"])

    def fund_value(self):
        return sum((self._market_value("fund", c) for c in self.funds), ZERO)

    def broker_value(self):
        return self.journal.cash["broker_cash"] + sum((self._market_value("stock", c) for c in self.stocks), ZERO)

    def snapshot(self, when):
        event = self.journal.record(when, Business("snapshot", "资产快照", "投资理财", "中性", "每月"),
                                    ZERO, payer=A.OWNER, payee=A.OWNER)
        d = when.date().isoformat()
        v = self.journal.values

        def add(account, name, market, basis, cash=ZERO, realized=ZERO, accrued=ZERO):
            floating = market - basis
            self.snapshots.append({"event_id": event.id, "date": d, "account": account, "name": name,
                                   "market_value": market, "cost": basis, "cash": cash,
                                   "realized": realized + accrued, "total_pnl": floating + realized + accrued,
                                   "pnl": floating, "pnl_pct": money(floating / basis * 100) if basis else ZERO})

        for code, name in self.funds.items():
            add("基金", name, self._market_value("fund", code), v[f"fund:{code}:cost"],
                realized=v[f"fund:{code}:realized"])
        cash = self.journal.cash["broker_cash"]
        add("证券", "华泰证券账户", self.broker_value() - cash,
            sum(v[f"stock:{code}:cost"] for code in self.stocks), cash,
            sum(v[f"stock:{code}:realized"] for code in self.stocks))
        principal = D.ACCOUNTS["wealth"].opening
        add("理财", "招行理财", principal, principal, self.journal.cash["wealth"] - principal,
            accrued=self.journal.cash["wealth"] - principal)
