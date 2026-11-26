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
from decimal import Decimal

from . import actors as A, data as D
from .investments import Investments
from .journal import Change, Event, Journal, ZERO, money
from .settlement import Settlement


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
    event_id: str = ""
    secondary: bool = False
    refunded: Decimal = ZERO
    refund_full: bool = False


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
    journal: Journal

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
            if p.order_channel and p.kind in {"purchase", "closed"}:
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


class Ledger(Settlement):
    def __init__(self, seed: int, start: date, end: date):
        self.seed = seed
        self.rng = random.Random(seed)
        self.start = start
        self.end = end
        self.postings: list[Posting] = []
        self.journal = Journal()
        A.setup(self.journal)
        self.journal.state(f"tax:{start.year - 1}:refundable_withholding", money("3000"))
        self.counter = 0
        self.statement_at: dict[str, datetime] = {}
        self.sales: dict[str, tuple[Event, Posting, str]] = {}
        self.refunded: dict[str, Decimal] = {}
        self.wechat_yield_rows: list[dict] = []
        self.invest = Investments(self.rng, self.journal)

    @property
    def bal(self):
        return self.journal.cash

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

    def _product(self, account: str, funding: str) -> Decimal:
        return self.journal.cash.get(f"{account}:{funding}", ZERO)

    def _payable(self, account: str) -> Decimal:
        return sum((self._product(account, name) for name in D.PAYABLE_FUNDING[account]), ZERO)

    def _project(self, event: Event, account: str, direction: str, counterparty: str,
                 *, when=None, item=None, mirror=False, amount=None, **view) -> Posting:
        when = when or event.at
        p = Posting(
            account, when, direction, event.amount if amount is None else amount,
            event.business.category, counterparty, event.business.item if item is None else item,
            tx_no=self.order_no(account, when), wallet_domain=WALLET_DOMAINS.get(account, ""),
            effect=view.pop("effect", {"支出": "out", "收入": "in"}.get(direction, "none")),
            counterparty_account=view.pop("counterparty_account", "") or D.party_account(counterparty),
            event_id=event.id, secondary=mirror, **view,
        )
        self.postings.append(p)
        return p

    def pay(self, when: datetime, amount: Decimal, category: str, counterparty: str,
            item: str, *, channel: str | None = None, order_channel: str = "",
            kind: str = "purchase", merchant_no: str = "",
            merchant: str = "", purpose: str = "") -> Posting | None:
        amount = money(amount)
        auto_channel = channel is None
        if auto_channel:
            channel = self.choose_channel(amount, counterparty)
        # 个人转账不会直接刷信用卡；自动选到信用卡时改走微信/支付宝余额。
        # 显式指定 channel="credit" 的分期扣款不受影响。
        if auto_channel and purpose == "gift" and channel in D.CREDIT_ACCOUNTS:
            channel = "wechat" if self.rng.random() < 0.5 else "alipay"
        kwargs = {"kind": kind, "order_channel": order_channel,
                  "merchant_no": merchant_no, "merchant": merchant, "purpose": purpose}

        # 小荷包消费要同时出现在支付宝主账单和小荷包账单里，
        # 不能只落到小荷包账户；余额不够时退回普通支付宝付款。
        if channel in WALLET_NAME and purpose != "gift":
            if self._wallet_pay("alipay", f"支付宝小荷包({WALLET_NAME[channel]})", when, amount,
                                category, counterparty, item, wallet_account=channel, **kwargs):
                return self._last_post("alipay", when)
            channel = "alipay"

        # 钱包通道要先处理：alipay / wechat 同时也是账户 id，
        # 直接走账户分支会绕掉「付款方式」和信用卡联动。
        if channel == "alipay":
            # 转账/红包不能刷信用卡或花呗，只能用余额或余额宝。
            if purpose == "gift":
                for method in ("余额宝", "账户余额"):
                    if self._wallet_pay("alipay", method, when, amount, category,
                                        counterparty, item, **kwargs):
                        return self._last_post("alipay", when)
                return None
            r = self.rng.random()
            if r < 0.04 and amount <= Decimal("300") \
                    and self.journal.values["allowance:parent"] + amount <= D.QINQING_LIMIT:
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
            if purpose == "gift":
                for method in ("零钱通", "零钱"):
                    if self._wallet_pay("wechat", method, when, amount, category,
                                        counterparty, item, **kwargs):
                        return self._last_post("wechat", when)
                return None
            r = self.rng.random()
            if r < 0.05 and amount <= Decimal("200") \
                    and self.journal.values["allowance:child"] + amount <= D.QINSHU_LIMIT:
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
        actions = {"income": self.receipt, "spend": self.pay, "closed": self._closed,
                   "move": self._move, "yield": self._yield, "statement": self._statement,
                   "repay": self._repay, "transfer": self.transfer, "refund": self._refund_action,
                   "reset": self._reset, "raise": self._raise, "market": self.invest.mark,
                   "sip": self.invest.sip, "redeem": self.invest.redeem, "trade": self.invest.trade,
                   "snapshot": self.invest.snapshot}
        d = self.start
        while d <= self.end:
            intents: list[tuple[datetime, str, dict]] = []
            self._plan_funding(d, intents)
            self._plan_products(d, intents)
            self._plan_fixed(d, intents)
            self._plan_travel(d, intents)
            self._plan_daily(d, intents)
            self.invest.plan(d, self.end, intents)
            for when, kind, payload in sorted(intents, key=lambda x: x[0]):
                if kind in {"sip", "redeem"}:
                    actions[kind](when, self)
                else:
                    actions[kind](**(payload | {"when": when}))
            d += timedelta(days=1)

        return self.dataset()

    def dataset(self) -> Dataset:
        balances = {k: self.bal[k] for k in D.ACCOUNTS if k in self.bal}
        balances.update({account: sum((self.bal[k] for k, p in D.WALLET_PRODUCTS.items()
                                        if p.account == account), ZERO) for account in D.PAYABLE_FUNDING})
        balances.update(fund=self.invest.fund_value(), broker=self.invest.broker_value())
        credit = {k: max(ZERO, self.journal.values[f"credit:{k}"]) for k in D.CREDIT_ACCOUNTS}
        balances.update(dict.fromkeys(credit, ZERO))
        return Dataset(
            postings=self.postings, balances=balances, credit_used=sum(credit.values(), ZERO),
            credit_used_by=credit, snapshots=self.invest.snapshots, fund_rows=self.invest.fund_rows,
            broker_rows=self.invest.broker_rows, wealth_rows=self.invest.wealth_rows,
            start=self.start, end=self.end, seed=self.seed, journal=self.journal,
            wallet_products={k: self.bal[k] for k in D.WALLET_PRODUCTS},
            huabei_used=max(ZERO, self.journal.values["credit:huabei"]),
            wechat_yield_rows=self.wechat_yield_rows,
        )

    def _closed(self, when, account, amount, category, counterparty, item, order_channel):
        merchant = "淘宝虚构服饰店"
        seller = A.external(self.journal, merchant, "merchant")
        order = str(self.rng.randint(10**15, 10**16 - 1))
        e = self.journal.record(when, self._business("closed", item, category,
                                                    reference=f"order:{order_channel}:{order}"),
                                amount, payer=A.OWNER, payee=seller, status="closed")
        self._project(e, account, "支出", counterparty, kind="closed", effect="none",
                      status="交易关闭", order_channel=order_channel, merchant=merchant, merchant_no=order)

    def _reset(self, when):
        keys = ("allowance:parent", "allowance:child")
        self.journal.record(when, self._business("control", "亲属卡月度额度重置", "信用借还"),
                            ZERO, payer=A.OWNER, payee=A.OWNER,
                            changes=tuple(Change(k, -self.journal.values[k]) for k in keys))

    def _raise(self, when):
        key = "employment:net_salary"
        salary = self.journal.values[key]
        self.journal.record(when, self._business("control", "调薪", "收入", reference="employment"),
                            ZERO, payer="employer", payee=A.OWNER,
                            changes=(Change(key, money(salary * D.RAISE_RATE) - salary),))

    def _plan_products(self, d: date, intents: list) -> None:
        """余额宝/零钱通收益、花呗账单、平台内部划转。"""
        def at(h: int, m: int = 0) -> datetime:
            return datetime.combine(d, time(h, m, self.rng.randint(0, 59)))

        if d.day == 1:
            intents.extend([(datetime.combine(d, time(0, 0)), "reset", {}),
                            (datetime.combine(d, time(0, 0)), "statement", {"credit": "huabei"}),
                            (at(3), "yield", {"account": "alipay", "funding": "理财", "divisor": 12,
                                               "item": "理财-收益发放", "counterparty": D.ALIPAY_WEALTH,
                                               "counterparty_account": "/"}),
                            (at(3, 5), "yield", {"account": "wechat", "funding": "理财通", "divisor": 12})])
        intents.extend([(at(4, 5), "yield", {"account": "alipay", "funding": "余额宝",
                                             "item": f"余额宝-{d:%Y.%m.%d}-收益发放",
                                             "counterparty": D.YUEBAO_FUND, "pay_method": "余额宝",
                                             "counterparty_account": "/"}),
                        (at(4, 10), "yield", {"account": "wechat", "funding": "零钱通"})])
        if d.day == 10:
            intents.append((at(9, 25), "repay", {"credit": "huabei"}))

        if d.day == 11:
            available = self._product("alipay", "余额") - Decimal("1500.00")
            amount = min(available, Decimal("10000.00"))
            if amount >= Decimal("100.00"):
                intents.append((at(7, 0), "move", {
                    "account": "alipay", "src": "余额",
                    "dst": "余额宝", "amount": amount, "item": "余额宝-自动转入",
                    "counterparty": D.YUEBAO_COMPANY, "pay_method": "",
                    "counterparty_account": "/",
                }))

        if d.day == 15 \
                and self._product("alipay", "余额宝") - Decimal("1000.00") >= Decimal("15000.00"):
            intents.append((at(10, 0), "move", {
                "account": "alipay", "src": "余额宝",
                "dst": "理财", "amount": Decimal("1000.00"), "item": "理财产品申购",
                "counterparty": D.ALIPAY_WEALTH, "pay_method": "余额宝",
                "counterparty_account": "/",
            }))

        if d.month in (3, 6, 9, 12) and d.day == 20 \
                and self._product("alipay", "理财") >= Decimal("1500.00"):
            intents.append((at(20, 0), "move", {
                "account": "alipay", "src": "理财",
                "dst": "余额宝", "amount": Decimal("1500.00"), "item": "理财产品赎回",
                "counterparty": D.ALIPAY_WEALTH, "pay_method": "",
                "counterparty_account": "/",
            }))

        if d.day == 25 and self._product("alipay", "余额") < Decimal("800.00") \
                and self._product("alipay", "余额宝") > Decimal("2000.00"):
            intents.append((at(8, 0), "move", {
                "account": "alipay", "src": "余额宝",
                "dst": "余额", "amount": Decimal("1500.00"),
                "item": "余额宝-转出到余额", "counterparty": "余额宝",
                "pay_method": "账户余额", "counterparty_account": "jij***@southernfund.com",
            }))

        if d.day == 12 and self._product("wechat", "零钱") > Decimal("3000.00"):
            intents.append((at(7, 0), "move", {
                "account": "wechat", "src": "零钱",
                "dst": "零钱通", "amount": Decimal("3000.00"),
                "item": "转入零钱通-来自零钱", "counterparty": "/", "pay_method": "零钱",
                "platform_type": "转入零钱通-来自零钱", "status": "支付成功",
                "counterparty_account": "/",
            }))

        if d.day == 16 \
                and self._product("wechat", "零钱通") - Decimal("800.00") >= Decimal("5000.00"):
            intents.append((at(10, 0), "move", {
                "account": "wechat", "src": "零钱通",
                "dst": "理财通", "amount": Decimal("800.00"), "item": "活期＋",
                "counterparty": "理财通", "pay_method": "零钱通",
                "platform_type": "购买理财通", "status": "支付成功",
                "counterparty_account": "/",
            }))

        # 每月把理财赎回到可付款产品，保持余额宝/零钱通的最低水位。
        if d.day == 21:
            gap = Decimal("16000.00") - self._product("alipay", "余额宝")
            spare = self._product("alipay", "理财") - Decimal("5000.00")
            gap = min(gap, spare)
            if gap >= Decimal("100.00"):
                intents.append((at(9, 30), "move", {
                    "account": "alipay", "src": "理财",
                    "dst": "余额宝", "amount": gap, "item": "理财产品赎回",
                    "counterparty": D.ALIPAY_WEALTH, "pay_method": "",
                    "counterparty_account": "/",
                }))
            gap = Decimal("6000.00") - self._product("wechat", "零钱通")
            spare = self._product("wechat", "理财通") - Decimal("5000.00")
            gap = min(gap, spare)
            if gap >= Decimal("100.00"):
                intents.append((at(9, 35), "move", {
                    "account": "wechat", "src": "理财通",
                    "dst": "零钱通", "amount": gap, "item": "活期＋",
                    "counterparty": "理财通", "pay_method": "零钱通",
                    "platform_type": "理财通赎回", "status": "已到账",
                    "counterparty_account": "/",
                }))

        if d.month in (3, 6, 9, 12) and d.day == 20 \
                and self._product("wechat", "理财通") >= Decimal("1000.00"):
            intents.append((at(14, 0), "move", {
                "account": "wechat", "src": "理财通",
                "dst": "零钱通", "amount": Decimal("1000.00"), "item": "活期＋",
                "counterparty": "理财通", "pay_method": "零钱通",
                "platform_type": "理财通赎回", "status": "已到账",
                "counterparty_account": "/",
            }))

        if d.day == 18 and self._product("wechat", "零钱") < Decimal("400.00") \
                and self._product("wechat", "零钱通") > Decimal("1000.00"):
            intents.append((at(18, 0), "move", {
                "account": "wechat", "src": "零钱通",
                "dst": "零钱", "amount": Decimal("800.00"), "item": "/",
                "counterparty": "/", "pay_method": "零钱通",
                "platform_type": "零钱通转出-到零钱", "status": "资金已到账",
                "counterparty_account": "/",
            }))

        if d.month in (3, 6, 9, 12) and d.day == 22 \
                and self._product("wechat", "零钱") >= Decimal("500.00"):
            intents.append((at(16, 0), "transfer", {
                "src": "wechat", "dst": "salary", "source_funding": "零钱", "withdraw": True,
                "amount": Decimal("500.00"), "item": "零钱提现", "counterparty": "招商银行储蓄卡(0371)",
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
                "account": "salary", "amount": D.BONUS,
                "category": "收入", "counterparty": "启明科技有限公司",
                "action": "bonus", "item": "年终奖金", "kind": "income",
            }))

        if d.month == 1 and d.day == 22:
            ts = at(15, 0)
            for amount, item, channel, category in (
                (money(self.rng.uniform(3000, 4500)), "春节年货", "alipay", "日用百货"),
                (money(self.rng.uniform(4000, 6000)), "春节红包", "wechat", "转账红包"),
                (money(self.rng.uniform(1500, 2500)), "春节礼品", "alipay", "文化休闲"),
            ):
                intents.append((ts, "spend", {
                    "amount": amount, "category": category,
                    "counterparty": "春节年货" if "年货" in item else
                                    ("亲友红包" if "红包" in item else "春节礼品"),
                    "item": item, "channel": channel,
                    "kind": "transfer" if "红包" in item else "purchase",
                    "purpose": "gift" if "红包" in item else "",
                }))
                ts += timedelta(minutes=7)

        # 4 月调薪 5%，只影响之后的工资；年终奖仍按 3 个月基数发。
        if d.month == D.RAISE_MONTH and d.day == 10:
            intents.append((datetime.combine(d, time(0, 1)), "raise", {}))

        if d.month == 4 and d.day == 15:
            ts = at(11, 20)
            intents.append((ts, "income", {
                "account": "salary", "when": ts,
                "amount": money(self.rng.uniform(800, 2600)),
                "category": "收入", "counterparty": "国家税务总局",
                "action": "tax_refund", "item": "个税汇算退税", "kind": "income",
            }))

        # 半年：4/10 月车辆保养
        if d.month in (4, 10) and d.day == 15:
            ts = at(14, 10)
            intents.append((ts, "spend", {
                "amount": money(self.rng.uniform(1200, 2000)),
                "category": "汽车", "counterparty": "广州鹏辉汽车服务",
                "item": "车辆保养", "channel": "credit2",
            }))

        # 618 / 双 11 大额采购
        if d.month == 6 and d.day == 18:
            ts = at(20, 0)
            intents.append((ts, "spend", {
                "amount": money(self.rng.uniform(6000, 8000)),
                "category": "服饰装扮", "counterparty": "天猫",
                "item": "618 大促购物", "channel": "credit", "order_channel": "tmall",
                "merchant": "岭南生活旗舰店",
            }))
        if d.month == 11 and d.day == 11:
            ts = at(20, 0)
            intents.append((ts, "spend", {
                "amount": money(self.rng.uniform(8000, 11000)),
                "category": "服饰装扮", "counterparty": "京东",
                "item": "双 11 大促购物", "channel": "credit", "order_channel": "jd",
                "merchant": "京东自营家居",
            }))

        # 年缴保险
        for month, day, item, amount, counterparty in D.INSURANCE_POLICIES:
            if d.month == month and d.day == day:
                ts = at(11, 0)
                intents.append((ts, "spend", {
                    "amount": amount, "category": "保险",
                    "counterparty": counterparty, "item": item,
                    "channel": "credit", "purpose": "contract",
                }))

        # ── 每季度 ────────────────────────────────────────────
        if d.month in (1, 4, 7, 10) and d.day == 5:
            ts = at(10, 20)
            intents.append((ts, "spend", {
                "amount": D.PROPERTY_FEE, "category": "房租物业",
                "counterparty": "锦园物业", "item": "物业费", "channel": "wallet_family", "purpose": "contract",
            }))
            ts += timedelta(minutes=5)
            intents.append((ts, "spend", {
                "amount": D.PARKING_FEE, "category": "交通出行",
                "counterparty": "锦园物业", "item": "停车位租金", "channel": "alipay", "purpose": "contract",
            }))

        if d.month in (1, 4, 7, 10) and d.day == 10:
            ts = at(13, 0)
            intents.append((ts, "spend", {
                "amount": D.TUITION, "category": "教育培训",
                "counterparty": "育才实验学校", "item": "学杂费", "purpose": "contract",
                "channel": "salary", "kind": "transfer",
            }))

        # ── 每月 ──────────────────────────────────────────────
        for credit, (statement, due) in D.CREDIT_CYCLE.items():
            if d.day == statement:
                intents.append((datetime.combine(d, time(0, 0)), "statement", {"credit": credit}))
            if d.day == due:
                intents.append((at(20, 30), "repay", {"credit": credit}))

        if d.day == 8:
            ts = at(9, 0)
            intents.append((ts, "spend", {
                "amount": D.CAR_LOAN,
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
                "account": "salary", "amount": D.SALARY,
                "category": "收入", "counterparty": "启明科技有限公司",
                "action": "salary", "item": "工资", "kind": "income",
            }))
            # 配偶每月转入家庭小荷包，体现共同承担。
            ts = at(10, 40)
            intents.append((ts, "income", {
                "account": "wallet_family", "amount": D.SPOUSE_TRANSFER,
                "category": "转账红包", "counterparty": "林知夏",
                "action": "contribution", "item": "转入小荷包", "kind": "transfer",
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
                "amount": D.CONSUMER_LOAN_MONTHLY,
                "category": "投资理财", "counterparty": "招联消费金融",
                "item": "消费贷分期扣款", "channel": "salary", "kind": "transfer",
            }))
            ts += timedelta(minutes=2)
            intents.append((ts, "spend", {
                "amount": D.CREDIT_LOAN_MONTHLY,
                "category": "投资理财", "counterparty": "招商银行",
                "item": "信用贷分期扣款", "channel": "salary", "kind": "transfer",
            }))
            ts += timedelta(minutes=2)
            intents.append((ts, "spend", {
                "amount": D.CREDIT_INSTALLMENT_MONTHLY,
                "category": "投资理财", "counterparty": "招商银行信用卡",
                "item": "信用卡分期还款", "channel": "credit", "kind": "transfer",
            }))

        if d.day == 15:
            ts = at(9, 30)
            intents.append((ts, "spend", {
                "amount": D.MORTGAGE, "category": "房租物业",
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
                    "amount": money(self.rng.uniform(low, high)),
                    "category": "生活缴费", "counterparty": counterparty, "purpose": "contract",
                    "item": item, "channel": "living_card",
                }))
                ts += timedelta(seconds=35)

        # 订阅：每月 1—4 号各扣一笔
        if d.day <= len(D.SUBSCRIPTIONS):
            name, item, amount = D.SUBSCRIPTIONS[d.day - 1]
            ts = at(12, 10)
            intents.append((ts, "spend", {
                "amount": amount, "category": "文化休闲",
                "counterparty": name, "item": item, "channel": "alipay", "purpose": "contract",
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
                    "amount": money(self.rng.uniform(480, 620) * 2),
                    "category": "交通出行", "counterparty": "中国铁路",
                    "item": f"高铁票-{city}", "channel": "credit2", "purpose": "travel",
                }))
            intents.append((at(12), "spend", {
                "amount": money(self.rng.uniform(380, 860)),
                "category": "文化休闲", "counterparty": f"{city}栖迟酒店",
                "item": "酒店住宿", "channel": "credit2", "purpose": "travel",
            }))
            intents.append((at(13), "spend", {
                "amount": money(self.rng.uniform(80, 260)),
                "category": "餐饮美食", "counterparty": f"{city}老街小馆",
                "item": "旅游餐饮", "channel": "wallet_travel", "purpose": "travel",
            }))
            intents.append((at(15), "spend", {
                "amount": money(self.rng.uniform(60, 320)),
                "category": "文化休闲", "counterparty": f"{city}景区",
                "item": "景点门票", "channel": "alipay", "purpose": "travel",
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
                "amount": money(amount), "category": category,
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
                  "转账备注:微信转账", kind="transfer", purpose="gift")
        if maybe(0.1):
            intents.append((at(9, 21), "refund", {}))

        if maybe(0.04):
            t = at(10, 22)
            intents.append((t, "closed", {
                "account": "alipay", "amount": money(rng.uniform(20, 300)),
                "category": "服饰装扮", "counterparty": "淘宝", "item": "订单已取消",
                "order_channel": "taobao",
            }))

    def _refund_action(self, when):
        start = when - timedelta(days=7)
        candidates = [e for e, _, _ in self.sales.values()
                      if start <= e.at < when and self.refunded.get(e.id, ZERO) < e.amount]
        if candidates:
            sale = self.rng.choice(candidates)
            self.refund(when, sale.id, sale.amount - self.refunded.get(sale.id, ZERO))


def generate(seed: int = 20261126, start: date | None = None,
             months: int = 12) -> Dataset:
    if months < 1:
        raise ValueError("months must be positive")
    start = start or date(2026, 1, 1)
    # 结束日 = start + months 个月 - 1 天
    y, m = divmod(start.month - 1 + months, 12)
    end = date(start.year + y, m + 1, 1) - timedelta(days=1)
    dataset = Ledger(seed, start, end).run()
    # 答案层依赖引擎里的 Posting，放在这里做一次性标注；局部导入避免循环引用。
    from . import truth
    truth.annotate(dataset)
    return dataset
