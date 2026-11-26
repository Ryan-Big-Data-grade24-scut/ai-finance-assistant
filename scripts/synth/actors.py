"""Economic owners and standing agreements, separate from platform display fixtures."""
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256

from . import data as D
from .journal import Journal, ZERO, money

OWNER = "household:owner"
SPOUSE = "household:spouse"
CHILD = "household:child"
MOTHER = "household:mother"

# ID, legal/economic name, role, cash at the opening of the simulation.
EXTERNAL = {
    "employer": ("启明科技有限公司", "employer", "2500000"),
    "tax": ("国家税务总局", "tax_authority", "1000000"),
    "cmb": ("招商银行", "issuer", "1000000"),
    "cgb": ("广发银行", "issuer", "1000000"),
    "huabei_issuer": ("花呗授信机构", "issuer", "500000"),
    "alipay_clearing": ("支付宝", "collector", "0"),
    "wechat_clearing": ("微信支付", "collector", "0"),
    "yuebao": (D.YUEBAO_FUND, "product_provider", "100000"),
    "wechat_mm": ("零钱通货币基金", "product_provider", "100000"),
    "alipay_wealth": (D.ALIPAY_WEALTH, "product_provider", "100000"),
    "wechat_wealth": ("理财通产品管理人", "product_provider", "100000"),
    "bank_wealth": ("招银理财产品管理人", "product_provider", "200000"),
    "fund_app": ("天天基金", "investment_interface", "0"),
    "broker": ("华泰证券", "broker", "0"),
    "market": ("虚构证券市场结算对手", "market_counterparty", "1000000"),
}

CREDIT_ISSUER = {"credit": "cmb", "credit2": "cgb", "huabei": "huabei_issuer"}
YIELD_PROVIDER = {"alipay:余额宝": "yuebao", "alipay:理财": "alipay_wealth",
                  "wechat:零钱通": "wechat_mm", "wechat:理财通": "wechat_wealth",
                  "wealth": "bank_wealth"}

# A channel can hide the seller or show a payment service instead of the supplier.
SERVICE_PROVIDER = {
    "手机地铁乘车码": "广州地铁运营方", "公交乘车码": "广州公交运营方",
    "滴滴出行": "广州安行网约车服务", "春节年货": "家家乐超市",
    "春节礼品": "岭南礼品店", "亲友红包": "周姨",
}


@dataclass(frozen=True)
class Loan:
    key: str
    lender: str
    opening: Decimal
    installment: Decimal
    periods: int
    first: str
    channel: str

    @property
    def rate(self) -> Decimal:
        return (self.installment - money(self.opening / self.periods)) / self.opening


LOANS = {
    item: Loan(key, lender, money(opening), amount, periods, first, channel)
    for item, key, lender, opening, amount, periods, first, channel in (
        ("个人住房贷款还款", "mortgage", "中国工商银行", "1800000", D.MORTGAGE, 360, "2025-08", "mortgage_card"),
        ("汽车贷款月供", "auto", "招商银行汽车金融", "87000", D.CAR_LOAN, 36, "2025-06", "salary"),
        ("消费贷分期扣款", "consumer", "招联消费金融", "24000", D.CONSUMER_LOAN_MONTHLY, 24, "2025-09", "salary"),
        ("信用贷分期扣款", "personal", "招商银行", "18000", D.CREDIT_LOAN_MONTHLY, 36, "2025-10", "salary"),
        ("信用卡分期还款", "installment", "招商银行", "12000", D.CREDIT_INSTALLMENT_MONTHLY, 12, "2025-12", "credit"),
    )
}


def digest(name: str) -> str:
    return sha256(name.encode()).hexdigest()[:16]


def external(journal: Journal, name: str, role: str) -> str:
    key = f"{role}:{digest(name)}"
    journal.actor(key, name, role)
    # Late-discovered actors still have window-opening balances, never event-time top-ups.
    journal.account(key, key, "operating", money("5000") if role in {"merchant", "individual"} else ZERO)
    return key


def setup(journal: Journal) -> None:
    for key, name, role in ((OWNER, D.PERSONA["name"], "account_owner"),
                            (SPOUSE, "林知夏", "spouse"), (CHILD, "周小满", "child"),
                            (MOTHER, "周母", "parent")):
        journal.actor(key, name, role)
    journal.account(SPOUSE, SPOUSE, "shared_budget_funding", money("150000"))
    for key, (name, role, opening) in EXTERNAL.items():
        journal.actor(key, name, role)
        journal.account(key, key, "clearing" if role == "collector" else "operating", money(opening))
    for key, acct in D.ACCOUNTS.items():
        if key not in {"alipay", "wechat", "fund", "broker", *D.CREDIT_ACCOUNTS}:
            journal.account(key, OWNER, acct.kind, acct.opening)
    for key, product in D.WALLET_PRODUCTS.items():
        journal.account(key, OWNER, "wallet_product", product.opening)
    for key, issuer in CREDIT_ISSUER.items():
        limit = D.HUABEI_LIMIT if key == "huabei" else D.ACCOUNTS[key].limit
        journal.state(f"credit:{key}", floor=None, ceiling=limit)
        journal.state(f"bill:{key}")
        journal.account(f"prepaid:{key}", OWNER, "credit_overpayment")
    for loan in LOANS.values():
        journal.state(f"loan:{loan.key}", loan.opening)
    journal.state("allowance:parent", ceiling=D.QINQING_LIMIT)
    journal.state("allowance:child", ceiling=D.QINSHU_LIMIT)
    journal.state("employment:net_salary", D.SALARY)
