"""答案层：把生成的流水还原成一本「真实账」，并逐行对回原始导出。

这里故意把两件事分开：

* **真实口径**（`true_category` / `expense_class` / `cadence` / `flow_role`）
  由账务事件本身决定，用来回答「收入、义务、刚性、弹性各有多少」。
* **平台口径**（`platform_category`）保留真实平台的粗粒度与系统性偏差：
  个体户小商贩被压成「其他」或「商户消费」，找不到关键词就归「其他」。
  它只写进原始导出，不能拿来当答案。

对账的硬要求：每一行原始导出都能按单号（或投资类文件的多字段元组）
找到且只找到一条答案；答案里的每笔预期出现也都能在导出里找到。
"""
from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable

import openpyxl

from . import data as D
from .engine import Dataset, Posting, WALLET_NAME, money

EXPENSE_CLASSES = ("收入", "义务支出", "刚性支出", "弹性支出", "中性")
CADENCES = ("每日", "每周", "每月", "每季", "半年", "每年", "事件", "结算")
FLOW_ROLES = ("income", "primary", "mirror", "settlement", "asset_move",
              "external_inflow", "refund", "closed", "tax_refund")

# 公司/机构：平台会看到对公账户，但账主心里知道这是一笔「机构收款」。
INSTITUTION_NAMES = frozenset({
    "启明科技有限公司", "国家税务总局", "中国工商银行", "工商银行",
    "招商银行汽车金融", "招联消费金融", "招商银行", "广发银行",
    "中国建设银行", "中国铁路", "中国电信", "广东电网", "广州自来水",
    "广州燃气", "国家电网", "中国移动", "长宁人寿保险", "众安在线财险",
    "平安产险", "锦园物业", "育才实验学校", "广州鹏辉汽车服务",
    "天天基金", "华泰证券", "建信理财", "招商银行理财",
})
PLATFORM_NAMES = frozenset({
    "支付宝", "微信支付", "微信零钱", "花呗", "余额宝", "理财通", "零钱通",
    "淘宝", "天猫", "京东", "美团", "滴滴出行", "哈啰单车",
    "手机地铁乘车码", "公交乘车码", "招商银行信用卡", "广发银行信用卡",
    "招商银行储蓄卡", "招商银行储蓄卡(0371)", "工商银行储蓄卡",
    "建设银行储蓄卡",
})

OBLIGATION_ITEMS = frozenset({
    "个人住房贷款还款", "汽车贷款月供", "消费贷分期扣款", "信用贷分期扣款",
    "信用卡分期还款", "物业费", "停车位租金",
    "重疾险年缴保费", "百万医疗险保费", "家庭财产险保费", "定期寿险保费",
    "车辆保险保费",
})
RIGID_ITEMS = frozenset({
    "水费", "电费", "燃气费", "宽带及话费", "学杂费",
    "地铁乘车", "公交通勤", "共享单车", "快车-科技园",
    "感冒药", "门诊挂号", "洗牙", "常规体检",
    "男士剪发", "洗剪吹", "车辆保养",
    "文具教辅", "校车月费", "兴趣班材料",
})
FLEXIBLE_ITEMS = frozenset({
    "视频会员月费", "音乐会员月费", "云盘会员月费", "健身房月卡",
    "春节年货", "春节红包", "春节礼品", "618 大促购物", "双 11 大促购物",
    "旅游餐饮", "酒店住宿", "景点门票",
})
ASSET_MOVE_ITEMS = frozenset({
    "基金定投扣款", "基金赎回款", "理财产品申购", "理财产品赎回",
    "余额宝-自动转入", "余额宝-转出到余额", "活期＋",
    "转入零钱通-来自零钱", "购买理财通", "理财通赎回", "零钱通转出-到零钱",
})
SETTLEMENT_ITEMS = frozenset({
    "转账", "跨行转账", "转入小荷包", "家庭预算转入", "充值", "零钱充值",
    "信用卡还款", "零钱提现",
})
MONTHLY_ITEMS = frozenset({
    "工资", "汽车贷款月供", "消费贷分期扣款", "信用贷分期扣款",
    "信用卡分期还款", "基金定投扣款", "个人住房贷款还款",
    "水费", "电费", "燃气费", "宽带及话费", "信用卡还款",
    "视频会员月费", "音乐会员月费", "云盘会员月费", "健身房月卡",
    "余额宝-自动转入", "理财产品申购", "理财产品赎回", "活期＋",
    "转入零钱通-来自零钱", "购买理财通", "理财通赎回", "零钱通转出-到零钱",
})
QUARTERLY_ITEMS = frozenset({"物业费", "停车位租金", "学杂费", "基金赎回款"})
ANNUAL_ITEMS = frozenset({
    "年终奖金", "个税汇算退税", "春节年货", "春节红包", "春节礼品",
    "重疾险年缴保费", "百万医疗险保费", "家庭财产险保费", "定期寿险保费",
    "车辆保险保费", "618 大促购物", "双 11 大促购物",
})
DAILY_ITEMS = frozenset(
    set(D.BREAKFAST_ITEMS) | set(D.LUNCH_ITEMS) | set(D.DINNER_ITEMS)
    | set(D.GROCERY_ITEMS) | set(D.COFFEE_ITEMS)
    | {"早餐补给", "饮料", "日用品", "电池/纸巾", "外卖订单"}
)
TRAVEL_ITEMS = frozenset({"旅游餐饮", "酒店住宿", "景点门票"})

_ORDER_DIR = {"meituan": "美团/美团订单明细.csv",
              "taobao": "淘宝天猫/淘宝天猫订单明细.csv",
              "tmall": "淘宝天猫/淘宝天猫订单明细.csv",
              "jd": "京东/京东订单明细.csv"}
_BANK_DIR = {"salary": "银行卡/招商银行储蓄卡_交易明细.csv",
             "mortgage_card": "银行卡/工商银行_交易明细.csv",
             "living_card": "银行卡/建设银行_交易明细.csv"}
_CREDIT_DIR = {"credit": "银行卡/招商银行信用卡_账单明细.csv",
               "credit2": "银行卡/广发银行信用卡_账单明细.csv"}


# ── 身份与分类 ────────────────────────────────────────────────
def counterparty_type(p: Posting) -> str:
    name = p.counterparty
    if name in INSTITUTION_NAMES:
        return "institution"
    if name in PLATFORM_NAMES or name.startswith(("支付宝小荷包", "小荷包")):
        return "platform"
    kind = D.party_kind(name)
    if kind == "person":
        return "person"
    if kind == "individual":
        return "individual"
    if kind == "platform":
        return "platform"
    return "merchant"


def _food_class(p: Posting) -> str:
    if p.item in D.BREAKFAST_ITEMS or p.item in D.LUNCH_ITEMS \
            or p.item in D.DINNER_ITEMS or p.item == "外卖订单":
        return "刚性支出"
    return "弹性支出"


def true_expense_class(p: Posting) -> str:
    """真实口径的支出大类；镜像/结算在 flow_role 里再区分。"""
    item, category = p.item, p.category
    if p.kind == "closed":
        return "中性"
    if p.kind == "refund":
        return "中性"
    if item in {"工资", "年终奖金"} or category == "收入" \
            or "收益发放" in item:
        return "收入"
    if item in OBLIGATION_ITEMS:
        return "义务支出"
    if item in RIGID_ITEMS:
        return "刚性支出"
    if item in FLEXIBLE_ITEMS:
        return "弹性支出"
    if item in ASSET_MOVE_ITEMS or item == "信用卡还款" \
            or item.startswith("花呗主动还款") or item in SETTLEMENT_ITEMS:
        return "中性"
    if p.order_channel and p.order_channel != "meituan":
        return "弹性支出"
    if category == "生活缴费":
        return "刚性支出"
    if category == "餐饮美食":
        return _food_class(p)
    if category == "日用百货":
        if p.order_channel:
            return "弹性支出"
        return "刚性支出"
    if category in {"服饰装扮", "数码电器", "居家生活", "文化休闲"}:
        return "弹性支出"
    if category == "交通出行":
        return "刚性支出"
    if category in {"医疗健康", "生活服务", "教育培训"}:
        return "刚性支出"
    if category == "房租物业":
        return "刚性支出"
    if category == "保险":
        return "义务支出"
    if category == "汽车":
        return "刚性支出"
    if category == "投资理财":
        return "中性"
    if category == "转账红包":
        if p.kind == "transfer" and p.counterparty_type == "person":
            return "弹性支出"
        return "中性"
    if category == "信用借还":
        return "中性"
    return "弹性支出" if p.kind == "purchase" else "中性"


def cadence_of(p: Posting) -> str:
    item = p.item
    if item in ANNUAL_ITEMS:
        return "每年"
    if item in QUARTERLY_ITEMS:
        return "每季"
    if item == "车辆保养":
        return "半年"
    if item in MONTHLY_ITEMS:
        return "每月"
    if item.startswith("花呗主动还款"):
        return "每月"
    if "收益发放" in item:
        return "每日" if item.startswith("余额宝-") else "每月"
    if item in DAILY_ITEMS:
        return "每日"
    if item in D.DINING_OUT_ITEMS or item in {"朋友聚餐", "家庭聚餐", "同事聚会", "生日宴"}:
        return "每周"
    if item in TRAVEL_ITEMS:
        return "事件"
    if p.kind in {"refund", "closed"}:
        return "事件"
    if p.flow_role in {"settlement", "asset_move", "external_inflow", "mirror"}:
        return "结算"
    return "事件"


def business_labels(kind: str, item: str, category: str, channel: str,
                    purpose: str) -> tuple[str, str]:
    if kind in {"tax_refund", "contribution", "settlement", "asset_move", "statement", "control", "refund", "closed"}:
        cls = "中性"
    elif kind in {"yield", "payroll"}:
        cls = "收入"
    elif purpose in {"travel", "gift"}:
        cls = "弹性支出"
    else:
        # Policy applied to the known item/purpose, not a bank or wallet description.
        cls = true_expense_class(Posting("", None, "", money(0), category, "", item,
                                         kind="purchase", order_channel=channel))
    p = Posting("", None, "", money(0), category, "", item, kind=kind,
                flow_role="settlement" if kind == "settlement" else "")
    cadence = cadence_of(p)
    if kind == "yield":
        cadence = "每日" if purpose == "daily" else "每月"
    elif kind in {"statement", "contribution"}:
        cadence = "每月"
    return cls, cadence


# ── 答案标注 ──────────────────────────────────────────────────
def annotate(dataset: Dataset) -> None:
    """Join projections to their business event. No fuzzy matching for mirror rows."""
    postings = sorted(dataset.postings, key=lambda p: (p.when, p.tx_no))
    main = {}
    for i, p in enumerate(postings, 1):
        p.truth_id = f"GT{i:06d}"
        if not p.secondary:
            main.setdefault(p.event_id, p.truth_id)
    for p in postings:
        e = dataset.journal.by_id[p.event_id]
        p.counterparty_type = counterparty_type(p)
        p.mirror_of = main[p.event_id] if p.secondary else ""
        p.expense_class = e.business.expense_class
        p.cadence = e.business.cadence
        p.flow_role = "mirror" if p.secondary else {
            "yield": "income", "payroll": "income", "tax_refund": "tax_refund",
            "contribution": "external_inflow", "refund": "refund", "closed": "closed",
            "settlement": "settlement", "asset_move": "asset_move",
        }.get(e.business.kind, "primary")
        if p.account == "alipay":
            p.platform_category = D.platform_category(
                p.category, direction=p.direction, kind=p.kind,
                counterparty=p.counterparty,
                counterparty_type=p.counterparty_type, item=p.item)
        elif p.account == "wechat":
            p.platform_category = p.platform_type or D.wechat_tx_type(
                kind=p.kind, counterparty_type=p.counterparty_type,
                tx_no=p.tx_no, direction=p.direction)
        else:
            p.platform_category = ""


def is_platform_classified(p: Posting) -> bool:
    if p.kind != "purchase" or p.secondary:
        return False
    if p.account == "alipay":
        return bool(p.platform_category)
    if p.account == "wechat":
        return p.platform_category in {"商户消费", "扫二维码付款", "商户消费-退款"}
    return False


def platform_mismatch(p: Posting) -> bool:
    return is_platform_classified(p) and p.platform_category != p.category


def platform_bias_stats(dataset: Dataset) -> dict:
    classified = [p for p in dataset.postings if is_platform_classified(p)]
    bad = [p for p in classified if platform_mismatch(p)]
    alipay = [p for p in classified if p.account == "alipay"]
    wechat = [p for p in classified if p.account == "wechat"]
    individual = [p for p in dataset.postings
                  if p.counterparty_type == "individual"
                  and p.kind == "purchase"
                  and p.account in {"alipay", "wechat"}]
    collapsed = [p for p in individual
                 if p.platform_category in {"其他", "商户消费", "扫二维码付款"}]
    return {
        "classified": len(classified),
        "mismatched": len(bad),
        "rate": (len(bad) / len(classified)) if classified else 0.0,
        "alipay": len(alipay),
        "alipay_mismatched": sum(platform_mismatch(p) for p in alipay),
        "wechat": len(wechat),
        "wechat_mismatched": sum(platform_mismatch(p) for p in wechat),
        "individual_purchases": len(individual),
        "individual_collapsed_to_merchant": len(collapsed),
    }


# ── 来源与周期 ────────────────────────────────────────────────
def source_exports(p: Posting, dataset: Dataset) -> list[str]:
    tag = f"{dataset.start:%Y%m%d}-{dataset.end:%Y%m%d}"
    out: list[str] = []
    if p.account == "alipay":
        out.append(f"支付宝/支付宝交易明细({tag}).csv")
    elif p.account == "wechat":
        out.append(f"微信/微信支付账单({tag}).xlsx")
    elif p.account in WALLET_NAME:
        out.append(f"支付宝小荷包/小荷包收支明细_{WALLET_NAME[p.account]}.xlsx")
    elif p.account in _BANK_DIR:
        out.append(_BANK_DIR[p.account])
    elif p.account in _CREDIT_DIR:
        out.append(_CREDIT_DIR[p.account])
    if p.order_channel and p.kind in {"purchase", "closed"}:
        out.append(_ORDER_DIR[p.order_channel])
    return out


def _period_keys(d: date) -> list[tuple[str, str]]:
    q = (d.month - 1) // 3 + 1
    h = 1 if d.month <= 6 else 2
    return [
        ("day", d.isoformat()),
        ("month", f"{d.year:04d}-{d.month:02d}"),
        ("quarter", f"{d.year:04d}-Q{q}"),
        ("half", f"{d.year:04d}-H{h}"),
        ("year", f"{d.year:04d}"),
    ]


SPEND_COLUMNS = {"义务支出": "obligation", "刚性支出": "rigid", "弹性支出": "flexible"}
PERIOD_TOTALS = ["income", "tax_refund", "contribution", *SPEND_COLUMNS.values(),
                 *(f"{name}_refund" for name in SPEND_COLUMNS.values()),
                 *(f"{name}_net" for name in SPEND_COLUMNS.values()),
                 "outflow", "refunds", "net_outflow", "net"]


def period_summary(dataset: Dataset) -> list[dict]:
    buckets: dict[tuple[str, str], Counter] = defaultdict(Counter)
    journal = dataset.journal
    for e in journal.events:
        if e.status != "settled":
            continue
        kind = e.business.kind
        cls = e.business.expense_class
        if kind == "refund":
            col = f"{SPEND_COLUMNS[journal.by_id[e.original].business.expense_class]}_refund"
        elif kind in {"tax_refund", "contribution"}:
            col = kind
        elif cls == "收入":
            col = "income"
        elif cls in SPEND_COLUMNS:
            col = SPEND_COLUMNS[cls]
        else:
            continue
        for key in _period_keys(e.at.date()):
            buckets[key][col] += e.amount

    rows = []
    for (gran, key), c in sorted(buckets.items(),
                                 key=lambda kv: (("day", "month", "quarter", "half", "year").index(kv[0][0]), kv[0][1])):
        for col in SPEND_COLUMNS.values():
            c[f"{col}_net"] = c[col] - c[f"{col}_refund"]
        c["outflow"] = sum(c[col] for col in SPEND_COLUMNS.values())
        c["refunds"] = sum(c[f"{col}_refund"] for col in SPEND_COLUMNS.values())
        c["net_outflow"] = c["outflow"] - c["refunds"]
        c["net"] = c["income"] - c["net_outflow"]
        rows.append({"granularity": gran, "period": key, **{col: money(c[col]) for col in PERIOD_TOTALS}})
    return rows


# ── 写答案文件 ────────────────────────────────────────────────
POSTING_HEADER = [
    "truth_id", "date", "time", "account", "direction", "amount",
    "true_category", "platform_category", "platform_mismatch",
    "expense_class", "cadence", "flow_role", "mirror_of",
    "counterparty", "counterparty_type", "item", "kind", "pay_method",
    "status", "tx_no", "merchant_no", "order_channel", "effect",
    "source_exports", "event_id", "funding", "original_event", "original_date", "original_class",
    "counterparty_account", "merchant", "remark", "refunded", "refund_full",
]


def _write_csv(path: Path, header: list[str], rows: Iterable[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8-sig")


def _posting_rows(dataset: Dataset) -> list[list]:
    out = []
    for p in sorted(dataset.postings, key=lambda x: (x.when, x.tx_no)):
        event = dataset.journal.by_id[p.event_id]
        original = dataset.journal.by_id.get(event.original)
        out.append([
            p.truth_id, p.when.date().isoformat(), p.when.strftime("%H:%M:%S"),
            p.account, p.direction, f"{p.amount:.2f}",
            p.category, p.platform_category,
            "1" if platform_mismatch(p) else "0",
            p.expense_class, p.cadence, p.flow_role, p.mirror_of,
            p.counterparty, p.counterparty_type, p.item, p.kind, p.pay_method,
            p.status, p.tx_no, p.merchant_no, p.order_channel, p.effect,
            ";".join(source_exports(p, dataset)), p.event_id, p.funding,
            event.original, original.at.date().isoformat() if original else "",
            original.business.expense_class if original else "", p.counterparty_account,
            p.merchant, p.remark, p.refunded, int(p.refund_full),
        ])
    return out


def _truth_tables(dataset: Dataset) -> dict[str, tuple[list[str], list[list]]]:
    fund = [["truth_id", "date", "code", "name", "business", "amount",
             "shares", "nav", "fee", "rounding", "source_tx_no"]]
    for i, r in enumerate(sorted(dataset.fund_rows, key=lambda x: x["date"]), 1):
        fund.append([f"FD{i:06d}", r["date"], r["code"], r["name"], r["business"],
                     f"{r['amount']:.2f}", f"{r['shares']:.2f}", f"{r['nav']:.4f}",
                     f"{r['fee']:.2f}", r["rounding"], f"FD{r['date'].replace('-', '')}{i:06d}"])
    broker = [["truth_id", "date", "code", "name", "side", "qty", "price",
               "amount", "fee", "pnl"]]
    for i, r in enumerate(sorted(dataset.broker_rows, key=lambda x: x["date"]), 1):
        broker.append([f"BR{i:06d}", r["date"], r["code"], r["name"], r["side"],
                       str(r["qty"]), f"{r['price']:.2f}", f"{r['amount']:.2f}",
                       f"{r['fee']:.2f}", r["pnl"]])
    wealth = [["truth_id", "date", "amount", "balance", "note"]]
    for i, r in enumerate(dataset.wealth_rows, 1):
        wealth.append([f"WL{i:06d}", r["date"], f"{r['amount']:.2f}",
                       f"{r['balance']:.2f}", r["note"]])
    yield_ = [["truth_id", "date", "product", "amount", "balance"]]
    for i, r in enumerate(dataset.wechat_yield_rows, 1):
        yield_.append([f"WY{i:06d}", r["date"], r["product"],
                       f"{r['amount']:.2f}", f"{r['balance']:.2f}"])
    snap = [["truth_id", "date", "account", "name", "market_value", "cost",
             "pnl", "pnl_pct", "cash", "realized", "total_pnl"]]
    for i, r in enumerate(dataset.snapshots, 1):
        snap.append([f"SN{i:06d}", r["date"], r["account"], r["name"],
                     f"{r['market_value']:.2f}", f"{r['cost']:.2f}",
                     f"{r['pnl']:.2f}", f"{r['pnl_pct']:.2f}", r["cash"], r["realized"], r["total_pnl"]])
    for table, source in ((fund, sorted(dataset.fund_rows, key=lambda r: r["date"])),
                          (broker, sorted(dataset.broker_rows, key=lambda r: r["date"])),
                          (wealth, dataset.wealth_rows), (yield_, dataset.wechat_yield_rows),
                          (snap, dataset.snapshots)):
        table[0].append("event_id")
        for dest, entry in zip(table[1:], source, strict=True):
            dest.append(entry["event_id"])
    return {
        "ground_truth_fund.csv": fund,
        "ground_truth_broker.csv": broker,
        "ground_truth_wealth.csv": wealth,
        "ground_truth_wechat_yield.csv": yield_,
        "ground_truth_snapshots.csv": snap,
    }


def _label(p: Posting) -> str:
    """收益条目自带日期，汇总时要压成稳定的项目名。"""
    if "收益发放" in p.item:
        return "余额宝收益" if p.item.startswith("余额宝") else "理财收益"
    return p.item


def _bucket(p: Posting) -> str:
    """把几百个具体菜名/商品名压成十几类，答案文档才能是「简要版」。"""
    item = p.item
    if p.kind == "transfer" and p.expense_class == "弹性支出":
        return "亲友转账/红包"
    if p.order_channel == "meituan":
        return "外卖/到店"
    if p.order_channel in {"taobao", "tmall", "jd"}:
        return "网购"
    if item in D.BREAKFAST_ITEMS or item in D.LUNCH_ITEMS or item in D.DINNER_ITEMS:
        return "日常餐食"
    if item in D.COFFEE_ITEMS or item == "下午茶":
        return "咖啡下午茶"
    if item in D.DINING_OUT_ITEMS or item in {"朋友聚餐", "家庭聚餐", "同事聚会", "生日宴"}:
        return "聚餐"
    if item in D.GROCERY_ITEMS or item in {
            "零食饮料", "水果拼盘", "酸奶", "面包", "早餐补给", "饮料",
            "日用品", "电池/纸巾"}:
        return "生鲜日用"
    if item in D.TRANSPORT_ITEMS:
        return "通勤交通"
    if item in D.MEDICAL_ITEMS:
        return "医疗"
    if item in D.HAIRCUT_ITEMS:
        return "理发"
    if item in {"文具教辅", "校车月费", "兴趣班材料"}:
        return "子女教育"
    if item in TRAVEL_ITEMS or item.startswith("高铁票-"):
        return "旅行"
    if item in {"视频会员月费", "音乐会员月费", "云盘会员月费", "健身房月卡"}:
        return "订阅服务"
    return item


def _aggregate(postings: list[Posting], role: str, cls: str | None = None,
               *, bucket: bool = False) -> list[dict]:
    picked = [p for p in postings if p.flow_role == role
              and (cls is None or p.expense_class == cls)]
    groups: dict[tuple, dict] = {}
    for p in picked:
        label = _bucket(p) if bucket else _label(p)
        key = (p.expense_class, label) if bucket else (p.expense_class, label, p.cadence)
        g = groups.setdefault(key, {"class": p.expense_class, "item": label,
                                    "cadence": set() if bucket else p.cadence,
                                    "count": 0, "amount": Decimal("0.00")})
        if bucket:
            g["cadence"].add(p.cadence)
        g["count"] += 1
        g["amount"] += p.amount
    out = list(groups.values())
    for g in out:
        if isinstance(g["cadence"], set):
            g["cadence"] = g["cadence"].pop() if len(g["cadence"]) == 1 else "混合"
    return sorted(out, key=lambda g: (-g["amount"], g["item"]))


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(out)


def _money(v) -> str:
    return f"{Decimal(v):,.2f}"


def write_answer(dataset: Dataset, out_dir: Path, stats: dict) -> str:
    postings = dataset.postings
    income_rows = _aggregate(postings, "income", "收入")
    obligation_rows = _aggregate(postings, "primary", "义务支出")
    rigid_rows = _aggregate(postings, "primary", "刚性支出", bucket=True)
    flexible_rows = _aggregate(postings, "primary", "弹性支出", bucket=True)
    neutral_rows = _aggregate(postings, "settlement")
    asset_rows = _aggregate(postings, "asset_move")
    mirror_count = sum(1 for p in postings if p.flow_role == "mirror")
    refund_count = sum(1 for p in postings if p.flow_role == "refund")

    income_postings = sum((p.amount for p in postings if p.flow_role == "income"),
                          Decimal("0.00"))
    posting_invest = sum((p.amount for p in postings if p.flow_role == "income"
                          and p.category == "投资理财"), Decimal("0.00"))
    invest_income = posting_invest
    invest_income += sum((r["amount"] for r in dataset.wechat_yield_rows), Decimal("0.00"))
    invest_income += sum((r["amount"] for r in dataset.wealth_rows), Decimal("0.00"))
    salary_income = income_postings - posting_invest
    obligation_total = sum((p.amount for p in postings
                            if p.flow_role == "primary" and p.expense_class == "义务支出"),
                           Decimal("0.00"))
    rigid_total = sum((p.amount for p in postings
                       if p.flow_role == "primary" and p.expense_class == "刚性支出"),
                      Decimal("0.00"))
    flexible_total = sum((p.amount for p in postings
                          if p.flow_role == "primary" and p.expense_class == "弹性支出"),
                         Decimal("0.00"))
    asset_total = sum((p.amount for p in postings if p.flow_role == "asset_move"),
                      Decimal("0.00"))
    external_total = sum((p.amount for p in postings if p.flow_role == "external_inflow"),
                         Decimal("0.00"))

    periods = period_summary(dataset)
    yearly = [r for r in periods if r["granularity"] == "year"]
    half = [r for r in periods if r["granularity"] == "half"]
    quarter = [r for r in periods if r["granularity"] == "quarter"]
    monthly = [r for r in periods if r["granularity"] == "month"]
    daily = [r for r in periods if r["granularity"] == "day"]

    def period_rows(rows):
        return [[r["period"], _money(r["income"]), _money(r["obligation"]),
                 _money(r["rigid"]), _money(r["flexible"]),
                 _money(r["outflow"]), _money(r["refunds"]), _money(r["net_outflow"]), _money(r["net"])] for r in rows]

    # 每日视图：正文只放月内日均与峰值，逐日明细在 CSV。
    days_per_month: dict[str, list[dict]] = defaultdict(list)
    for r in daily:
        days_per_month[r["period"][:7]].append(r)
    daily_rows = []
    for month, rows in sorted(days_per_month.items()):
        n = len(rows)
        daily_rows.append([
            month, str(n),
            _money(sum(r["income"] for r in rows) / n),
            _money(sum(r["outflow"] for r in rows) / n),
            _money(max(rows, key=lambda r: r["outflow"])["outflow"]),
        ])
    peak_days = sorted(daily, key=lambda r: r["outflow"], reverse=True)[:5]
    peak_rows = [[r["period"], _money(r["outflow"]), _money(r["income"])]
                 for r in peak_days]

    def item_rows(rows, limit: int | None = None):
        picked = rows if limit is None else rows[:limit]
        return [[g["item"], g["cadence"], str(g["count"]), _money(g["amount"])]
                for g in picked]

    doc = f"""# Ground Truth 简要答案（{dataset.start} 至 {dataset.end}）

本文件由 `scripts/synth/truth.py` 从同一批账务事件生成，不是事后口述。
全部金额为人民币元，`seed={dataset.seed}`。

## 口径

- **收入**：工资、年终奖、产品利息/派发收益。退款不是收入；基金/理财
  赎回本金按资产划转处理，不重复算收入。
- **义务支出**：合同或法律上必须付的款——房贷、车贷、消费贷、信用贷、
  信用卡分期、保险、物业费、停车位租金。
- **刚性支出**：维持基本生活必须发生的支出——水电气宽带、通勤、基础餐食、
  生鲜日用、医疗、教育、理发、车辆保养。
- **弹性支出**：可延后或可取消的支出——咖啡、聚餐、外卖加餐、网购、
  服饰数码、旅行住宿、订阅、春节礼品、618/双11。
- **中性**：账户间划转、信用卡/花呗还款、投资本金搬运、配偶转入小荷包、
  退款冲减、关闭订单。它们不重复计入收入或支出。
- 信用卡账单和小荷包账单里的消费是**镜像行**，只在平台主流水计一次；
  本答案的 `flow_role=mirror` 共 {mirror_count} 行。

## 一、真实收入

{_md_table(["项目", "周期", "笔数", "期间金额"], item_rows(income_rows))}

- 工资性收入（含年终奖）：{_money(salary_income)}
- 投资理财收益：{_money(invest_income)}（余额宝/理财/零钱通/理财通/银行理财）
- **收入合计：{_money(salary_income + invest_income)}**
- 另有配偶每月转入家庭小荷包 {_money(external_total)}，属于家庭共同承担，
  不计入账主个人收入。历史税款退回 {_money(sum(r['tax_refund'] for r in yearly))}，也不算本期所得。
- 证券/基金的卖出损益及持仓浮盈亏列在投资账户，不计入日常预算收入。

## 二、义务支出

{_md_table(["项目", "周期", "笔数", "期间金额"], item_rows(obligation_rows))}

**义务支出合计：{_money(obligation_total)}**

## 三、刚性支出

{_md_table(["项目", "周期", "笔数", "期间金额"], item_rows(rigid_rows, 18))}

**刚性支出合计：{_money(rigid_total)}**

## 四、弹性支出

{_md_table(["项目", "周期", "笔数", "期间金额"], item_rows(flexible_rows, 18))}

**弹性支出合计：{_money(flexible_total)}**

## 五、中性/资金划转

- 账户间划转与还款：{len(neutral_rows)} 类，合计 {_money(sum((g['amount'] for g in neutral_rows), Decimal('0.00')))}
- 投资本金搬运（基金定投、理财申赎、余额宝/零钱通划转）：{_money(asset_total)}
- 退款：{refund_count} 笔，到账期冲减 {_money(sum(r['refunds'] for r in yearly))}；原交易日期和分类另保留。
- 应付未付、取消、授信余额和交易腿见事件表和核验证据；只有已结算业务计入收支。
- 各支出类的退款及净额见周期 CSV；月供支出含本金，资产负债口径另行拆分。
- 详细逐笔见 `ground_truth_postings.csv` 的 `flow_role` 列。

## 六、周期视图

### 年

{_md_table(["期间", "收入", "义务支出", "刚性支出", "弹性支出", "毛支出", "退款", "净支出", "收入－净支出"], period_rows(yearly))}

### 半年

{_md_table(["期间", "收入", "义务支出", "刚性支出", "弹性支出", "毛支出", "退款", "净支出", "收入－净支出"], period_rows(half))}

### 季

{_md_table(["期间", "收入", "义务支出", "刚性支出", "弹性支出", "毛支出", "退款", "净支出", "收入－净支出"], period_rows(quarter))}

### 月

{_md_table(["期间", "收入", "义务支出", "刚性支出", "弹性支出", "毛支出", "退款", "净支出", "收入－净支出"], period_rows(monthly))}

### 日

正文只列每月日均与峰值；逐日明细在 `ground_truth_periods.csv`。

{_md_table(["月份", "天数", "日均收入", "日均支出", "单日最高支出"], daily_rows)}

单日支出最高的 5 天：

{_md_table(["日期", "支出", "收入"], peak_rows)}

## 七、平台分类偏差（初始分类故意不准）

- 可比较的平台分类流水：{stats['classified']} 笔
- 被分错：{stats['mismatched']} 笔，**{stats['rate']:.1%}**
- 支付宝：{stats['alipay']} 笔中错 {stats['alipay_mismatched']} 笔
- 微信消费：{stats['wechat']} 笔中错 {stats['wechat_mismatched']} 笔
- 个体户/小商贩消费：{stats['individual_purchases']} 笔，其中
  {stats['individual_collapsed_to_merchant']} 笔被压成「其他/商户消费」等粗桶。

偏差是设计出来的，不是生成错误：平台按字面关键词和有无营业执照粗分，
个体户的真实品类被抹掉。详细逐笔见 `ground_truth_postings.csv` 的
`platform_category` / `platform_mismatch` 列。

## 八、如何还原原始流水

1. `ground_truth_postings.csv` 每行有 `tx_no`（平台交易单号）和
   `source_exports`（该笔出现在哪些原始文件）。可按单号查回导出行（不代表凭该行就能看出经济关系）。
2. 电商订单再按 `merchant_no` 对回订单 CSV；银行/信用卡按 `tx_no` 对回流水号。
3. 基金、证券、银行理财、微信收益、资产快照是独立事实表，分别见
   `ground_truth_fund.csv`、`ground_truth_broker.csv`、`ground_truth_wealth.csv`、
   `ground_truth_wechat_yield.csv`、`ground_truth_snapshots.csv`。
4. `reconciliation.csv` 是逐文件对账结果：每个原始导出文件的每一行都必须在
   答案里有且只有一条对应记录；`result` 全为 PASS 才算通过。
5. `ground_truth_events.csv` 和 `ground_truth_legs.csv` 保存生成器已知的资金来去；
   `ground_truth_evidence.csv` 区分直证、组合可核对、多解、缺证。缺证不计入盲测失败。
"""
    path = out_dir / "ANSWER.md"
    path.write_text(doc, encoding="utf-8")
    return doc


# ── 原始导出对账 ──────────────────────────────────────────────
def _s(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _dec(value) -> Decimal | str:
    text = _s(value)
    try:
        return Decimal(text)
    except ArithmeticError:
        return text


def _read_table(path: Path) -> tuple[list[str], list[dict]]:
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.active
            if hasattr(ws, "reset_dimensions"):
                ws.reset_dimensions()
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
    else:
        raw = path.read_bytes()
        text = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError(f"无法识别编码：{path}")
        rows = list(csv.reader(io.StringIO(text)))
    firsts = {"交易时间", "交易日期", "交易日", "订单号", "订单编号",
              "确认日期", "成交日期", "入账日期", "日期"}
    idx = None
    for i, row in enumerate(rows):
        if row and _s(row[0]) in firsts:
            idx = i
            break
    if idx is None:
        raise ValueError(f"找不到表头：{path}")
    header = [_s(x) for x in rows[idx]]
    data = []
    for row in rows[idx + 1:]:
        if not row or all(_s(x) == "" for x in row):
            continue
        if _s(row[0]).startswith("#"):
            continue
        if any(_s(x) for x in row[len(header):]) or len(row) < len(header):
            raise ValueError(f"列数异常：{path}")
        data.append(dict(zip(header, row)))
    return header, data


def _posting_truth_index(dataset: Dataset) -> tuple[dict[str, Posting], dict[tuple[str, str], Posting]]:
    by_tx: dict[str, Posting] = {}
    by_order: dict[tuple[str, str], Posting] = {}
    for p in dataset.postings:
        if p.tx_no in by_tx:
            raise AssertionError(f"交易单号重复：{p.tx_no}")
        by_tx[p.tx_no] = p
        if p.order_channel and p.kind in {"purchase", "closed"}:
            key = (p.order_channel, p.merchant_no)
            if key in by_order:
                raise AssertionError(f"订单号重复：{p.order_channel}/{p.merchant_no}")
            by_order[key] = p
    return by_tx, by_order


def _posting_source_index(dataset: Dataset) -> dict[str, list[Posting]]:
    out: dict[str, list[Posting]] = defaultdict(list)
    for p in dataset.postings:
        for name in source_exports(p, dataset):
            out[name].append(p)
    return out


def _raw_posting_fields(p: Posting) -> dict:
    date_s, time_s = p.when.strftime("%Y-%m-%d %H:%M:%S").split()
    direction = "不计收支" if p.direction == "中性" else p.direction
    amount = f"{p.amount:.2f}"
    if p.account == "alipay":
        return dict(zip([
            "交易时间", "交易分类", "交易对方", "对方账号", "商品说明", "收/支", "金额",
            "收/付款方式", "交易状态", "交易订单号", "商家订单号", "备注",
        ], [f"{date_s} {time_s}", p.platform_category or p.category, p.counterparty,
            p.counterparty_account, p.item, direction, amount, p.pay_method, p.status,
            p.tx_no, p.merchant_no, p.remark], strict=True))
    if p.account == "wechat":
        status = ("已全额退款" if p.refund_full else "已部分退款") if p.kind == "refund" else (
            p.status if p.platform_type else "已存入零钱" if p.kind == "income" else "支付成功")
        return dict(zip([
            "交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)", "支付方式",
            "当前状态", "交易单号", "商户单号", "备注",
        ], [f"{date_s} {time_s}", p.platform_category, p.counterparty, p.item,
            "/" if p.direction == "中性" else direction, amount, p.pay_method or "零钱",
            status, p.tx_no, p.merchant_no or "/", p.remark or "/"], strict=True))
    if p.account in WALLET_NAME:
        desc = {"transfer": "转入" if p.effect == "in" else "转出",
                "refund": "退款", "income": "收益发放"}.get(p.kind, "消费")
        return {"订单号": p.tx_no, "交易时间": f"{date_s} {time_s}", "交易说明": desc,
                "备注": p.remark or (f"{p.counterparty}-{p.item}" if p.kind == "purchase" else ""),
                "操作人昵称": D.PERSONA["name"], "操作人姓名": D.PERSONA["name"],
                "收入金额": amount if p.effect == "in" or p.direction == "收入" else "",
                "支出金额": amount if p.effect == "out" or p.direction == "支出" else ""}
    if p.account in _CREDIT_DIR:
        tx_type = ("退款" if p.kind == "refund" else "分期" if p.item == "信用卡分期还款"
                   else "还款" if p.kind == "transfer" and p.direction == "收入" else "消费")
        return {"交易日": date_s, "记账日": date_s, "交易类型": tx_type, "交易金额": amount,
                "卡号后四位": D.ACCOUNTS[p.account].mask[-4:], "商户名称": p.counterparty,
                "商户编号": D.merchant_id(p.merchant or p.counterparty), "交易描述": p.item,
                "交易流水号": p.tx_no}
    tx_type = {"transfer": "转账", "income": "收入", "investment": "投资理财",
               "refund": "退款", "purchase": "消费", "fee": "手续费"}[p.kind]
    return {"交易日期": date_s, "交易时间": time_s, "交易类型": tx_type,
            "支出金额": amount if p.effect == "out" else "", "收入金额": amount if p.effect == "in" else "",
            "对方户名": p.counterparty, "对方账号": D.party(p.counterparty).bank or "/",
            "摘要": p.item, "交易流水号": p.tx_no}


def _raw_order_fields(p: Posting) -> dict:
    at = p.when.strftime("%Y-%m-%d %H:%M:%S")
    amount = f"{p.amount:.2f}"
    closed = p.kind == "closed"
    pay = {"wechat": "微信支付", "alipay": "支付宝"}.get(p.account, "银行卡")
    store = p.merchant or p.counterparty
    merchant_id = D.merchant_id(store)
    if p.order_channel == "meituan":
        return {"订单号": p.merchant_no, "下单时间": at, "商家名称": store, "商家ID": merchant_id,
                "商品": p.item, "订单状态": "已完成", "订单金额": amount, "实付金额": amount,
                "支付方式": pay, "退款金额": f"{p.refunded:.2f}"}
    if p.order_channel == "jd":
        return {"订单号": p.merchant_no, "订单时间": at, "商品名称": p.item, "店铺": store,
                "店铺ID": merchant_id, "订单金额": amount, "实付金额": amount, "支付方式": pay,
                "订单状态": "已完成"}
    return {"订单编号": p.merchant_no, "订单创建时间": at, "商品名称": p.item, "店铺名称": store,
            "店铺ID": merchant_id, "订单状态": "交易关闭" if closed else "交易成功", "商品数量": "1",
            "订单金额": amount, "实付金额": "0.00" if closed else amount,
            "支付方式": "" if closed else pay, "运费": "0.00"}


_MONEY_FIELDS = {"金额", "金额(元)", "收入金额", "支出金额", "账户余额", "交易金额",
                 "订单金额", "实付金额", "退款金额", "运费"}


def _matches_fields(actual: dict, expected: dict) -> bool:
    if set(actual) != set(expected):
        return False
    for col, value in expected.items():
        if col in _MONEY_FIELDS and value != "":
            # Decimal equality tolerates Excel's 5.2 vs CSV's 5.20 without rounding away bad data.
            try:
                if Decimal(_s(actual[col])) != Decimal(value):
                    return False
            except ArithmeticError:
                return False
        elif _s(actual[col]) != value:
            return False
    return True


def _match_keyed(path: Path, rel: str, key_column: str, expected: dict) -> dict:
    _, rows = _read_table(path)
    matched = set()
    unmatched = 0
    for row in rows:
        key = _s(row.get(key_column))
        if key in expected and key not in matched and _matches_fields(row, expected[key]):
            matched.add(key)
        else:
            unmatched += 1
    return {"file": rel, "truth": "ground_truth_postings.csv", "key": key_column,
            "export_rows": len(rows), "truth_rows": len(expected), "matched": len(matched),
            "unmatched": unmatched, "truth_unmatched": len(expected) - len(matched),
            "result": "PASS" if not unmatched and len(matched) == len(expected) else "FAIL"}


def _match_multiset(path: Path, rel: str, truth_rows: list[tuple],
                    file_tuple) -> dict:
    _, rows = _read_table(path)
    file_counter = Counter(file_tuple(row) for row in rows)
    truth_counter = Counter(truth_rows)
    matched = sum((file_counter & truth_counter).values())
    unmatched = sum(file_counter.values()) - matched
    truth_unmatched = sum(truth_counter.values()) - matched
    return {
        "file": rel, "truth": rel, "key": "row_tuple",
        "export_rows": len(rows), "truth_rows": len(truth_rows),
        "matched": matched, "unmatched": unmatched,
        "truth_unmatched": truth_unmatched,
        "result": "PASS" if unmatched == 0 and truth_unmatched == 0 else "FAIL",
    }


def verify_ground_truth(dataset: Dataset, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    tag = f"{dataset.start:%Y%m%d}-{dataset.end:%Y%m%d}"
    _posting_truth_index(dataset)
    source_index = _posting_source_index(dataset)
    rows: list[dict] = []

    posting_files = [
        (f"支付宝/支付宝交易明细({tag}).csv", "交易订单号"),
        (f"微信/微信支付账单({tag}).xlsx", "交易单号"),
    ]
    for account in WALLET_NAME:
        posting_files.append(
            (f"支付宝小荷包/小荷包收支明细_{WALLET_NAME[account]}.xlsx", "订单号"))
    for rel in _BANK_DIR.values():
        posting_files.append((rel, "交易流水号"))
    for rel in _CREDIT_DIR.values():
        posting_files.append((rel, "交易流水号"))
    for rel, key in posting_files:
        expected = {}
        for p in sorted(source_index.get(rel, []), key=lambda p: (p.when, p.tx_no)):
            fields = _raw_posting_fields(p)
            if p.account in _BANK_DIR:
                if not expected:
                    balance = dataset.journal.accounts[p.account].opening
                balance += p.amount * {"in": 1, "out": -1, "none": 0}[p.effect]
                fields["账户余额"] = f"{balance:.2f}"
            expected[p.tx_no] = fields
        rows.append(_match_keyed(out_dir / rel, rel, key, expected))

    for rel, channels in (
            ("美团/美团订单明细.csv", ("meituan",)),
            ("淘宝天猫/淘宝天猫订单明细.csv", ("taobao", "tmall")),
            ("京东/京东订单明细.csv", ("jd",))):
        expected = [p for p in dataset.postings
                    if p.order_channel in channels and p.kind in {"purchase", "closed"}]
        fields = {p.merchant_no: _raw_order_fields(p) for p in expected}
        if len(fields) != len(expected):
            raise AssertionError(f"订单号重复：{rel}")
        key = "订单编号" if "taobao" in channels else "订单号"
        rows.append(_match_keyed(out_dir / rel, rel, key, fields))

    # 投资/收益/快照：没有单号，用整行元组做多重集对账。
    fund_truth = sorted(
        (_s(r["date"]), _s(r["code"]), _s(r["name"]), _s(r["business"]),
         _dec(r["amount"]), _dec(r["shares"]), _dec(r["nav"]),
         _dec(r["fee"]), f"FD{r['date'].replace('-', '')}{i:06d}")
        for i, r in enumerate(sorted(dataset.fund_rows, key=lambda r: r["date"]), 1))
    rows.append(_match_multiset(
        out_dir / "投资/天天基金_交易确认.csv", "投资/天天基金_交易确认.csv",
        fund_truth,
        lambda row: (_s(row["确认日期"]), _s(row["基金代码"]), _s(row["基金名称"]),
                     _s(row["业务类型"]), _dec(row["确认金额"]),
                     _dec(row["确认份额"]), _dec(row["确认净值"]),
                     _dec(row["手续费"]), _s(row["交易单号"]))))

    broker_truth = sorted(
        (_s(r["date"]), _s(r["code"]), _s(r["name"]), _s(r["side"]), _s(r["qty"]),
         _dec(r["price"]), _dec(r["amount"]), _dec(r["fee"]), _s(r["pnl"]))
        for r in dataset.broker_rows)
    rows.append(_match_multiset(
        out_dir / "投资/证券_成交明细.csv", "投资/证券_成交明细.csv",
        broker_truth,
        lambda row: (_s(row["成交日期"]), _s(row["证券代码"]), _s(row["证券名称"]),
                     _s(row["买卖方向"]), _s(row["成交数量"]), _dec(row["成交价格"]),
                     _dec(row["成交金额"]), _dec(row["手续费"]),
                     _s(row["实现盈亏"]))))

    wealth_truth = sorted(
        (_s(r["date"]), _dec(r["amount"]), _dec(r["balance"]), _s(r["note"]))
        for r in dataset.wealth_rows)
    rows.append(_match_multiset(
        out_dir / "投资/银行理财_收益明细.csv", "投资/银行理财_收益明细.csv",
        wealth_truth,
        lambda row: (_s(row["入账日期"]), _dec(row["收益金额"]),
                     _dec(row["账户余额"]), _s(row["备注"]))))

    yield_truth = sorted(
        (_s(r["date"]), _s(r["product"]), _dec(r["amount"]), _dec(r["balance"]))
        for r in dataset.wechat_yield_rows)
    rows.append(_match_multiset(
        out_dir / "投资/微信零钱通理财通收益明细.csv",
        "投资/微信零钱通理财通收益明细.csv", yield_truth,
        lambda row: (_s(row["日期"]), _s(row["产品"]),
                     _dec(row["收益金额"]), _dec(row["产品余额"]))))

    snap_truth = sorted(
        (_s(r["date"]), _s(r["account"]), _s(r["name"]),
         _dec(r["market_value"]), _dec(r["cost"]),
         _dec(r["pnl"]), _dec(r["pnl_pct"]), _dec(r["cash"]),
         _dec(r["realized"]), _dec(r["total_pnl"]))
        for r in dataset.snapshots)
    rows.append(_match_multiset(
        out_dir / f"投资/资产快照_{dataset.start.year}.csv",
        f"投资/资产快照_{dataset.start.year}.csv", snap_truth,
        lambda row: (_s(row["日期"]), _s(row["账户类型"]), _s(row["账户名称"]),
                     _dec(row["持仓市值"]), _dec(row["持仓成本"]),
                     _dec(row["浮动盈亏"]), _dec(row["浮动盈亏率(%)"]),
                     _dec(row["现金及已派发收益"]), _dec(row["已实现盈亏及收益"]),
                     _dec(row["合计盈亏及收益"]))))

    _write_csv(out_dir / "reconciliation.csv",
               ["file", "truth", "key", "export_rows", "truth_rows", "matched",
                "unmatched", "truth_unmatched", "result"],
               [[r["file"], r["truth"], r["key"], r["export_rows"], r["truth_rows"],
                 r["matched"], r["unmatched"], r["truth_unmatched"], r["result"]]
                for r in rows])

    total_export = sum(r["export_rows"] for r in rows)
    total_matched = sum(r["matched"] for r in rows)
    ok = all(r["result"] == "PASS" for r in rows)
    payload = {"ok": ok, "files": len(rows), "export_rows": total_export,
               "matched": total_matched, "rows": rows}
    (out_dir / "reconciliation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


# ── 总入口 ────────────────────────────────────────────────────
def _write_journal(dataset: Dataset, out: Path) -> None:
    from .verification import evidence, replay

    j = dataset.journal
    checks = replay(dataset)
    _write_csv(out / "ground_truth_actors.csv", ["actor", "name", "role"],
               [[k, *v] for k, v in sorted(j.actors.items())])
    _write_csv(out / "ground_truth_accounts.csv", ["account", "owner", "role", "opening", "closing"],
               [[k, a.owner, a.role, a.opening, j.cash[k]] for k, a in sorted(j.accounts.items())])
    _write_csv(out / "ground_truth_events.csv",
               ["event_id", "at", "kind", "item", "category", "expense_class", "cadence", "reference",
                "amount", "payer", "payee", "initiator", "status", "failure", "original_event", "funding_route", "credit"],
               [[e.id, e.at.isoformat(sep=" "), e.business.kind, e.business.item, e.business.category,
                 e.business.expense_class, e.business.cadence, e.business.reference, e.amount, e.payer, e.payee,
                 e.initiator, e.status, e.failure, e.original, ";".join(e.route), e.credit] for e in j.events])
    _write_csv(out / "ground_truth_legs.csv", ["event_id", "step", "from_account", "to_account", "amount", "purpose"],
               [[e.id, i, leg.source, leg.destination, leg.amount, leg.purpose]
                for e in j.events for i, leg in enumerate(e.legs, 1)])
    _write_csv(out / "ground_truth_state_changes.csv", ["event_id", "state", "delta"],
               [[e.id, c.key, c.delta] for e in j.events for c in e.changes])
    _write_csv(out / "ground_truth_evidence.csv", ["event_id", "fact", "evidence", "reason",
                                                  "candidates", "score_in_blind_test"], evidence(dataset))
    for name, (header, rows) in checks.items():
        _write_csv(out.parent / "verification" / "full_year" / name, header, rows)


def write_ground_truth(dataset: Dataset, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    gt = out_dir / "ground_truth"
    gt.mkdir(parents=True, exist_ok=True)
    annotate(dataset)

    _write_csv(gt / "ground_truth_postings.csv", POSTING_HEADER, _posting_rows(dataset))
    _write_journal(dataset, gt)
    for name, table in _truth_tables(dataset).items():
        _write_csv(gt / name, table[0], table[1:])

    periods = period_summary(dataset)
    _write_csv(gt / "ground_truth_periods.csv",
               ["granularity", "period", *PERIOD_TOTALS],
               [[r["granularity"], r["period"], *(r[col] for col in PERIOD_TOTALS)] for r in periods])

    stats = platform_bias_stats(dataset)
    _write_csv(gt / "platform_bias.csv",
               ["metric", "value"],
               [[k, f"{v:.4f}" if isinstance(v, float) else str(v)]
                for k, v in stats.items()])

    write_answer(dataset, gt, stats)
    recon = verify_ground_truth(dataset, out_dir)
    (gt / "ground_truth_summary.json").write_text(json.dumps({
        "seed": dataset.seed,
        "period": [dataset.start.isoformat(), dataset.end.isoformat()],
        "postings": len(dataset.postings),
        "platform_bias": stats,
        "reconciliation": {"ok": recon["ok"], "files": recon["files"],
                           "export_rows": recon["export_rows"],
                           "matched": recon["matched"]},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    if not recon["ok"]:
        failed = [r for r in recon["rows"] if r["result"] != "PASS"]
        raise AssertionError(f"原始流水对账失败：{failed[:3]}")
    return {"stats": stats, "reconciliation": recon}
