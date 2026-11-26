"""虚构账单引擎：账户余额、分期摊还、投资收益与逐日交易生成。

设计原则
--------
1. 确定性：同一个 seed 必须产出逐字节一致的数据（时间、金额、单号都不依赖系统时钟）。
2. 账务自洽：储蓄卡/钱包余额不为负、信用卡不超额、转账两边金额相等。
3. 平台视角：同一笔消费可能出现在多份导出里（微信账单 + 信用卡账单 + 美团订单），
   这正是真实数据的形态；每份导出只看自己平台的那一面。
4. 原始格式优先：生成的是平台导出文件，不是我们数据库的表结构。
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP

from . import data as D


TWO = Decimal("0.01")


def money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(TWO, rounding=ROUND_HALF_UP)


def _fmt(value: Decimal) -> str:
    return f"{value:.2f}"


def _month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


@dataclass
class Posting:
    account: str
    when: datetime
    direction: str          # 支出 / 收入 / 中性（平台导出口径）
    amount: Decimal
    category: str
    counterparty: str
    item: str
    merchant: str = ""     # 订单类平台里的实际店铺/商户（钱包账单里仍显示平台名）
    pay_method: str = ""
    status: str = "交易成功"
    tx_no: str = ""
    merchant_no: str = ""
    remark: str = ""
    kind: str = "purchase"  # purchase / transfer / income / refund / investment / fee / closed
    order_channel: str = ""  # meituan / taobao / tmall / jd
    effect: str = ""        # in / out / none；为空时按 direction 推导
    wallet_domain: str = ""
    funding: str = ""       # 钱包内部出资产品：余额 / 余额宝 / 零钱 / 零钱通 / 花呗...
    counterparty_account: str = ""   # 支付宝「对方账号」等稳定脱敏账号
    platform_type: str = ""          # 微信「交易类型」等平台自有类型
    # ── 答案层（不影响平台导出格式，用于 ground truth 与后端对账）──
    counterparty_type: str = ""      # individual / merchant / person / platform / institution
    platform_category: str = ""      # 平台原始分类（故意带系统性偏差）
    expense_class: str = ""          # 收入 / 义务支出 / 刚性支出 / 弹性支出 / 中性
    cadence: str = ""                # 每日/每周/每月/每季/半年/每年/事件/结算
    flow_role: str = ""              # income/primary/mirror/settlement/asset_move/...
    mirror_of: str = ""              # 镜像行的主流水 truth_id
    truth_id: str = ""               # 稳定答案键


@dataclass
class Dataset:
    postings: list[Posting]
    balances: dict[str, Decimal]
    credit_used: Decimal
    credit_used_by: dict[str, Decimal]
    snapshots: list[dict]
    fund_rows: list[dict]
    broker_rows: list[dict]
    wealth_rows: list[dict]
    start: date
    end: date
    seed: int
    wallet_products: dict[str, Decimal]
    huabei_used: Decimal
    wechat_yield_rows: list[dict]

    def by_account(self, *accounts: str) -> list[Posting]:
        wanted = set(accounts)
        return [p for p in self.postings if p.account in wanted]

    def digest(self) -> str:
        h = hashlib.sha256()
        for p in self.postings:
            h.update(
                f"{p.account}|{p.when:%Y-%m-%d %H:%M:%S}|{p.direction}|{p.amount}|"
                f"{p.counterparty}|{p.item}|{p.tx_no}\n".encode("utf-8")
            )
        return h.hexdigest()

    def monthly_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for p in self.postings:
            key = p.when.strftime("%Y-%m")
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items()))

    def monthly_export_rows(self) -> dict[str, int]:
        """各平台导出文件的总行数：记账笔数 + 电商平台自己的订单行。"""
        counts = self.monthly_counts()
        for p in self.postings:
            if p.order_channel and p.kind == "purchase":
                key = p.when.strftime("%Y-%m")
                counts[key] += 1
        return dict(sorted(counts.items()))


# 小荷包账户 ID：每个钱包一个，用于文件头和分域去重（只是虚构数字）。
WALLET_DOMAINS = {
    "wallet_family": "2026010100000001",
    "wallet_travel": "2026010100000002",
    "wallet_auto": "2026010100000003",
    "wallet_education": "2026010100000004",
}

WALLET_NAME = {
    "wallet_family": "家庭开销",
    "wallet_travel": "旅行基金",
    "wallet_auto": "自动攒",
    "wallet_education": "教育金",
}


class Ledger:
    def __init__(self, seed: int, start: date, end: date):
        self.seed = seed
        self.rng = random.Random(seed)
        self.start = start
        self.end = end
        self.postings: list[Posting] = []
        self.bal = {aid: acct.opening for aid, acct in D.ACCOUNTS.items()}
        # 钱包不是单一余额：支付宝有余额/余额宝/理财，微信有零钱/零钱通/理财通。
        self.products = {key: p.opening for key, p in D.WALLET_PRODUCTS.items()}
        self._sync_wallet("alipay")
        self._sync_wallet("wechat")
        # 每张信用卡独立计额度、独立账单；credit_used 只是合计，用于报表。
        self.credit_used_by = {aid: Decimal("0.00") for aid in D.CREDIT_ACCOUNTS}
        self.credit_used = Decimal("0.00")
        self.bills = {aid: Decimal("0.00") for aid in D.CREDIT_ACCOUNTS}
        self.huabei_used = Decimal("0.00")
        self.huabei_bill = Decimal("0.00")
        self.qinqing_used = Decimal("0.00")
        self.qinshu_used = Decimal("0.00")
        self.salary_amount = D.SALARY
        self.counter = 0
        self.wechat_yield_rows: list[dict] = []
        self.invest = Investments(self.rng, start, end)

    # ── 单号 ────────────────────────────────────────────────
    def order_no(self, account: str, when: datetime, *, width: int = 18) -> str:
        self.counter += 1
        tail = f"{self.rng.randint(0, 10 ** 6 - 1):06d}{self.counter:06d}"
        platform = D.ACCOUNTS[account].platform
        if platform == "alipay":
            return (f"{when:%Y%m%d%H%M%S}{tail}{self.rng.randint(10, 99)}"[:28]).ljust(28, "0")
        if platform == "wechat":
            return f"4200{when:%Y%m%d%H%M%S}{tail}"[:28].ljust(28, "0")
        if platform == "alipay_wallet":
            return (f"{when:%Y%m%d}{tail}{self.rng.randint(10, 99)}"[:26]).ljust(26, "0")
        if platform in {"cmb_debit", "cmb_credit", "icbc_debit", "ccb_debit", "cgb_credit"}:
            return (f"{when:%Y%m%d}{tail}"[:20]).ljust(20, "0")
        return (f"{when:%Y%m%d}{tail}"[:width]).ljust(width, "0")

    # ── 钱包内部产品 ────────────────────────────────────────
    def _sync_wallet(self, account: str) -> None:
        total = sum((v for k, v in self.products.items()
                     if k.startswith(f"{account}:")), Decimal("0.00"))
        self.bal[account] = money(total)

    def _product(self, account: str, funding: str) -> Decimal:
        return self.products.get(f"{account}:{funding}", Decimal("0.00"))

    def _adjust_product(self, account: str, funding: str, delta: Decimal) -> bool:
        key = f"{account}:{funding}"
        if key not in self.products:
            return False
        value = self.products[key] + delta
        if value < 0:
            return False
        self.products[key] = money(value)
        self._sync_wallet(account)
        return True

    def _payable(self, account: str) -> Decimal:
        return sum((self._product(account, name) for name in D.PAYABLE_FUNDING[account]),
                   Decimal("0.00"))

    def _default_funding(self, account: str, prefer: str = "") -> str:
        """优先用指定的可付款产品；不够时再退回同平台其他可付款产品。"""
        if prefer and self._product(account, prefer) > 0:
            return prefer
        for name in D.PAYABLE_FUNDING[account]:
            if self._product(account, name) > 0:
                return name
        return D.DEFAULT_FUNDING[account]

    # ── 记账原语 ────────────────────────────────────────────
    @staticmethod
    def _effect(direction: str, effect: str | None) -> str:
        if effect:
            return effect
        return {"支出": "out", "收入": "in"}.get(direction, "none")

    def _post(self, account: str, when: datetime, direction: str, amount: Decimal,
              category: str, counterparty: str, item: str, **kwargs) -> Posting | None:
        amount = money(amount)
        if amount <= 0:
            return None
        acct = D.ACCOUNTS[account]
        effect = self._effect(direction, kwargs.pop("effect", None))
        funding = kwargs.pop("funding", "")
        if account in D.PAYABLE_FUNDING and effect in {"in", "out"}:
            funding = funding or self._default_funding(account)
            delta = amount if effect == "in" else -amount
            if not self._adjust_product(account, funding, delta):
                return None
        elif effect == "out":
            if acct.kind == "credit":
                if self.credit_used_by[account] + amount > (acct.limit or Decimal("0")):
                    return None
                self.credit_used_by[account] += amount
                self.credit_used += amount
            else:
                if self.bal[account] < amount:
                    return None
                self.bal[account] -= amount
        elif effect == "in":
            if acct.kind == "credit":
                paid = min(amount, self.credit_used_by[account])
                self.credit_used_by[account] -= paid
                self.credit_used = max(Decimal("0.00"), self.credit_used - paid)
            else:
                self.bal[account] += amount

        account_no = kwargs.pop("counterparty_account", "") or D.party_account(counterparty)
        posting = Posting(
            account=account, when=when, direction=direction, amount=amount,
            category=category, counterparty=counterparty, item=item,
            tx_no=kwargs.pop("tx_no", "") or self.order_no(account, when),
            wallet_domain=WALLET_DOMAINS.get(account, ""),
            effect=effect, funding=funding, counterparty_account=account_no, **kwargs,
        )
        self.postings.append(posting)
        return posting

    def income(self, account: str, when: datetime, amount: Decimal, category: str,
               counterparty: str, item: str, **kwargs) -> Posting | None:
        return self._post(account, when, "收入", amount, category, counterparty, item, **kwargs)

    def spend(self, account: str, when: datetime, amount: Decimal, category: str,
              counterparty: str, item: str, **kwargs) -> Posting | None:
        amount = money(amount)
        kwargs.pop("channel", None)  # 直接指定账户时，channel 只是调用方注释
        acct = D.ACCOUNTS[account]
        # 信用卡/花呗付款在钱包账单里也要留一行，但这笔钱不从钱包余额扣。
        if kwargs.get("effect") != "none":
            if account in D.PAYABLE_FUNDING:
                funding = kwargs.get("funding") or self._default_funding(account)
                if self._product(account, funding) < amount:
                    return None
            elif acct.kind != "credit" and self.bal[account] < amount:
                return None
        return self._post(account, when, "支出", amount, category, counterparty, item, **kwargs)

    def transfer(self, when: datetime, src: str, dst: str, amount: Decimal,
                 item: str = "转账", counterparty: str = "") -> bool:
        """两边成对记账；源账户余额不足则整笔放弃。"""
        amount = money(amount)
        src_acct = D.ACCOUNTS[src]
        dst_acct = D.ACCOUNTS[dst]
        src_funding = ""
        if src in D.PAYABLE_FUNDING:
            prefer = "余额宝" if src == "alipay" else "零钱通"
            src_funding = self._default_funding(src, prefer)
            if self._product(src, src_funding) < amount:
                return False
        elif src_acct.kind != "credit" and self.bal[src] < amount:
            return False
        if src_acct.kind == "credit" \
                and self.credit_used_by[src] + amount > (src_acct.limit or Decimal("0")):
            return False

        pair = str(self.rng.randint(10 ** 9, 10 ** 10 - 1))

        src_dir = "支出" if src_acct.platform in {"cmb_debit", "icbc_debit", "ccb_debit"} else "中性"
        if not self._post(src, when, src_dir, amount, "转账红包",
                          counterparty or dst_acct.name, item, kind="transfer",
                          merchant_no=pair, effect="out", funding=src_funding):
            return False

        dst_funding = ""
        dst_pay = ""
        if dst in D.PAYABLE_FUNDING:
            dst_funding = "余额" if dst == "alipay" else "零钱"
            if src_acct.mask:
                dst_pay = f"{src_acct.name}({src_acct.mask.replace(' ', '')[-4:]})"
            else:
                dst_pay = src_acct.name
        if dst_acct.platform == "alipay":
            dst_dir, dst_item = "中性", "充值"
        elif dst_acct.platform == "wechat":
            dst_dir, dst_item = "中性", "零钱充值"
        else:
            dst_dir, dst_item = "收入", item
        # 收款方看到的是付款账户名，而不是付款方账单里填的收款对象。
        self._post(dst, when + timedelta(seconds=45), dst_dir, amount, "转账红包",
                   src_acct.name, dst_item, kind="transfer",
                   merchant_no=pair, effect="in", funding=dst_funding,
                   pay_method=dst_pay)
        return True

    # ── 付款：选通道、可能同时产生信用卡流水 ─────────────────
    def _wallet_pay(self, wallet: str, pay_method: str, when: datetime, amount: Decimal,
                    category: str, counterparty: str, item: str, **kwargs) -> bool:
        credit_for_label = {label: aid for aid, label in D.CREDIT_LABEL.items()}
        if pay_method in credit_for_label:
            aid = credit_for_label[pay_method]
            limit = D.ACCOUNTS[aid].limit or Decimal("0")
            if self.credit_used_by[aid] + amount > limit:
                return False
            if not self.spend(wallet, when, amount, category, counterparty, item,
                              pay_method=pay_method, effect="none", funding="信用卡",
                              **kwargs):
                return False
            return self.spend(aid, when, amount, category, counterparty, item,
                              pay_method="", kind=kwargs.get("kind", "purchase")) is not None

        if pay_method == "花呗":
            if self.huabei_used + amount > D.HUABEI_LIMIT:
                return False
            if not self.spend(wallet, when, amount, category, counterparty, item,
                              pay_method=pay_method, effect="none", funding="花呗",
                              **kwargs):
                return False
            self.huabei_used += amount
            return True

        if pay_method.startswith("亲情卡"):
            if self.qinqing_used + amount > D.QINQING_LIMIT:
                return False
            if not self.spend(wallet, when, amount, category, counterparty, item,
                              pay_method=pay_method, funding="余额宝", **kwargs):
                return False
            self.qinqing_used += amount
            return True

        if pay_method.startswith("亲属卡"):
            if self.qinshu_used + amount > D.QINSHU_LIMIT:
                return False
            if not self.spend(wallet, when, amount, category, counterparty, item,
                              pay_method=pay_method, funding="零钱通", **kwargs):
                return False
            self.qinshu_used += amount
            return True

        # 小荷包也是支付宝的一种付款方式：主账单留一行（付款方式写小荷包），
        # 钱从小荷包余额扣，并在小荷包自己的账单里再出现一次。
        if pay_method.startswith("支付宝小荷包"):
            if not self.spend(wallet, when, amount, category, counterparty, item,
                              pay_method=pay_method, effect="none", funding="小荷包",
                              **kwargs):
                return False
            return self.spend("wallet_family", when, amount, category,
                              counterparty, item, **kwargs) is not None

        # 余额宝/零钱通同时是理财账户，真实用户不会为了付款把它清零；
        # 低于保留金额时改用花呗/信用卡，等下次转入再恢复。
        if pay_method == "余额宝" and self._product(wallet, pay_method) - amount < Decimal("15000"):
            return False
        if pay_method == "零钱通" and self._product(wallet, pay_method) - amount < Decimal("5000"):
            return False

        funding = {"账户余额": "余额", "余额": "余额", "零钱": "零钱"}.get(pay_method, pay_method)
        return self.spend(wallet, when, amount, category, counterparty, item,
                          pay_method=pay_method, funding=funding, **kwargs) is not None

    def pay(self, when: datetime, amount: Decimal, category: str, counterparty: str,
            item: str, *, channel: str | None = None, order_channel: str = "",
            kind: str = "purchase", merchant_no: str = "",
            merchant: str = "") -> Posting | None:
        amount = money(amount)
        auto_channel = channel is None
        if auto_channel:
            channel = self.choose_channel(amount, counterparty)
        # 个人转账不会直接刷信用卡；自动选到信用卡时改走微信/支付宝余额。
        # 显式指定 channel="credit" 的分期扣款不受影响。
        if auto_channel and kind == "transfer" and channel == "credit":
            channel = "wechat" if self.rng.random() < 0.5 else "alipay"
        kwargs = {"kind": kind, "order_channel": order_channel,
                  "merchant_no": merchant_no, "merchant": merchant}

        # 小荷包消费要同时出现在支付宝主账单和小荷包账单里，
        # 不能只落到小荷包账户；余额不够时退回普通支付宝付款。
        if channel == "wallet_family" and kind != "transfer":
            if self._wallet_pay("alipay", "支付宝小荷包(家庭开销)", when, amount,
                                category, counterparty, item, **kwargs):
                return self._last_post("alipay", when)
            channel = "alipay"

        # 钱包通道要先处理：alipay / wechat 同时也是账户 id，
        # 直接走账户分支会绕掉「付款方式」和信用卡联动。
        if channel == "alipay":
            # 转账/红包不能刷信用卡或花呗，只能用余额或余额宝。
            if kind == "transfer":
                for method in ("余额宝", "账户余额"):
                    if self._wallet_pay("alipay", method, when, amount, category,
                                        counterparty, item, **kwargs):
                        return self._last_post("alipay", when)
                return None
            r = self.rng.random()
            if r < 0.04 and amount <= Decimal("300") \
                    and self.qinqing_used + amount <= D.QINQING_LIMIT:
                method = "亲情卡(周母)"
            elif r < 0.58:
                method = "余额宝"
            elif r < 0.78:
                method = "花呗"
            elif r < 0.92:
                method = "招商银行信用卡(6620)"
            elif r < 0.96:
                method = "广发银行信用卡(1188)"
            else:
                method = "账户余额"
            if not self._wallet_pay("alipay", method, when, amount, category,
                                    counterparty, item, **kwargs):
                # 首次选中的产品不够时，按真实支付顺序退到余额宝/绑定信用卡；
                # 支付宝账单里仍保留这一行。
                if method != "余额宝" and self._wallet_pay(
                        "alipay", "余额宝", when, amount, category,
                        counterparty, item, **kwargs):
                    return self._last_post("alipay", when)
                if not self._wallet_pay("alipay", "招商银行信用卡(6620)", when, amount,
                                        category, counterparty, item, **kwargs):
                    return self.spend("credit", when, amount, category,
                                      counterparty, item, **kwargs)
            return self._last_post("alipay", when)

        if channel == "wechat":
            # 微信转账/红包同样不能刷信用卡，只能用零钱或零钱通。
            if kind == "transfer":
                for method in ("零钱通", "零钱"):
                    if self._wallet_pay("wechat", method, when, amount, category,
                                        counterparty, item, **kwargs):
                        return self._last_post("wechat", when)
                return None
            r = self.rng.random()
            if r < 0.05 and amount <= Decimal("200") \
                    and self.qinshu_used + amount <= D.QINSHU_LIMIT:
                method = "亲属卡(周小满)"
            elif r < 0.57:
                method = "零钱通"
            elif r < 0.68:
                method = "零钱"
            elif r < 0.92:
                method = "招商银行信用卡(6620)"
            else:
                method = "广发银行信用卡(1188)"
            if not self._wallet_pay("wechat", method, when, amount, category,
                                    counterparty, item, **kwargs):
                if method != "零钱通" and self._wallet_pay(
                        "wechat", "零钱通", when, amount, category,
                        counterparty, item, **kwargs):
                    return self._last_post("wechat", when)
                if not self._wallet_pay("wechat", "招商银行信用卡(6620)", when, amount,
                                        category, counterparty, item, **kwargs):
                    return self.spend("credit", when, amount, category,
                                      counterparty, item, **kwargs)
            return self._last_post("wechat", when)

        # 直接落到某个账户（工资卡、还款卡、信用卡、小荷包）
        if channel in D.ACCOUNTS:
            return self.spend(channel, when, amount, category, counterparty, item, **kwargs)
        raise ValueError(f"未知付款通道：{channel}")

    def _last_post(self, account: str, when: datetime) -> Posting | None:
        for p in reversed(self.postings):
            if p.account == account and p.when == when:
                return p
        return None

    def choose_channel(self, amount: Decimal, counterparty: str) -> str:
        r = self.rng.random()
        if counterparty in {"美团", "京东", "淘宝", "天猫"}:
            return "wechat" if counterparty == "京东" else "alipay"
        if amount >= Decimal("400") and r < 0.45:
            return "credit2" if r < 0.08 else "credit"
        if counterparty in {"惠民生鲜", "家家乐超市", "美好生活超市", "邻里生鲜",
                            "育才文具店", "校车服务", "少年宫"} and r < 0.35:
            return "wallet_family"
        return "alipay" if r < 0.62 else "wechat"

    # ── 每日推进 ────────────────────────────────────────────
    def run(self) -> Dataset:
        d = self.start
        while d <= self.end:
            intents: list[tuple[datetime, str, dict]] = []
            self._plan_funding(d, intents)
            self._plan_products(d, intents)
            self._plan_fixed(d, intents)
            self._plan_travel(d, intents)
            self._plan_daily(d, intents)
            for when, kind, payload in sorted(intents, key=lambda x: x[0]):
                if kind == "transfer":
                    self.transfer(when, **payload)
                elif kind == "income":
                    self.income(**payload)
                elif kind == "spend":
                    self.pay(**payload)
                elif kind == "closed":
                    self._post_closed(**payload)
                elif kind == "move":
                    self._move(**payload)
                elif kind == "accrue":
                    self._accrue(**payload)
                elif kind == "bill_huabei":
                    self.huabei_bill = self.huabei_used
                elif kind == "repay_huabei":
                    self._repay_huabei(**payload)
                elif kind == "yield_wechat":
                    self._yield_wechat(**payload)
                elif kind == "wechat_withdraw":
                    self._wechat_withdraw(**payload)
            self.invest.step(d, self)
            d += timedelta(days=1)

        # 投资账户的期末余额取市值，而不是开户时的本金。
        self.bal["fund"] = self.invest.fund_value()
        self.bal["broker"] = self.invest.broker_value()
        self.bal["wealth"] = self.invest.wealth
        for account in ("alipay", "wechat"):
            self._sync_wallet(account)
        return Dataset(
            postings=self.postings, balances=self.bal, credit_used=self.credit_used,
            credit_used_by=dict(self.credit_used_by),
            snapshots=self.invest.snapshots, fund_rows=self.invest.fund_rows,
            broker_rows=self.invest.broker_rows, wealth_rows=self.invest.wealth_rows,
            start=self.start, end=self.end, seed=self.seed,
            wallet_products=dict(self.products), huabei_used=self.huabei_used,
            wechat_yield_rows=self.wechat_yield_rows,
        )

    def _post_closed(self, **kwargs) -> None:
        """被取消的订单：出现在导出里，但不影响余额。"""
        when = kwargs.pop("when")
        counterparty = kwargs.pop("counterparty")
        p = Posting(account=kwargs.pop("account"), when=when, direction="支出",
                    amount=money(kwargs.pop("amount")), status="交易关闭",
                    category=kwargs.pop("category"), counterparty=counterparty,
                    counterparty_account=kwargs.pop("counterparty_account", "")
                    or D.party_account(counterparty),
                    item=kwargs.pop("item"), kind="closed", effect="none",
                    **kwargs)
        p.tx_no = self.order_no(p.account, when)
        self.postings.append(p)

    # ── 钱包内部划转、收益与账单 ────────────────────────────
    def _move(self, account: str, when: datetime, src: str, dst: str, amount: Decimal,
              *, item: str, counterparty: str, category: str = "投资理财",
              pay_method: str = "", platform_type: str = "",
              status: str = "交易成功", counterparty_account: str = "/",
              remark: str = "") -> Posting | None:
        """同一个平台内部的产品划转：余额→余额宝、零钱→零钱通等。"""
        amount = money(amount)
        if amount <= 0 or self._product(account, src) < amount:
            return None
        p = self._post(account, when, "中性", amount, category, counterparty, item,
                       kind="transfer", effect="none", funding=src,
                       pay_method=pay_method, platform_type=platform_type,
                       status=status, counterparty_account=counterparty_account,
                       remark=remark)
        if not p:
            return None
        self._adjust_product(account, src, -amount)
        if not self._adjust_product(account, dst, amount):
            self._adjust_product(account, src, amount)
            self.postings.remove(p)
            return None
        return p

    def _accrue(self, account: str, when: datetime, funding: str, amount: Decimal,
                *, item: str, counterparty: str, category: str = "投资理财",
                pay_method: str = "", platform_type: str = "",
                status: str = "交易成功", counterparty_account: str = "/",
                **kwargs) -> Posting | None:
        """货币基金/理财收益：账单里是「不计收支」，但产品余额确实增加。"""
        amount = money(amount)
        if amount <= 0:
            return None
        p = self._post(account, when, "中性", amount, category, counterparty, item,
                       kind="income", effect="none", funding=funding,
                       pay_method=pay_method, platform_type=platform_type,
                       status=status, counterparty_account=counterparty_account,
                       **kwargs)
        if not p:
            return None
        self._adjust_product(account, funding, amount)
        return p

    def _repay_huabei(self, when: datetime, amount: Decimal) -> Posting | None:
        amount = min(money(amount), self.huabei_used)
        if amount <= 0:
            return None
        source = next((name for name in ("余额宝", "余额")
                       if self._product("alipay", name) >= amount), "")
        if not source:
            return None
        pay = "余额宝" if source == "余额宝" else "账户余额"
        p = self._post("alipay", when, "中性", amount, "信用借还", "花呗",
                       f"花呗主动还款-{when:%Y年%m月}账单", kind="transfer",
                       effect="none", funding=source, pay_method=pay,
                       status="还款成功", counterparty_account="/")
        if not p:
            return None
        self._adjust_product("alipay", source, -amount)
        self.huabei_used = max(Decimal("0.00"), self.huabei_used - amount)
        return p

    def _yield_wechat(self, when: datetime, funding: str, amount: Decimal) -> None:
        """微信支付账单不列零钱通/理财通收益，只在收益明细里体现。"""
        amount = money(amount)
        if amount <= 0 or not self._adjust_product("wechat", funding, amount):
            return
        self.wechat_yield_rows.append({
            "date": when.date().isoformat(),
            "product": funding,
            "amount": amount,
            "balance": self._product("wechat", funding),
        })

    def _wechat_withdraw(self, when: datetime, amount: Decimal) -> bool:
        amount = money(amount)
        if self._product("wechat", "零钱") < amount:
            return False
        pair = str(self.rng.randint(10 ** 9, 10 ** 10 - 1))
        p = self._post("wechat", when, "中性", amount, "转账红包",
                       "招商银行储蓄卡(0371)", "/", kind="transfer",
                       effect="out", funding="零钱", pay_method="零钱",
                       platform_type="零钱提现", status="提现已到账",
                       counterparty_account="/", merchant_no=pair)
        if not p:
            return False
        self.income("salary", when + timedelta(seconds=45), amount, "转账红包",
                    "微信零钱", "零钱提现", kind="transfer", merchant_no=pair)
        return True

    def _plan_products(self, d: date, intents: list) -> None:
        """余额宝/零钱通收益、花呗账单、平台内部划转。"""
        def at(h: int, m: int = 0) -> datetime:
            return datetime.combine(d, time(h, m, self.rng.randint(0, 59)))

        # 花呗每月 1 日出账、10 日还款；亲情卡/亲属卡额度每月重置。
        if d.day == 1:
            self.huabei_bill = self.huabei_used
            self.qinqing_used = Decimal("0.00")
            self.qinshu_used = Decimal("0.00")

        # 余额宝收益每天入账，账单口径为「不计收支」。
        yuebao = self._product("alipay", "余额宝")
        yield_amount = money(yuebao * D.WALLET_PRODUCTS["alipay:余额宝"].annual_yield
                             / Decimal("365"))
        if yield_amount >= Decimal("0.01"):
            ts = at(4, 5)
            intents.append((ts, "accrue", {
                "account": "alipay", "when": ts, "funding": "余额宝",
                "amount": yield_amount, "item": f"余额宝-{d:%Y.%m.%d}-收益发放",
                "counterparty": D.YUEBAO_FUND, "pay_method": "余额宝",
                "counterparty_account": "/",
            }))

        # 微信零钱通收益不进入支付账单，只在单独的收益明细里。
        change_yield = money(self._product("wechat", "零钱通")
                             * D.WALLET_PRODUCTS["wechat:零钱通"].annual_yield
                             / Decimal("365"))
        if change_yield >= Decimal("0.01"):
            intents.append((at(4, 10), "yield_wechat", {
                "when": datetime.combine(d, time(4, 10)), "funding": "零钱通",
                "amount": change_yield,
            }))

        # 理财按月结息。
        if d.day == 1:
            amount = money(self._product("alipay", "理财")
                           * D.WALLET_PRODUCTS["alipay:理财"].annual_yield
                           / Decimal("12"))
            if amount >= Decimal("0.01"):
                ts = at(3, 0)
                intents.append((ts, "accrue", {
                    "account": "alipay", "when": ts, "funding": "理财",
                    "amount": amount, "item": "理财-收益发放",
                    "counterparty": D.ALIPAY_WEALTH, "pay_method": "",
                    "counterparty_account": "/",
                }))
            amount = money(self._product("wechat", "理财通")
                           * D.WALLET_PRODUCTS["wechat:理财通"].annual_yield
                           / Decimal("12"))
            if amount >= Decimal("0.01"):
                intents.append((at(3, 5), "yield_wechat", {
                    "when": datetime.combine(d, time(3, 5)), "funding": "理财通",
                    "amount": amount,
                }))

        if d.day == 10 and self.huabei_bill > 0:
            ts = at(9, 25)
            intents.append((ts, "repay_huabei", {
                "when": ts, "amount": self.huabei_bill,
            }))

        if d.day == 11:
            available = self._product("alipay", "余额") - Decimal("1500.00")
            amount = min(available, Decimal("10000.00"))
            if amount >= Decimal("100.00"):
                intents.append((at(7, 0), "move", {
                    "account": "alipay", "when": at(7, 0), "src": "余额",
                    "dst": "余额宝", "amount": amount, "item": "余额宝-自动转入",
                    "counterparty": D.YUEBAO_COMPANY, "pay_method": "",
                    "counterparty_account": "/",
                }))

        if d.day == 15 \
                and self._product("alipay", "余额宝") - Decimal("1000.00") >= Decimal("15000.00"):
            intents.append((at(10, 0), "move", {
                "account": "alipay", "when": at(10, 0), "src": "余额宝",
                "dst": "理财", "amount": Decimal("1000.00"), "item": "理财产品申购",
                "counterparty": D.ALIPAY_WEALTH, "pay_method": "余额宝",
                "counterparty_account": "/",
            }))

        if d.month in (3, 6, 9, 12) and d.day == 20 \
                and self._product("alipay", "理财") >= Decimal("1500.00"):
            intents.append((at(20, 0), "move", {
                "account": "alipay", "when": at(20, 0), "src": "理财",
                "dst": "余额宝", "amount": Decimal("1500.00"), "item": "理财产品赎回",
                "counterparty": D.ALIPAY_WEALTH, "pay_method": "",
                "counterparty_account": "/",
            }))

        if d.day == 25 and self._product("alipay", "余额") < Decimal("800.00") \
                and self._product("alipay", "余额宝") > Decimal("2000.00"):
            intents.append((at(8, 0), "move", {
                "account": "alipay", "when": at(8, 0), "src": "余额宝",
                "dst": "余额", "amount": Decimal("1500.00"),
                "item": "余额宝-转出到余额", "counterparty": "余额宝",
                "pay_method": "账户余额", "counterparty_account": "jij***@southernfund.com",
            }))

        if d.day == 12 and self._product("wechat", "零钱") > Decimal("3000.00"):
            intents.append((at(7, 0), "move", {
                "account": "wechat", "when": at(7, 0), "src": "零钱",
                "dst": "零钱通", "amount": Decimal("3000.00"),
                "item": "转入零钱通-来自零钱", "counterparty": "/", "pay_method": "零钱",
                "platform_type": "转入零钱通-来自零钱", "status": "支付成功",
                "counterparty_account": "/",
            }))

        if d.day == 16 \
                and self._product("wechat", "零钱通") - Decimal("800.00") >= Decimal("5000.00"):
            intents.append((at(10, 0), "move", {
                "account": "wechat", "when": at(10, 0), "src": "零钱通",
                "dst": "理财通", "amount": Decimal("800.00"), "item": "活期＋",
                "counterparty": "理财通", "pay_method": "零钱通",
                "platform_type": "购买理财通", "status": "支付成功",
                "counterparty_account": "/",
            }))

        # 每月把理财赎回到可付款产品，保持余额宝/零钱通的最低水位。
        if d.day == 21:
            gap = Decimal("12000.00") - self._product("alipay", "余额宝")
            spare = self._product("alipay", "理财") - Decimal("5000.00")
            gap = min(gap, spare)
            if gap >= Decimal("100.00"):
                intents.append((at(9, 30), "move", {
                    "account": "alipay", "when": at(9, 30), "src": "理财",
                    "dst": "余额宝", "amount": gap, "item": "理财产品赎回",
                    "counterparty": D.ALIPAY_WEALTH, "pay_method": "",
                    "counterparty_account": "/",
                }))
            gap = Decimal("6000.00") - self._product("wechat", "零钱通")
            spare = self._product("wechat", "理财通") - Decimal("5000.00")
            gap = min(gap, spare)
            if gap >= Decimal("100.00"):
                intents.append((at(9, 35), "move", {
                    "account": "wechat", "when": at(9, 35), "src": "理财通",
                    "dst": "零钱通", "amount": gap, "item": "活期＋",
                    "counterparty": "理财通", "pay_method": "零钱通",
                    "platform_type": "理财通赎回", "status": "已到账",
                    "counterparty_account": "/",
                }))

        if d.month in (3, 6, 9, 12) and d.day == 20 \
                and self._product("wechat", "理财通") >= Decimal("1000.00"):
            intents.append((at(14, 0), "move", {
                "account": "wechat", "when": at(14, 0), "src": "理财通",
                "dst": "零钱通", "amount": Decimal("1000.00"), "item": "活期＋",
                "counterparty": "理财通", "pay_method": "零钱通",
                "platform_type": "理财通赎回", "status": "已到账",
                "counterparty_account": "/",
            }))

        if d.day == 18 and self._product("wechat", "零钱") < Decimal("400.00") \
                and self._product("wechat", "零钱通") > Decimal("1000.00"):
            intents.append((at(18, 0), "move", {
                "account": "wechat", "when": at(18, 0), "src": "零钱通",
                "dst": "零钱", "amount": Decimal("800.00"), "item": "/",
                "counterparty": "/", "pay_method": "零钱通",
                "platform_type": "零钱通转出-到零钱", "status": "资金已到账",
                "counterparty_account": "/",
            }))

        if d.month in (3, 6, 9, 12) and d.day == 22 \
                and self._product("wechat", "零钱") >= Decimal("500.00"):
            intents.append((at(16, 0), "wechat_withdraw", {
                "when": at(16, 0), "amount": Decimal("500.00"),
            }))

    def _plan_funding(self, d: date, intents: list) -> None:
        """钱包低于水位就补钱；补款本身是真实转账，不是凭空造余额。"""
        if d.weekday() == 6:
            return
        when = datetime.combine(d, time(7, 45))
        for wallet, floor in D.WALLET_FLOOR.items():
            if wallet.startswith("wallet_"):
                if self.bal[wallet] >= floor:
                    continue
            elif self._payable(wallet) >= floor:
                continue
            src = "alipay" if wallet.startswith("wallet_") else "salary"
            intents.append((when, "transfer", {
                "src": src, "dst": wallet, "amount": D.WALLET_TOPUP[wallet],
                "item": "转账", "counterparty": D.ACCOUNTS[wallet].name,
            }))
            when += timedelta(seconds=30)

    def _plan_fixed(self, d: date, intents: list) -> None:
        """合约层：按年/半年/季/月/周固定触发的收支。"""
        def at(h: int, m: int = 0) -> datetime:
            return datetime.combine(d, time(h, m, self.rng.randint(0, 59)))

        # ── 每年 ──────────────────────────────────────────────
        if d.month == 1 and d.day == 20:
            ts = at(10, 20)
            intents.append((ts, "income", {
                "account": "salary", "when": ts, "amount": D.BONUS,
                "category": "收入", "counterparty": "启明科技有限公司",
                "item": "年终奖金", "kind": "income",
            }))

        if d.month == 1 and d.day == 22:
            ts = at(15, 0)
            for amount, item, channel, category in (
                (money(self.rng.uniform(3000, 4500)), "春节年货", "alipay", "日用百货"),
                (money(self.rng.uniform(4000, 6000)), "春节红包", "wechat", "转账红包"),
                (money(self.rng.uniform(1500, 2500)), "春节礼品", "alipay", "文化休闲"),
            ):
                intents.append((ts, "spend", {
                    "when": ts, "amount": amount, "category": category,
                    "counterparty": "春节年货" if "年货" in item else
                                    ("亲友红包" if "红包" in item else "春节礼品"),
                    "item": item, "channel": channel,
                    "kind": "transfer" if "红包" in item else "purchase",
                }))
                ts += timedelta(minutes=7)

        # 4 月调薪 5%，只影响之后的工资；年终奖仍按 3 个月基数发。
        if d.month == D.RAISE_MONTH and d.day == 10:
            self.salary_amount = money(self.salary_amount * D.RAISE_RATE)

        if d.month == 4 and d.day == 15:
            ts = at(11, 20)
            intents.append((ts, "income", {
                "account": "salary", "when": ts,
                "amount": money(self.rng.uniform(800, 2600)),
                "category": "收入", "counterparty": "国家税务总局",
                "item": "个税汇算退税", "kind": "income",
            }))

        # 半年：4/10 月车辆保养
        if d.month in (4, 10) and d.day == 15:
            ts = at(14, 10)
            intents.append((ts, "spend", {
                "when": ts, "amount": money(self.rng.uniform(1200, 2000)),
                "category": "汽车", "counterparty": "广州鹏辉汽车服务",
                "item": "车辆保养", "channel": "credit2",
            }))

        # 618 / 双 11 大额采购
        if d.month == 6 and d.day == 18:
            ts = at(20, 0)
            intents.append((ts, "spend", {
                "when": ts, "amount": money(self.rng.uniform(6000, 8000)),
                "category": "服饰装扮", "counterparty": "天猫",
                "item": "618 大促购物", "channel": "credit",
            }))
        if d.month == 11 and d.day == 11:
            ts = at(20, 0)
            intents.append((ts, "spend", {
                "when": ts, "amount": money(self.rng.uniform(8000, 11000)),
                "category": "服饰装扮", "counterparty": "京东",
                "item": "双 11 大促购物", "channel": "credit",
            }))

        # 年缴保险
        for month, day, item, amount, counterparty in D.INSURANCE_POLICIES:
            if d.month == month and d.day == day:
                ts = at(11, 0)
                intents.append((ts, "spend", {
                    "when": ts, "amount": amount, "category": "保险",
                    "counterparty": counterparty, "item": item,
                    "channel": "credit",
                }))

        # ── 每季度 ────────────────────────────────────────────
        if d.month in (1, 4, 7, 10) and d.day == 5:
            ts = at(10, 20)
            intents.append((ts, "spend", {
                "when": ts, "amount": D.PROPERTY_FEE, "category": "房租物业",
                "counterparty": "锦园物业", "item": "物业费", "channel": "wallet_family",
            }))
            ts += timedelta(minutes=5)
            intents.append((ts, "spend", {
                "when": ts, "amount": D.PARKING_FEE, "category": "交通出行",
                "counterparty": "锦园物业", "item": "停车位租金", "channel": "alipay",
            }))

        if d.month in (1, 4, 7, 10) and d.day == 10:
            ts = at(13, 0)
            intents.append((ts, "spend", {
                "when": ts, "amount": D.TUITION, "category": "教育培训",
                "counterparty": "育才实验学校", "item": "学杂费",
                "channel": "salary", "kind": "transfer",
            }))

        # ── 每月 ──────────────────────────────────────────────
        if d.day == 5:
            # 主卡账单日；当天及之后的消费进下期账单。
            self.bills["credit"] = self.credit_used_by["credit"]

        if d.day == 8:
            ts = at(9, 0)
            intents.append((ts, "spend", {
                "when": ts, "amount": D.CAR_LOAN,
                "category": "交通出行", "counterparty": "招商银行汽车金融",
                "item": "汽车贷款月供", "channel": "salary", "kind": "transfer",
            }))

        if d.day == 10:
            # 房贷卡提前备款，15 日扣款。
            ts = at(12, 50)
            intents.append((ts, "transfer", {
                "src": "salary", "dst": "mortgage_card", "amount": D.MORTGAGE,
                "item": "跨行转账", "counterparty": "工商银行",
            }))
            ts = at(10, 5)
            intents.append((ts, "income", {
                "account": "salary", "when": ts, "amount": self.salary_amount,
                "category": "收入", "counterparty": "启明科技有限公司",
                "item": "工资", "kind": "income",
            }))
            # 配偶每月转入家庭小荷包，体现共同承担。
            ts = at(10, 40)
            intents.append((ts, "income", {
                "account": "wallet_family", "when": ts, "amount": D.SPOUSE_TRANSFER,
                "category": "转账红包", "counterparty": "林知夏",
                "item": "转入小荷包", "kind": "transfer",
            }))
            # 发薪后向钱包、生活缴费卡分散。
            ts = at(12, 30)
            for dst, amount, item in (
                ("alipay", Decimal("2300.00"), "转账"),
                ("wechat", Decimal("1300.00"), "转账"),
                ("living_card", Decimal("1000.00"), "跨行转账"),
            ):
                intents.append((ts, "transfer", {
                    "src": "salary", "dst": dst, "amount": amount,
                    "item": item, "counterparty": D.ACCOUNTS[dst].name,
                }))
                ts += timedelta(seconds=40)
            for dst, amount in D.WALLET_ALLOWANCE.items():
                intents.append((ts, "transfer", {
                    "src": "salary", "dst": dst, "amount": amount,
                    "item": "转入小荷包", "counterparty": D.ACCOUNTS[dst].name,
                }))
                ts += timedelta(seconds=40)
            # 广发卡账单日。
            self.bills["credit2"] = self.credit_used_by["credit2"]

        if d.day == 11:
            # 家庭小荷包承担一部分日常开销，月初把预算转回支付平台。
            ts = at(9, 20)
            for dst, amount in (("alipay", Decimal("3000.00")),
                                ("wechat", Decimal("1500.00"))):
                intents.append((ts, "transfer", {
                    "src": "wallet_family", "dst": dst, "amount": amount,
                    "item": "家庭预算转入", "counterparty": D.ACCOUNTS[dst].name,
                }))
                ts += timedelta(seconds=40)

        if d.day == 12:
            ts = at(9, 0)
            intents.append((ts, "spend", {
                "when": ts, "amount": D.CONSUMER_LOAN_MONTHLY,
                "category": "投资理财", "counterparty": "招联消费金融",
                "item": "消费贷分期扣款", "channel": "salary", "kind": "transfer",
            }))
            ts += timedelta(minutes=2)
            intents.append((ts, "spend", {
                "when": ts, "amount": D.CREDIT_LOAN_MONTHLY,
                "category": "投资理财", "counterparty": "招商银行",
                "item": "信用贷分期扣款", "channel": "salary", "kind": "transfer",
            }))
            ts += timedelta(minutes=2)
            intents.append((ts, "spend", {
                "when": ts, "amount": D.CREDIT_INSTALLMENT_MONTHLY,
                "category": "投资理财", "counterparty": "招商银行信用卡",
                "item": "信用卡分期还款", "channel": "credit", "kind": "transfer",
            }))

        if d.day == 15:
            ts = at(9, 30)
            intents.append((ts, "spend", {
                "when": ts, "amount": D.MORTGAGE, "category": "房租物业",
                "counterparty": "中国工商银行", "item": "个人住房贷款还款",
                "channel": "mortgage_card", "kind": "transfer",
            }))
            ts = at(9, 40)
            for counterparty, item, low, high in (
                ("广东电网", "电费", 300, 420),
                ("广州自来水", "水费", 55, 95),
                ("广州燃气", "燃气费", 70, 130),
                ("中国电信", "宽带及话费", 220, 320),
            ):
                intents.append((ts, "spend", {
                    "when": ts, "amount": money(self.rng.uniform(low, high)),
                    "category": "生活缴费", "counterparty": counterparty,
                    "item": item, "channel": "living_card",
                }))
                ts += timedelta(seconds=35)

        if d.day == 23 and self.bills["credit"] > 0:
            amount = min(self.bills["credit"], self.credit_used_by["credit"])
            # 工资卡不够就保留账单，下期继续还；不能把未还账单抹掉。
            if amount > 0 and self.bal["salary"] >= amount:
                ts = at(20, 40)
                intents.append((ts, "transfer", {
                    "src": "salary", "dst": "credit", "amount": amount,
                    "item": "信用卡还款", "counterparty": "招商银行信用卡",
                }))
                self.bills["credit"] = Decimal("0.00")

        if d.day == 28 and self.bills["credit2"] > 0:
            amount = min(self.bills["credit2"], self.credit_used_by["credit2"])
            if amount > 0 and self.bal["salary"] >= amount:
                ts = at(20, 30)
                intents.append((ts, "transfer", {
                    "src": "salary", "dst": "credit2", "amount": amount,
                    "item": "信用卡还款", "counterparty": "广发银行信用卡",
                }))
                self.bills["credit2"] = Decimal("0.00")

        # 订阅：每月 1—4 号各扣一笔
        if d.day <= len(D.SUBSCRIPTIONS):
            name, item, amount = D.SUBSCRIPTIONS[d.day - 1]
            ts = at(12, 10)
            intents.append((ts, "spend", {
                "when": ts, "amount": amount, "category": "文化休闲",
                "counterparty": name, "item": item, "channel": "alipay",
            }))

    def _plan_travel(self, d: date, intents: list) -> None:
        trips = ((5, 1, 5), (7, 20, 26), (10, 2, 6))
        for month, begin, finish in trips:
            if d.month != month or not (begin <= d.day <= finish):
                continue
            city = D.TRAVEL_CITIES[(month + d.day) % len(D.TRAVEL_CITIES)]
            at = lambda h: datetime.combine(d, time(h, self.rng.randint(0, 59)))
            if d.day == begin:
                intents.append((at(8), "transfer", {
                    "src": "alipay", "dst": "wallet_travel", "amount": Decimal("1500.00"),
                    "item": "转入小荷包", "counterparty": "小荷包·旅行基金",
                }))
                intents.append((at(9), "spend", {
                    "when": at(9), "amount": money(self.rng.uniform(480, 620) * 2),
                    "category": "交通出行", "counterparty": "中国铁路",
                    "item": f"高铁票-{city}", "channel": "credit2",
                }))
            intents.append((at(12), "spend", {
                "when": at(12), "amount": money(self.rng.uniform(380, 860)),
                "category": "文化休闲", "counterparty": f"{city}栖迟酒店",
                "item": "酒店住宿", "channel": "credit2",
            }))
            intents.append((at(13), "spend", {
                "when": at(13), "amount": money(self.rng.uniform(80, 260)),
                "category": "餐饮美食", "counterparty": f"{city}老街小馆",
                "item": "旅游餐饮", "channel": "wallet_travel",
            }))
            intents.append((at(15), "spend", {
                "when": at(15), "amount": money(self.rng.uniform(60, 320)),
                "category": "文化休闲", "counterparty": f"{city}景区",
                "item": "景点门票", "channel": "alipay",
            }))

    def _plan_daily(self, d: date, intents: list) -> None:
        rng = self.rng
        weekday = d.weekday() < 5
        season = {1: 1.30, 6: 1.20, 7: 1.15, 8: 1.15, 11: 1.25,
                  12: 1.10}.get(d.month, 1.0)
        if not weekday:
            season *= 1.10

        def maybe(p: float) -> bool:
            return rng.random() < p

        def at(h: int, h2: int | None = None) -> datetime:
            hour = h if h2 is None else rng.randint(h, h2)
            return datetime.combine(d, time(hour, rng.randint(0, 59), rng.randint(0, 59)))

        def pick(pool):
            return rng.choice(pool)

        def spend(h: int, h2: int | None, amount: float, category: str,
                  counterparty: str, item: str, **kw) -> None:
            t = at(h, h2)
            if category in {"餐饮美食", "日用百货", "服饰装扮", "文化休闲"} \
                    and amount >= 15:
                amount *= season
            intents.append((t, "spend", {
                "when": t, "amount": money(amount), "category": category,
                "counterparty": counterparty, "item": item, **kw,
            }))

        if maybe(0.85 if weekday else 0.55):
            spend(6, 8, rng.uniform(5.5, 16), "餐饮美食",
                  pick(D.BREAKFAST), pick(D.BREAKFAST_ITEMS))
        if maybe(0.55 if weekday else 0.35):
            spend(9, 10, rng.uniform(12, 32), "餐饮美食",
                  pick(D.COFFEE), pick(D.COFFEE_ITEMS))
        if maybe(0.2):
            spend(15, 16, rng.uniform(9, 26), "餐饮美食",
                  pick(D.COFFEE), "下午茶")
        if maybe(0.95 if weekday else 0.7):
            spend(11, 12, rng.uniform(16, 55), "餐饮美食",
                  pick(D.LUNCH), pick(D.LUNCH_ITEMS))
        if maybe(0.72):
            spend(14, 17, rng.uniform(4, 28), "日用百货",
                  pick(D.GROCERY), pick(("零食饮料", "水果拼盘", "酸奶", "面包")))
        if maybe(0.5):
            spend(7, 22, rng.uniform(3, 25), "日用百货",
                  pick(("青禾便利店", "每日鲜便利店")),
                  pick(("早餐补给", "饮料", "日用品", "电池/纸巾")))
        if maybe(0.86):
            spend(17, 20, rng.uniform(22, 88), "餐饮美食",
                  pick(D.DINNER), pick(D.DINNER_ITEMS))
        if weekday:
            for h in (8, 18):
                spend(h, h, rng.uniform(3, 6), "交通出行", "手机地铁乘车码", "地铁乘车")
            if maybe(0.18):
                spend(8, 9, rng.uniform(1.5, 3), "交通出行", "公交乘车码", "公交通勤")
            if maybe(0.1):
                spend(18, 19, rng.uniform(1.5, 3), "交通出行", "哈啰单车", "共享单车")
            if maybe(0.2):
                spend(18, 19, rng.uniform(1.5, 3), "交通出行", "公交乘车码", "公交通勤")
        if maybe(0.12):
            spend(21, 23, rng.uniform(16, 62), "交通出行", "滴滴出行", "快车-科技园")
        if maybe(0.3 if weekday else 0.55):
            spend(17, 20, rng.uniform(28, 190), "日用百货",
                  pick(D.GROCERY), pick(D.GROCERY_ITEMS))
        if (not weekday) and maybe(0.30):
            spend(18, 20, rng.uniform(120, 420), "餐饮美食",
                  pick(D.DINING_OUT), pick(D.DINING_OUT_ITEMS))

        # 网购：淘宝/天猫/京东，每单还会在电商平台留一条订单
        for _ in range(rng.choices((0, 1, 2, 3), weights=(40, 35, 18, 7))[0]):
            platform = rng.choices(("taobao", "tmall", "jd"), weights=(45, 30, 25))[0]
            counterparty = {"taobao": "淘宝", "tmall": "天猫", "jd": "京东"}[platform]
            item = pick(D.SHOP_ITEMS)
            category, stores = D.SHOP_CATALOG[platform][item]
            spend(10, 22, rng.uniform(9, 480), category, counterparty,
                  item, order_channel=platform,
                  merchant=pick(stores),
                  merchant_no=str(rng.randint(10 ** 15, 10 ** 16 - 1)))

        # 外卖：美团
        for _ in range(rng.choices((0, 1, 2), weights=(30, 42, 28))[0]):
            spend(11, 12, rng.uniform(18, 72), "餐饮美食", "美团",
                  pick(D.MEITUAN_ITEMS), order_channel="meituan",
                  merchant=pick(D.MEITUAN_STORES),
                  merchant_no=str(rng.randint(10 ** 15, 10 ** 16 - 1)))
        if maybe(0.38):
            spend(16, 19, rng.uniform(15, 280), "教育培训",
                  pick(("育才文具店", "校车服务", "少年宫")),
                  pick(("文具教辅", "校车月费", "兴趣班材料")))
        if maybe(0.06):
            spend(9, 20, rng.uniform(25, 620), "医疗健康",
                  pick(D.MEDICAL), pick(D.MEDICAL_ITEMS))
        if maybe(0.033):
            spend(10, 19, rng.uniform(45, 128), "生活服务",
                  pick(D.HAIRCUT), pick(D.HAIRCUT_ITEMS))
        if maybe(0.1):
            spend(12, 21, rng.uniform(50, 520), "转账红包", pick(D.WECHAT_FRIENDS),
                  "转账备注:微信转账", kind="transfer")
        if maybe(0.1):
            self._maybe_refund(d, intents, at)

        if maybe(0.04):
            t = at(10, 22)
            intents.append((t, "closed", {
                "when": t, "account": "alipay", "amount": money(rng.uniform(20, 300)),
                "category": "服饰装扮", "counterparty": "淘宝", "item": "订单已取消",
                "order_channel": "taobao",
            }))

    def _maybe_refund(self, d: date, intents: list, at) -> None:
        start = datetime.combine(d - timedelta(days=6), time(0, 0))
        stop = datetime.combine(d, time(23, 59))
        candidates = [p for p in self.postings
                      if p.kind == "purchase" and p.account in {"alipay", "wechat"}
                      and p.effect != "none" and start <= p.when <= stop]
        if not candidates:
            return
        orig = self.rng.choice(candidates)
        when = at(9, 21)
        intents.append((when, "income", {
            "account": orig.account, "when": when, "amount": orig.amount,
            "category": orig.category, "counterparty": orig.counterparty,
            "item": f"退款-{orig.item}", "kind": "refund",
            "pay_method": orig.pay_method, "status": "退款成功",
        }))


class Investments:
    """基金 / 券商 / 银行理财。只生成投资平台自己的流水与月末资产快照。"""

    def __init__(self, rng: random.Random, start: date, end: date):
        self.rng = rng
        self.start = start
        self.end = end
        self.funds = []
        per = money(Decimal("86000") / len(D.FUNDS))
        for code, name in D.FUNDS:
            nav = money(rng.uniform(1.2, 3.6))
            shares = money(per / nav)
            self.funds.append({
                "code": code, "name": name, "nav": nav, "shares": shares,
                "cost": money(shares * nav), "bias": rng.uniform(-0.004, 0.006),
            })
        self.fund_principal = Decimal("86000.00")

        # 券商账户共 5.4 万：先用其中约 4.2 万建仓，剩约 1.2 万现金。
        # 持仓集中在三只低价股，保证每次 100 股整数倍的买卖都买得起。
        self.broker_cash = Decimal("54000.00")
        self.positions = {}
        for code, name, base in D.BROKER_STOCKS:
            price = money(base * Decimal(str(rng.uniform(0.97, 1.03))))
            self.positions[code] = {"name": name, "qty": Decimal("0"),
                                    "cost": Decimal("0.00"), "price": price}
        for code in sorted(self.positions,
                           key=lambda c: self.positions[c]["price"])[:3]:
            pos = self.positions[code]
            qty = Decimal(int(Decimal("14000") / pos["price"] // 100) * 100)
            if qty <= 0:
                continue
            pos["qty"] = qty
            pos["cost"] = money(qty * pos["price"])
            self.broker_cash -= pos["cost"]
        self.broker_principal = Decimal("54000.00")

        self.wealth = Decimal("120000.00")
        self.wealth_principal = Decimal("120000.00")
        self.fund_rows: list[dict] = []
        self.broker_rows: list[dict] = []
        self.wealth_rows: list[dict] = []
        self.snapshots: list[dict] = []
        self._last_month: date | None = None

    def step(self, d: date, ledger: Ledger) -> None:
        for f in self.funds:
            drift = f["bias"] / 30 + self.rng.gauss(0, 0.006)
            f["nav"] = max(Decimal("0.5"), money(f["nav"] * Decimal(str(1 + drift))))
        for pos in self.positions.values():
            drift = self.rng.gauss(0.0002, 0.015)
            pos["price"] = max(Decimal("1"), money(pos["price"] * Decimal(str(1 + drift))))

        if d.day == 15:
            self._sip(d, ledger)
        if d.month in (3, 6, 9, 12) and d.day == 25:
            self._redeem(d, ledger)
        if d.day in (4, 11, 18, 24) and self.rng.random() < 0.8:
            self._trade(d)

        if d.day == 1:
            if self._last_month is not None:
                self._wealth_interest(self._last_month, d)
            self._last_month = d

        if d == _month_end(d) or d == self.end:
            self._snapshot(d)

    def _sip(self, d: date, ledger: Ledger) -> None:
        amount = D.FUND_SIP
        when = datetime.combine(d, time(9, 40))
        if not ledger.spend("salary", when, amount, "投资理财", "天天基金",
                            "基金定投扣款", channel="salary", kind="investment"):
            return
        picks = self.rng.sample(self.funds, 2)
        left = amount
        for i, f in enumerate(picks):
            part = left if i == len(picks) - 1 else money(amount / 2)
            left -= part
            fee = money(part * Decimal("0.0015"))
            shares = money((part - fee) / f["nav"])
            f["shares"] += shares
            f["cost"] += part
            self.fund_principal += part
            self.fund_rows.append({
                "date": d.isoformat(), "code": f["code"], "name": f["name"],
                "business": "申购确认", "amount": part, "shares": shares,
                "nav": f["nav"], "fee": fee,
            })

    def _redeem(self, d: date, ledger: Ledger) -> None:
        amount = Decimal("1500.00")
        f = max(self.funds, key=lambda x: x["shares"] * x["nav"])
        if f["shares"] * f["nav"] < amount:
            return
        fee = money(amount * Decimal("0.005"))
        net = money(amount - fee)
        shares = money(net / f["nav"])
        avg_cost = f["cost"] / f["shares"] if f["shares"] else Decimal("0")
        f["shares"] -= shares
        f["cost"] = max(Decimal("0.00"), f["cost"] - money(avg_cost * shares))
        when = datetime.combine(d, time(14, 30))
        if not ledger.income("salary", when, net, "投资理财", "天天基金",
                             "基金赎回款", kind="investment"):
            return
        self.fund_principal = max(Decimal("0.00"), self.fund_principal - amount)
        self.fund_rows.append({
            "date": d.isoformat(), "code": f["code"], "name": f["name"],
            "business": "赎回确认", "amount": amount, "shares": shares,
            "nav": f["nav"], "fee": fee,
        })

    def _trade(self, d: date) -> None:
        code = self.rng.choice(list(self.positions))
        pos = self.positions[code]
        price = max(Decimal("1"), money(pos["price"] * Decimal(str(1 + self.rng.gauss(0, 0.01)))))
        pos["price"] = price

        # 每笔 3000~15000 元，按 100 股整数倍取整；买不起就跳过。
        target = money(self.rng.uniform(3000, 15000))
        lots = int(target / price // 100)
        side = "买入" if self.rng.random() < 0.6 else "卖出"

        if side == "买入":
            lots = min(lots, int(self.broker_cash // (price * 100)))
            if lots <= 0:
                return
            qty = Decimal(lots * 100)
            gross = money(qty * price)
            fee = money(max(Decimal("5"), gross * Decimal("0.00025")))
            if gross + fee > self.broker_cash:
                return
            self.broker_cash -= gross + fee
            pos["qty"] += qty
            pos["cost"] += gross + fee
            pnl = None
        else:
            lots = min(lots, int(pos["qty"] // 100))
            if lots <= 0:
                return
            qty = Decimal(lots * 100)
            gross = money(qty * price)
            stamp = money(gross * Decimal("0.0005"))
            fee = money(max(Decimal("5"), gross * Decimal("0.00025")) + stamp)
            avg = pos["cost"] / pos["qty"] if pos["qty"] else Decimal("0")
            pos["qty"] -= qty
            pos["cost"] = max(Decimal("0.00"), pos["cost"] - money(avg * qty))
            self.broker_cash += gross - fee
            pnl = money(gross - fee - avg * qty)
        self.broker_rows.append({
            "date": d.isoformat(), "code": code, "name": pos["name"], "side": side,
            "qty": int(qty), "price": price, "amount": gross, "fee": fee,
            "pnl": "" if pnl is None else _fmt(pnl),
        })

    def _wealth_interest(self, month_first: date, today: date) -> None:
        interest = money(self.wealth * Decimal("0.0026"))
        self.wealth += interest
        self.wealth_rows.append({
            "date": today.isoformat(), "amount": interest,
            "balance": self.wealth, "note": "理财收益",
        })

    def fund_value(self) -> Decimal:
        return money(sum((f["shares"] * f["nav"] for f in self.funds), Decimal("0")))

    def broker_value(self) -> Decimal:
        return money(self.broker_cash + sum(
            (p["qty"] * p["price"] for p in self.positions.values()), Decimal("0")))

    def _snapshot(self, d: date) -> None:
        for f in self.funds:
            value = money(f["shares"] * f["nav"])
            pnl = money(value - f["cost"])
            pct = (pnl / f["cost"] * 100) if f["cost"] else Decimal("0")
            self.snapshots.append({
                "date": d.isoformat(), "account": "基金", "name": f["name"],
                "market_value": value, "cost": money(f["cost"]),
                "pnl": pnl, "pnl_pct": pct.quantize(Decimal("0.01")),
            })
        # 券商快照按账户总资产（现金 + 持仓市值）算，成本就是初始投入，期间无出入金。
        value = self.broker_value()
        cost = self.broker_principal
        self.snapshots.append({
            "date": d.isoformat(), "account": "证券", "name": "华泰证券账户",
            "market_value": value, "cost": cost,
            "pnl": money(value - cost),
            "pnl_pct": ((value - cost) / cost * 100).quantize(Decimal("0.01")),
        })
        self.snapshots.append({
            "date": d.isoformat(), "account": "理财", "name": "招行理财",
            "market_value": money(self.wealth), "cost": money(self.wealth_principal),
            "pnl": money(self.wealth - self.wealth_principal),
            "pnl_pct": ((self.wealth - self.wealth_principal) / self.wealth_principal * 100)
            .quantize(Decimal("0.01")),
        })


def generate(seed: int = 20261126, start: date | None = None,
             months: int = 12) -> Dataset:
    start = start or date(2026, 1, 1)
    # 结束日 = start + months 个月 - 1 天
    y, m = divmod(start.month - 1 + months, 12)
    end = date(start.year + y, m + 1, 1) - timedelta(days=1)
    dataset = Ledger(seed, start, end).run()
    # 答案层依赖引擎里的 Posting，放在这里做一次性标注；局部导入避免循环引用。
    from . import truth
    truth.annotate(dataset)
    return dataset
