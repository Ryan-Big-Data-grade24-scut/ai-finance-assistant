"""Business actions and funding routes used by the household calendar."""
from datetime import datetime, timedelta
from decimal import Decimal

from . import actors as A, data as D
from .journal import Business, Change, Event, Leg, ZERO, money


def legs(route, amount, purpose):
    return tuple(Leg(src, dst, amount, purpose) for src, dst in zip(route, route[1:]))


class Settlement:
    def _endpoint(self, account, funding=""):
        if account in D.PAYABLE_FUNDING:
            return f"{account}:{funding or D.DEFAULT_FUNDING[account]}"
        return A.CREDIT_ISSUER.get(account, account)

    def _business(self, kind, item, category, *, channel="", purpose="", reference=""):
        from .truth import business_labels

        cls, cadence = business_labels(kind, item, category, channel, purpose)
        return Business(kind, item, category, cls, cadence, reference)

    def _record(self, at, business, amount, source, destination, **kwargs):
        return self.journal.record(at, business, money(amount),
                                   payer=self.journal.accounts[source].owner,
                                   payee=self.journal.accounts[destination].owner, **kwargs)

    def _due(self, when, business, amount, payer, payee):
        key = self.journal.state(f"due:{business.reference}")
        if not self.journal.values[key]:
            self.journal.record(when, Business("due", business.item, business.category,
                                               "中性", business.cadence, business.reference),
                                amount, payer=payer, payee=payee, changes=(Change(key, amount),))
        if self.journal.values[key] != amount:
            raise ValueError(f"unexpected billed amount: {business.reference}")
        return (Change(key, -amount),)

    def receipt(self, account, when, amount, category, counterparty, item, *, action, **view):
        kind = view.get("kind", "income")
        dst = self._endpoint(account, view.get("funding", ""))
        changes = ()
        if action == "contribution":
            src, business_kind, ref = A.SPOUSE, action, f"shared-budget:{when:%Y-%m}"
        elif action == "tax_refund":
            src, business_kind = "tax", "tax_refund"
            ref = f"tax:{when.year - 1}:refundable_withholding"
            # The calendar declares one pre-window withholding claim, not annual free cash.
            if ref not in self.journal.states:
                return None
            changes = (Change(ref, -money(amount)),)
        elif action in {"salary", "bonus"}:
            src, business_kind = "employer", "payroll"
            ref = f"employment:{when:%Y-%m}:{action}"
            amount = self.journal.values["employment:net_salary"] if action == "salary" else D.BONUS
        else:
            raise ValueError(f"unsupported income instruction: {item}")
        route = (src, dst)
        business = self._business(business_kind, item, category, reference=ref)
        if business_kind == "payroll":
            changes += self._due(when, business, amount, self.journal.accounts[src].owner,
                                 self.journal.accounts[dst].owner)
        e = self._record(when, business, amount, src, dst, legs=legs(route, amount, business_kind),
                         changes=changes, route=route)

        if account in D.PAYABLE_FUNDING:
            view["funding"] = dst.split(":", 1)[1]
        if e.status == "settled":
            return self._project(e, account, "收入", counterparty, **(view | {"kind": kind}))
        return None

    def _sale(self, account, when, amount, category, counterparty, item, *,
              funding="", pay_method="", order_channel="", merchant="", merchant_no="",
              kind="purchase", purpose="", mirror="", credit="", initiator=A.OWNER, allowance=""):
        amount = money(amount)
        if amount <= 0:
            return None
        loan = A.LOANS.get(item)
        gift = purpose == "gift"
        name = loan.lender if loan else merchant or A.SERVICE_PROVIDER.get(counterparty, counterparty)
        role = "lender" if loan else "person" if gift else D.party_kind(name)
        seller = "cmb" if loan and loan.lender == "招商银行" else A.external(self.journal, name, role)
        src = self._endpoint(credit or mirror or account, funding)
        collector = f"{account}_clearing" if account in D.PAYABLE_FUNDING and not gift else ""
        checkout = ""
        if order_channel and A.SERVICE_PROVIDER.get(counterparty, counterparty) != name:
            checkout = A.external(self.journal, counterparty, "marketplace")
        route = (src, *(key for key in (collector, checkout) if key), seller)
        contract = purpose == "contract" or loan is not None
        label = "loan" if loan else "contract" if contract else "gift" if gift else "purchase"
        if order_channel and not merchant_no:
            merchant_no = str(self.rng.randint(10**15, 10**16 - 1))
        ref = (f"loan:{loan.key}:{when:%Y-%m}" if loan else
               f"contract:{seller}:{item}:{when:%Y-%m}" if contract else
               f"order:{order_channel}:{merchant_no}" if order_channel else
               f"{'gift' if gift else 'sale'}:{seller}:{len(self.journal.events)+1}")
        business = self._business(label, item, category, channel=order_channel,
                                  purpose=purpose, reference=ref)
        changes = []
        flows = legs(route, amount, "purchase" if not gift else "gift")
        if loan:
            if (credit or mirror or account) != loan.channel:
                raise ValueError(f"wrong loan payment account: {item}")
            key = f"loan:{loan.key}"
            remaining = self.journal.values[key]
            if remaining <= 0:
                return None
            charge = money(remaining * loan.rate)
            principal = min(amount - charge, remaining)
            amount = principal + charge
            changes.append(Change(key, -principal))
            if src == seller:  # Convert a legacy installment into the current statement, not a cash purchase.
                route, flows = (src,), ()
            else:
                flows = tuple(Leg(src, seller, value, name) for name, value in
                              (("loan_principal", principal), ("loan_interest", charge)) if value > 0)
        if credit:
            key = f"credit:{credit}"
            prepaid = min(amount, max(ZERO, -self.journal.values[key]))
            if prepaid:
                flows = (Leg(f"prepaid:{credit}", src, prepaid, "apply_card_credit"),) + flows
            changes.append(Change(key, amount))
        if allowance:
            changes.append(Change(allowance, amount))
        if contract:
            changes.extend(self._due(when, business, amount, A.OWNER, seller))
        e = self.journal.record(when, business, amount, payer=A.OWNER, payee=seller,
                                initiator=initiator, legs=flows, changes=tuple(changes),
                                route=route, credit=credit)
        if e.status != "settled":
            return None
        p = self._project(e, account, "支出", counterparty, kind=kind, funding=funding,
                          pay_method=pay_method, order_channel=order_channel,
                          merchant=merchant, merchant_no=merchant_no,
                          effect="none" if mirror or credit == "huabei" else "out")
        if mirror:
            self._project(e, mirror, "支出", counterparty, kind=kind, merchant=merchant, mirror=True)
        if business.kind == "purchase":
            self.sales[e.id] = (e, p, mirror)
        return p

    def spend(self, account, when, amount, category, counterparty, item, **view):
        view.pop("channel", None)
        credit = account if account in D.CREDIT_ACCOUNTS else ""
        return self._sale(account, when, amount, category, counterparty, item, credit=credit, **view)

    def _wallet_pay(self, wallet, pay_method, when, amount, category, counterparty, item, **view):
        issuer = next((key for key, label in D.CREDIT_LABEL.items() if label == pay_method), "")
        funding, mirror, credit, initiator, allowance = "", "", "", A.OWNER, ""
        if issuer:
            funding, mirror, credit = "信用卡", issuer, issuer
        elif pay_method == "花呗":
            funding, credit = "花呗", "huabei"
        elif pay_method.startswith("支付宝小荷包"):
            funding, mirror = "小荷包", view.pop("wallet_account", "wallet_family")
        elif pay_method.startswith("亲情卡"):
            funding, initiator, allowance = "余额宝", A.MOTHER, "allowance:parent"
        elif pay_method.startswith("亲属卡"):
            funding, initiator, allowance = "零钱通", A.CHILD, "allowance:child"
        else:
            funding = "余额" if pay_method == "账户余额" else pay_method
        if funding in {"余额宝", "零钱通"} and self._product(wallet, funding) - amount < D.PAYMENT_FLOOR[funding]:
            return None
        return self._sale(wallet, when, amount, category, counterparty, item,
                          funding=funding, pay_method=pay_method, mirror=mirror, credit=credit,
                          initiator=initiator, allowance=allowance, **view)

    def refund(self, when: datetime, original: str, amount: Decimal):
        sale, p, mirror = self.sales[original]
        amount = money(amount)
        so_far = self.refunded.get(original, ZERO)
        reverse = tuple(reversed(sale.route))
        business = self._business("refund", f"退款-{sale.business.item}", sale.business.category,
                                  reference=sale.business.reference)
        view = {"original": original, "route": reverse, "credit": sale.credit}
        if amount <= 0 or amount + so_far > sale.amount or when < sale.at:
            return self.journal.record(when, business, amount, payer=sale.payee, payee=sale.payer,
                                       status="failed", failure="refund_limit_or_date", **view)
        flows = legs(reverse, amount, "refund")
        changes = []
        if sale.credit:
            credit = sale.credit
            key = f"credit:{credit}"
            used = self.journal.values[key]
            excess = max(ZERO, amount - max(ZERO, used))
            if excess:
                flows += (Leg(reverse[-1], f"prepaid:{credit}", excess, "card_credit"),)
            changes.append(Change(key, -amount))
            bill_date = self.statement_at.get(credit)
            bill = self.journal.values[f"bill:{credit}"]
            if bill and bill_date is not None and sale.at < bill_date:
                changes.append(Change(f"bill:{credit}", -min(bill, amount)))
            view["initiator"] = sale.payee
        e = self.journal.record(when, business, amount, payer=sale.payee, payee=sale.payer,
                                legs=flows, changes=tuple(changes), **view)
        if e.status == "settled":
            total = self.refunded[original] = so_far + amount
            p.refunded = total
            self._project(e, p.account, "收入", p.counterparty, kind="refund", funding=p.funding,
                          pay_method=p.pay_method, status="退款成功",
                          effect="none" if mirror or sale.credit == "huabei" else "in",
                          refund_full=total == sale.amount)
            if mirror:
                self._project(e, mirror, "收入", p.counterparty, kind="refund", mirror=True,
                              status="退款成功")
        return e

    def transfer(self, when, src, dst, amount, item="转账", counterparty="", *,
                 source_funding="", withdraw=False):
        if src in D.PAYABLE_FUNDING and not source_funding:
            prefer = "余额宝" if src == "alipay" else "零钱通"
            source_funding = next((k for k in (prefer, D.DEFAULT_FUNDING[src])
                                   if self._product(src, k) - amount >= D.PAYMENT_FLOOR.get(k, ZERO)), prefer)
        destination_funding = D.DEFAULT_FUNDING.get(dst, "")
        amount = money(amount)
        source = self._endpoint(src, source_funding)
        destination = self._endpoint(dst, destination_funding)
        route = (source, destination)
        changes = ()
        if dst in A.CREDIT_ISSUER:
            key = f"credit:{dst}"
            amount = min(amount, self.journal.values[key])
            if amount <= 0:
                return False
            bill = f"bill:{dst}"
            changes = (Change(key, -amount), Change(bill, -min(amount, self.journal.values[bill])))
        elif src in D.PAYABLE_FUNDING and self.journal.cash[source] - amount < D.PAYMENT_FLOOR.get(source_funding, ZERO):
            return False
        category = "信用借还" if dst == "huabei" else "转账红包"
        business = self._business("settlement", item, category,
                                  reference=f"statement:{dst}:{when:%Y-%m}" if changes else "")
        e = self._record(when, business, amount, source, destination, legs=legs(route, amount, "transfer"),
                         changes=changes, route=route, credit=dst if changes else "")
        if e.status != "settled":
            return False
        debit = D.ACCOUNTS[src].kind == "debit"
        src_view = {"kind": "transfer", "funding": source_funding, "effect": "out"}
        if withdraw:
            src_view.update(pay_method="零钱", platform_type="零钱提现", status="提现已到账",
                            counterparty_account="/", item="/")
        if dst == "huabei":
            src_view.update(pay_method="余额宝" if source_funding == "余额宝" else "账户余额",
                            counterparty_account="/", status="还款成功")
        peer = "花呗" if dst == "huabei" else counterparty or D.ACCOUNTS[dst].name
        self._project(e, src, "支出" if debit else "中性", peer, **src_view)
        if dst == "huabei":
            if src == "salary":
                self._project(e, "alipay", "中性", "花呗", kind="transfer", effect="none",
                              pay_method="招商银行储蓄卡(0371)", status="还款成功", counterparty_account="/")
            return True
        src_acct = D.ACCOUNTS[src]
        dst_item = {"alipay": "充值", "wechat": "零钱充值"}.get(dst, item)
        src_label = src_acct.name
        dst_pay = ""
        if destination_funding:
            masked = src_acct.mask.replace(" ", "")[-4:]
            dst_pay = f"{src_label}({masked})" if masked else src_label
        self._project(e, dst, "中性" if destination_funding else "收入", src_label,
                      when=when + timedelta(seconds=45), item=dst_item, kind="transfer",
                      funding=destination_funding, effect="in", pay_method=dst_pay)
        return True

    def _statement(self, when, credit):
        key = f"bill:{credit}"
        amount = max(ZERO, self.journal.values[f"credit:{credit}"])
        self.journal.record(when, self._business("statement", "信用账单出账", "信用借还",
                                                reference=f"statement:{credit}:{when:%Y-%m}"),
                            amount, payer=A.OWNER, payee=A.CREDIT_ISSUER[credit], credit=credit,
                            changes=(Change(key, amount - self.journal.values[key]),))
        self.statement_at[credit] = when

    def _repay(self, when, credit):
        amount = min(self.journal.values[f"bill:{credit}"],
                     max(ZERO, self.journal.values[f"credit:{credit}"]))
        if not amount:
            return
        if credit == "huabei":
            funding = next((k for k in ("余额宝", "余额")
                            if self._product("alipay", k) - amount >= D.PAYMENT_FLOOR.get(k, ZERO)), "")
            if funding:
                self.transfer(when, "alipay", credit, amount,
                              f"花呗主动还款-{when:%Y年%m月}账单", source_funding=funding)
            else:
                # A linked bank card can settle the statement if neither wallet product can.
                self.transfer(when, "salary", credit, amount, f"花呗主动还款-{when:%Y年%m月}账单")
        else:
            self.transfer(when, "salary", credit, amount, "信用卡还款")

    def _move(self, account, when, src, dst, amount, *, item, counterparty, category="投资理财", **view):
        source, destination = f"{account}:{src}", f"{account}:{dst}"
        amount = money(amount)
        if self.journal.cash[source] - amount < D.PAYMENT_FLOOR.get(src, ZERO):
            return None
        route = (source, destination)
        e = self._record(when, self._business("asset_move", item, category), amount, source, destination,
                         legs=legs(route, amount, "product_switch"), route=route)
        if e.status == "settled":
            return self._project(e, account, "中性", counterparty, kind="transfer",
                                 effect="none", funding=src, **view)
        return None

    def _yield(self, when, account, funding, *, item="理财收益", counterparty="/", divisor=365, **view):
        dst = self._endpoint(account, funding)
        src = A.YIELD_PROVIDER[dst]
        rate = Decimal("0.0026") if account == "wealth" else D.WALLET_PRODUCTS[dst].annual_yield / divisor
        amount = money(self.journal.cash[dst] * rate)
        if amount <= 0:
            return
        route = (src, dst)
        e = self._record(when, self._business("yield", item, "投资理财", purpose="daily" if divisor == 365 else "monthly"),
                         amount, src, dst, legs=legs(route, amount, "yield"), route=route)
        if e.status != "settled":
            return
        row = {"date": when.date().isoformat(), "amount": amount, "balance": self.journal.cash[dst], "event_id": e.id}
        if account == "wechat":
            self.wechat_yield_rows.append(row | {"product": funding})
        elif account == "wealth":
            self.invest.wealth_rows.append(row | {"note": item})
        else:
            self._project(e, account, "中性", counterparty, effect="in", funding=funding, kind="income", **view)
