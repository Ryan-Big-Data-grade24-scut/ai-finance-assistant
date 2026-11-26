"""把内存里的账务事件写成各平台的「原始导出文件」。

三种格式完全对齐真实样本（支付宝 CSV、微信 XLSX、小荷包 XLSX）；
其余平台（银行、美团、淘宝、京东、基金、证券）没有真实样本可对照，
按常见导出字段生成，属于**近似格式**，文件头会标注。
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import openpyxl

from . import data as D
from .engine import Dataset, Posting, WALLET_DOMAINS, WALLET_NAME, money


# ── 通用 ──────────────────────────────────────────────────────
def _running(dataset: Dataset, account: str) -> list[tuple[Posting, Decimal]]:
    balance = dataset.journal.accounts[account].opening
    out = []
    for p in sorted(dataset.by_account(account), key=lambda x: (x.when, x.tx_no)):
        if p.effect == "out":
            balance -= p.amount
        elif p.effect == "in":
            balance += p.amount
        out.append((p, balance))
    return out


def _dir_label(direction: str) -> str:
    return "不计收支" if direction == "中性" else direction


def _iso(p: Posting) -> str:
    return p.when.strftime("%Y-%m-%d %H:%M:%S")


def _counterparty_account(p: Posting) -> str:
    return p.counterparty_account or D.party_account(p.counterparty)


def _write_csv(path: Path, rows: list[list], encoding: str = "utf-8-sig") -> None:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerows(rows)
    path.write_bytes(buf.getvalue().encode(encoding, errors="replace"))


# ── 支付宝 CSV（真实样本对齐）────────────────────────────────
ALIPAY_HEADER = ["交易时间", "交易分类", "交易对方", "对方账号", "商品说明", "收/支",
                 "金额", "收/付款方式", "交易状态", "交易订单号", "商家订单号", "备注"]


def write_alipay_csv(dataset: Dataset, path: Path) -> int:
    rows = sorted(dataset.by_account("alipay"), key=lambda p: (p.when, p.tx_no))
    lines: list[list[str]] = []

    def count(direction: str) -> tuple[int, Decimal]:
        picked = [p for p in rows if _dir_label(p.direction) == direction]
        return len(picked), sum((p.amount for p in picked), Decimal("0.00"))

    inc_n, inc_s = count("收入")
    exp_n, exp_s = count("支出")
    neu_n, neu_s = count("不计收支")

    lines.append(["-" * 84])
    lines.append(["导出信息："])
    lines.append([f"姓名：{D.PERSONA['name']}"])
    lines.append([f"支付宝账户：{D.PERSONA['alipay_account']}"])
    lines.append([f"起始时间：[{dataset.start:%Y-%m-%d} 00:00:00]    "
                  f"终止时间：[{dataset.end:%Y-%m-%d} 23:59:59]"])
    lines.append(["导出交易类型：[全部]"])
    lines.append([f"导出时间：[{dataset.end:%Y-%m-%d} 21:18:47]"])
    lines.append([f"共{len(rows)}笔记录"])
    lines.append([f"收入：{inc_n}笔 {inc_s:.2f}元"])
    lines.append([f"支出：{exp_n}笔 {exp_s:.2f}元"])
    lines.append([f"不计收支：{neu_n}笔 {neu_s:.2f}元"])
    lines.append([])
    lines.append(["特别提示："])
    notes = [
        "1.本回单仅供个人对账使用，不作为收款方发货依据。",
        "2.本回单内容以支付宝系统记录为准。",
        "3.如对本回单内容有疑问，请在导出后30天内联系支付宝客服。",
        "4.交易金额单位为人民币元。",
        "5.「不计收支」包含充值、提现、转账等不构成收支的交易。",
        "6.已关闭、已退款的交易仍会保留在明细中。",
        "7.本回单不含手续费明细。",
        "8.请妥善保管本回单，避免泄露个人信息。",
    ]
    lines.extend([n] for n in notes)
    lines.append([])
    lines.append(["-" * 24 + "支付宝（中国）网络技术有限公司  电子客户回单" + "-" * 24])
    lines.append(ALIPAY_HEADER)

    for p in rows:
        lines.append([
            _iso(p), p.platform_category or p.category,
            p.counterparty, _counterparty_account(p),
            p.item, _dir_label(p.direction), f"{p.amount:.2f}",
            p.pay_method, p.status, p.tx_no, p.merchant_no, p.remark,
        ])

    _write_csv(path, lines, encoding="gb18030")
    return len(rows)


# ── 微信 XLSX（真实样本对齐）──────────────────────────────────
WECHAT_HEADER = ["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)",
                 "支付方式", "当前状态", "交易单号", "商户单号", "备注"]


def write_wechat_xlsx(dataset: Dataset, path: Path) -> int:
    rows = sorted(dataset.by_account("wechat"), key=lambda p: (p.when, p.tx_no))
    inc = [p for p in rows if p.direction == "收入"]
    exp = [p for p in rows if p.direction == "支出"]
    neu = [p for p in rows if p.direction == "中性"]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "微信支付账单"
    ws.append(["微信支付账单明细"])
    ws.append([f"微信昵称：[{D.PERSONA['wechat_nickname']}]"])
    ws.append([f"起始时间：[{dataset.start:%Y-%m-%d} 00:00:00]  "
               f"终止时间：[{dataset.end:%Y-%m-%d} 23:59:59]"])
    ws.append(["导出类型：[全部]"])
    ws.append([f"导出时间：[{dataset.end:%Y-%m-%d} 21:24:09]"])
    ws.append([f"共{len(rows)}笔记录"])
    ws.append([f"收入：{len(inc)}笔 {sum((p.amount for p in inc), Decimal('0')):.2f}元"])
    ws.append([f"支出：{len(exp)}笔 {sum((p.amount for p in exp), Decimal('0')):.2f}元"])
    ws.append([f"中性交易：{len(neu)}笔 {sum((p.amount for p in neu), Decimal('0')):.2f}元"])
    ws.append(["注："])
    for note in (
        "1.本账单仅记录通过微信支付完成的交易。",
        "2.交易状态以支付时的系统记录为准。",
        "3.「/」表示该笔交易不计入收支。",
        "4.金额单位为人民币元。",
        "5.已退款交易会同时出现原交易与退款记录。",
        "6.请妥善保管本账单，避免泄露个人信息。",
    ):
        ws.append([note])
    ws.append(["-" * 22 + "微信支付账单明细列表" + "-" * 22])
    ws.append(WECHAT_HEADER)

    for p in rows:
        # 交易类型是微信自己的口径：转入零钱通、购买理财通、零钱提现等
        # 都与普通「商户消费」不同，不能只按 kind 推断。
        tx_type = p.platform_category or p.platform_type or {
            "transfer": "转账", "refund": "退款", "income": "其他",
        }.get(p.kind, "商户消费")
        if p.kind == "refund":
            status = "已全额退款" if p.refund_full else "已部分退款"
        elif p.platform_type:
            status = p.status or "支付成功"
        elif p.kind == "income":
            status = "已存入零钱"
        else:
            status = "支付成功"
        ws.append([
            p.when, tx_type, p.counterparty, p.item,
            "/" if p.direction == "中性" else p.direction,
            float(p.amount), p.pay_method or "零钱",
            status, p.tx_no, p.merchant_no or "/", p.remark or "/",
        ])

    _save_xlsx(wb, path)
    return len(rows)


# ── 小荷包 XLSX（真实样本对齐，含假尺寸）────────────────────
WALLET_HEADER = ["订单号", "交易时间", "交易说明", "备注", "操作人昵称", "操作人姓名",
                 "收入金额", "支出金额"]


def _save_xlsx(workbook, path, *, wallet=False):
    buf = io.BytesIO()
    workbook.save(buf)
    workbook.close()
    with zipfile.ZipFile(buf) as src, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "docProps/core.xml":
                data = re.sub(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", b"2026-01-01T00:00:00Z", data)
            if wallet and name == "xl/worksheets/sheet1.xml":
                data = re.sub(rb'<dimension ref="[^"]*"\s*/>', b'<dimension ref="A1"/>', data, count=1)
            dst.writestr(zipfile.ZipInfo(name), data, compress_type=zipfile.ZIP_DEFLATED)


def write_wallet_xlsx(dataset: Dataset, account: str, path: Path) -> int:
    rows = sorted(dataset.by_account(account), key=lambda p: (p.when, p.tx_no))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "小荷包收支明细"
    name = WALLET_NAME[account]
    ws.append(["支付宝小荷包余额收支明细"])
    ws.append([f"支付宝小荷包名称：[{name}]"])
    ws.append([f"支付宝小荷包账户ID：{WALLET_DOMAINS[account]}"])
    ws.append(["支付宝小荷包创建时间：[2026-01-01 00:00:00]"])
    ws.append([f"起始时间：[{dataset.start:%Y-%m-%d} 00:00:00]  "
               f"终止时间：[{dataset.end:%Y-%m-%d} 23:59:59]"])
    for line in ("特别提示：", "1.本明细仅记录小荷包内的资金变动。",
                 "2.转入、转出不计入个人收支统计。", "3.金额单位为人民币元。",
                 "备注：", "说明一：", "说明二："):
        ws.append([line])
    ws.append(WALLET_HEADER)

    for p in rows:
        if p.kind == "transfer":
            desc = "转入" if p.effect == "in" else "转出"
        elif p.kind == "refund":
            desc = "退款"
        elif p.kind == "income":
            desc = "收益发放"
        else:
            desc = "消费"
        income = p.amount if (p.direction == "收入" or p.effect == "in") else None
        expense = p.amount if (p.direction == "支出" or p.effect == "out") else None
        remark = p.remark or (f"{p.counterparty}-{p.item}"
                              if p.kind == "purchase" else "")
        ws.append([
            p.tx_no, p.when, desc, remark, D.PERSONA["name"], D.PERSONA["name"],
            float(income) if income is not None else "",
            float(expense) if expense is not None else "",
        ])

    _save_xlsx(wb, path, wallet=True)
    return len(rows)


# ── 银行（近似格式）─────────────────────────────────────────
def _write_debit_csv(dataset: Dataset, account: str, path: Path, title: str) -> int:
    rows = _running(dataset, account)
    out = [[f"# {title}（虚构样本）"],
           [f"# 账号：{D.ACCOUNTS[account].mask}"],
           [f"# 查询期间：{dataset.start:%Y%m%d}-{dataset.end:%Y%m%d}"],
           ["交易日期", "交易时间", "交易类型", "支出金额", "收入金额", "账户余额",
            "对方户名", "对方账号", "摘要", "交易流水号"]]
    kind_label = {"transfer": "转账", "income": "收入", "investment": "投资理财",
                  "refund": "退款", "purchase": "消费", "fee": "手续费"}
    for p, balance in rows:
        out.append([
            p.when.strftime("%Y-%m-%d"), p.when.strftime("%H:%M:%S"),
            kind_label.get(p.kind, p.kind),
            f"{p.amount:.2f}" if p.effect == "out" else "",
            f"{p.amount:.2f}" if p.effect == "in" else "",
            f"{balance:.2f}", p.counterparty,
            D.party(p.counterparty).bank or "/", p.item, p.tx_no,
        ])
    _write_csv(path, out)
    return len(rows)


def write_cmb_debit_csv(dataset: Dataset, path: Path) -> int:
    return _write_debit_csv(dataset, "salary", path, "招商银行个人账户交易明细")


def write_icbc_csv(dataset: Dataset, path: Path) -> int:
    return _write_debit_csv(dataset, "mortgage_card", path, "中国工商银行账户交易明细")


def write_ccb_csv(dataset: Dataset, path: Path) -> int:
    return _write_debit_csv(dataset, "living_card", path, "中国建设银行账户交易明细")


def _write_credit_csv(dataset: Dataset, account: str, path: Path, title: str,
                      tail: str) -> int:
    rows = sorted(dataset.by_account(account), key=lambda p: (p.when, p.tx_no))
    out = [[f"# {title}（虚构样本）"],
           [f"# 卡号：{D.ACCOUNTS[account].mask}"],
           [f"# 账单周期：{dataset.start:%Y-%m-%d} 至 {dataset.end:%Y-%m-%d}"],
           ["交易日", "记账日", "交易类型", "交易金额", "卡号后四位", "商户名称",
            "商户编号", "交易描述", "交易流水号"]]
    for p in rows:
        if p.kind == "refund":
            tx_type = "退款"
        elif p.item == "信用卡分期还款":
            tx_type = "分期"
        elif p.kind == "transfer" and p.direction == "收入":
            tx_type = "还款"
        else:
            tx_type = "消费"
        out.append([
            p.when.strftime("%Y-%m-%d"), p.when.strftime("%Y-%m-%d"),
            tx_type, f"{p.amount:.2f}", tail, p.counterparty,
            D.merchant_id(p.merchant or p.counterparty), p.item, p.tx_no,
        ])
    _write_csv(path, out)
    return len(rows)


def write_cmb_credit_csv(dataset: Dataset, path: Path) -> int:
    return _write_credit_csv(dataset, "credit", path, "招商银行信用卡交易明细", "6620")


def write_cgb_credit_csv(dataset: Dataset, path: Path) -> int:
    return _write_credit_csv(dataset, "credit2", path, "广发银行信用卡交易明细", "1188")


# ── 电商订单（近似格式）────────────────────────────────────
def _order_rows(dataset: Dataset, channel: str) -> list[Posting]:
    return sorted([p for p in dataset.postings
                   if p.order_channel == channel and p.kind in {"purchase", "closed"}],
                  key=lambda p: (p.when, p.tx_no))


def _pay_label(p: Posting) -> str:
    if p.account == "wechat":
        return "微信支付"
    if p.account == "alipay":
        return "支付宝"
    return "银行卡"


def write_meituan_csv(dataset: Dataset, path: Path) -> int:
    rows = _order_rows(dataset, "meituan")
    out = [["# 美团订单明细（虚构样本，字段为近似格式）"],
           ["订单号", "下单时间", "商家名称", "商家ID", "商品", "订单状态", "订单金额",
            "实付金额", "支付方式", "退款金额"]]
    for p in rows:
        store = p.merchant or p.counterparty
        out.append([p.merchant_no, _iso(p), store, D.merchant_id(store), p.item,
                    "已完成", f"{p.amount:.2f}", f"{p.amount:.2f}",
                    _pay_label(p), f"{p.refunded:.2f}"])
    _write_csv(path, out)
    return len(rows)


def write_taobao_csv(dataset: Dataset, path: Path) -> int:
    rows = _order_rows(dataset, "taobao") + _order_rows(dataset, "tmall")
    rows.sort(key=lambda p: (p.when, p.tx_no))
    out = [["# 淘宝/天猫订单明细（虚构样本，字段为近似格式）"],
           ["订单编号", "订单状态", "订单创建时间", "商品名称", "店铺名称", "店铺ID",
            "商品数量", "订单金额", "实付金额", "支付方式", "运费"]]
    for p in rows:
        store = p.merchant or ("天猫超市" if p.order_channel == "tmall" else "淘宝店铺")
        out.append([p.merchant_no, "交易关闭" if p.kind == "closed" else "交易成功", _iso(p), p.item, store,
                    D.merchant_id(store), 1, f"{p.amount:.2f}",
                    "0.00" if p.kind == "closed" else f"{p.amount:.2f}",
                    "" if p.kind == "closed" else _pay_label(p), "0.00"])
    _write_csv(path, out)
    return len(rows)


def write_jd_csv(dataset: Dataset, path: Path) -> int:
    rows = _order_rows(dataset, "jd")
    out = [["# 京东订单明细（虚构样本，字段为近似格式）"],
           ["订单号", "订单时间", "商品名称", "店铺", "店铺ID", "订单金额", "实付金额",
            "支付方式", "订单状态"]]
    for p in rows:
        store = p.merchant or "京东自营"
        out.append([p.merchant_no, _iso(p), p.item, store, D.merchant_id(store),
                    f"{p.amount:.2f}", f"{p.amount:.2f}", _pay_label(p), "已完成"])
    _write_csv(path, out)
    return len(rows)


# ── 投资（近似格式）────────────────────────────────────────
def write_fund_csv(dataset: Dataset, path: Path) -> int:
    out = [["# 天天基金交易确认（虚构样本）"],
           ["确认日期", "基金代码", "基金名称", "业务类型", "确认金额", "确认份额",
            "确认净值", "手续费", "交易单号"]]
    for i, row in enumerate(sorted(dataset.fund_rows, key=lambda r: r["date"]), 1):
        out.append([row["date"], row["code"], row["name"], row["business"],
                    f"{row['amount']:.2f}", f"{row['shares']:.2f}",
                    f"{row['nav']:.4f}", f"{row['fee']:.2f}",
                    f"FD{row['date'].replace('-', '')}{i:06d}"])
    _write_csv(path, out)
    return len(dataset.fund_rows)


def write_broker_csv(dataset: Dataset, path: Path) -> int:
    out = [["# 证券成交明细（虚构样本）"],
           ["成交日期", "证券代码", "证券名称", "买卖方向", "成交数量", "成交价格",
            "成交金额", "手续费", "实现盈亏"]]
    for row in sorted(dataset.broker_rows, key=lambda r: r["date"]):
        out.append([row["date"], row["code"], row["name"], row["side"], row["qty"],
                    f"{row['price']:.2f}", f"{row['amount']:.2f}", f"{row['fee']:.2f}",
                    row["pnl"]])
    _write_csv(path, out)
    return len(dataset.broker_rows)


def write_wealth_csv(dataset: Dataset, path: Path) -> int:
    out = [["# 银行理财收益明细（虚构样本）"],
           ["入账日期", "收益金额", "账户余额", "备注"]]
    for row in dataset.wealth_rows:
        out.append([row["date"], f"{row['amount']:.2f}", f"{row['balance']:.2f}", row["note"]])
    _write_csv(path, out)
    return len(dataset.wealth_rows)


def write_snapshot_csv(dataset: Dataset, path: Path) -> int:
    out = [["# 月末资产快照（虚构样本）"],
           ["日期", "账户类型", "账户名称", "持仓市值", "持仓成本", "浮动盈亏", "浮动盈亏率(%)",
            "现金及已派发收益", "已实现盈亏及收益", "合计盈亏及收益"]]
    for row in dataset.snapshots:
        out.append([row["date"], row["account"], row["name"],
                    f"{row['market_value']:.2f}", f"{row['cost']:.2f}",
                    f"{row['pnl']:.2f}", f"{row['pnl_pct']:.2f}", f"{row['cash']:.2f}",
                    f"{row['realized']:.2f}", f"{row['total_pnl']:.2f}"])
    _write_csv(path, out)
    return len(dataset.snapshots)


# ── 平台内部收益明细 ─────────────────────────────────────────
def write_wechat_yield_csv(dataset: Dataset, path: Path) -> int:
    """微信支付账单不列零钱通/理财通收益，真实数据只在理财通收益明细里。"""
    out = [["# 微信零钱通/理财通收益明细（虚构样本，字段为近似格式）"],
           ["日期", "产品", "收益金额", "产品余额"]]
    for row in dataset.wechat_yield_rows:
        out.append([row["date"], row["product"], f"{row['amount']:.2f}",
                    f"{row['balance']:.2f}"])
    _write_csv(path, out)
    return len(dataset.wechat_yield_rows)


# ── 汇总 ──────────────────────────────────────────────────────
def write_summary(dataset: Dataset, path: Path, counts: dict[str, int]) -> None:
    from . import truth
    payload = {
        "schema_version": 3,
        "seed": dataset.seed,
        "period": [dataset.start.isoformat(), dataset.end.isoformat()],
        "digest": dataset.digest(),
        "row_counts": counts,
        "monthly_postings": dataset.monthly_counts(),
        "monthly_export_rows": dataset.monthly_export_rows(),
        "balances": {k: f"{v:.2f}" for k, v in dataset.balances.items()},
        "wallet_products": {k: f"{v:.2f}" for k, v in dataset.wallet_products.items()},
        "credit_used": f"{dataset.credit_used:.2f}",
        "credit_used_by": {k: f"{v:.2f}" for k, v in dataset.credit_used_by.items()},
        "huabei_used": f"{dataset.huabei_used:.2f}",
        "wechat_yield_rows": len(dataset.wechat_yield_rows),
        "platform_bias": truth.platform_bias_stats(dataset),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


README = """# 虚构账单数据集（v3）

本目录下全部为**程序生成的虚构数据**，人物、账户、商户、单号均为编造，
不含任何真实个人信息，也不对应任何真实账户。模型说明见 `scripts/synth/MODEL.md`。

## 账户拓扑

- 银行：招行工资卡、工行房贷卡、建行生活缴费卡；招行信用卡（8 万额度，
  5 日出账/23 日还款）、广发信用卡（3 万额度，10 日出账/28 日还款）。
- 支付宝：余额、余额宝（可付款、每天计息）、理财（不可直接付款）、花呗、
  亲情卡；小荷包分为家庭开销、旅行基金、教育金、自动攒四个。
- 微信：零钱、零钱通（可付款、每天计息）、理财通、亲属卡。
- 投资：天天基金、华泰证券、招行理财；保险按年缴。

付款方式按真实概率混合：支付宝以余额宝为主，其次是花呗、招行/广发信用卡、
账户余额、亲情卡和小荷包（付款方式写作「支付宝小荷包(家庭开销)」，同时
记入小荷包账单）；微信以零钱通为主，其次是零钱、信用卡和亲属卡。
余额宝/零钱通保留日常周转金额，低于保留额时改刷花呗/信用卡，并从理财赎回
补足，不会为了付款把理财账户清零。

## 周期规则

- 每年：1 月年终奖与春节支出；3/6/7/9/11 月各一笔年缴保险；4 月调薪与
  个税汇算；5/7/10 月三次旅行；6 月 618、11 月双 11；4/10 月车辆保养。
- 每季度：1/4/7/10 月物业费、停车位租金、子女学费。
- 每月：8 日车贷；10 日工资、花呗还款、小荷包定额转入、配偶转入；
  12 日消费贷/信用贷/信用卡分期；15 日房贷、基金定投、水电气与宽带话费；
  23 日招行信用卡还款；28 日广发信用卡还款。
- 每日/每周：餐饮、交通、日用、网购、外卖、医疗、理发等按概率与季节因子生成。

## 格式可信度

| 文件 | 可信度 | 说明 |
|---|---|---|
| 支付宝/支付宝交易明细*.csv | 高 | 对齐真实导出的表头、前置信息、对方账号与统计口径（GB18030） |
| 微信/微信支付账单*.xlsx | 高 | 对齐真实导出的表头与前 17 行结构；真实微信账单没有「对方账号」列 |
| 支付宝小荷包/小荷包收支明细_*.xlsx | 高 | 对齐真实导出的两列金额、12 行前置信息与假尺寸（A1） |
| 投资/微信零钱通理财通收益明细.csv | 低 | 微信支付账单不含零钱通/理财通收益，单独按常见收益明细近似 |
| 银行卡/*.csv | 低 | 未见过真实导出，字段按常见网银明细近似 |
| 美团/淘宝天猫/京东/*.csv | 低 | 未见过真实导出，字段按常见订单导出近似 |
| 投资/*.csv | 低 | 未见过真实导出，字段按常见基金/券商/理财明细近似 |

## 交易对方身份

- 支付宝导出的「对方账号」就在流水里，和真实导出一样逐行填写：平台内部
  交易为 `/`，商户/机构多为脱敏邮箱，个人多为脱敏手机号；同名交易对方
  永远对应同一个账号。
- 微信官方账单**没有「对方账号」列**（真实导出同样没有），这是平台限制，
  不是生成遗漏；微信侧靠「交易对方」名称和「商户单号」区分。

## 账务约束

- 储蓄卡与钱包余额全程不为负；
- 两张信用卡分别计额度（8 万 / 3 万），各自按账单日/还款日结算；
- 花呗额度单独计算（2 万），每月 1 日出账、10 日还款；
- 余额宝/零钱通收益逐日计提，余额宝收益以「不计收支」进入支付宝账单，
  微信收益只进单独收益明细；
- 跨账户转账在两边成对出现，金额相等、时间相差不超过 1 分钟；
- 基金定投、赎回金额与份额按当日净值计算；
- 同一个 seed 重复生成，逐字节一致（见 summary.json 的 digest）。

## 答案层与平台分类偏差

`ground_truth/` 是与导出文件同源的答案包：逐笔 `ground_truth_postings.csv`、
按日/月/季/半年/年汇总的 `ground_truth_periods.csv`、基金/证券/银行理财/
微信收益/资产快照事实表，以及 `ANSWER.md`（收入、义务、刚性、弹性支出）。

平台导出的「交易分类 / 交易类型」**故意不是正确答案**：个体户小商贩被压成
「其他」或「商户消费」，找不到关键词的小店落进「其他」，个别字面词还会误判
品类（如「小米」→ 粮油）。错分比例见 `summary.json` 的 `platform_bias`，
逐行标记见 `ground_truth_postings.csv` 的 `platform_mismatch` 列。

`reconciliation.csv` 证明每一行原始导出都能唯一还原到答案；全部 PASS 才算
生成成功。`ground_truth/ANSWER.md` 只看答案口径，不看平台分类。大类与周期
定义见仓库里的 `scripts/synth/GROUND_TRUTH.md`。

## 已知与真实数据的差异

- 人物收入、家庭结构与消费水平是虚构设定，不代表任何真实用户；
- 银行、电商、投资类文件是近似格式，导入适配时需要以真实样本重新核对字段；
- 微信账单不写对方账号是真实限制，不是生成遗漏；
- 理财产品的申购/赎回频率与真实用户相比偏规律。
"""


def write_all(dataset: Dataset, out_dir: Path) -> dict[str, int]:
    from . import truth
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()):
        raise FileExistsError(f"输出目录非空：{out_dir}")
    truth.annotate(dataset)
    dirs = {
        "alipay": out_dir / "支付宝",
        "wechat": out_dir / "微信",
        "wallet": out_dir / "支付宝小荷包",
        "bank": out_dir / "银行卡",
        "meituan": out_dir / "美团",
        "taobao": out_dir / "淘宝天猫",
        "jd": out_dir / "京东",
        "invest": out_dir / "投资",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    tag = f"{dataset.start:%Y%m%d}-{dataset.end:%Y%m%d}"
    counts: dict[str, int] = {}

    counts["支付宝"] = write_alipay_csv(dataset, dirs["alipay"] / f"支付宝交易明细({tag}).csv")
    counts["微信支付"] = write_wechat_xlsx(dataset, dirs["wechat"] / f"微信支付账单({tag}).xlsx")
    for account in WALLET_NAME:
        counts[f"小荷包·{WALLET_NAME[account]}"] = write_wallet_xlsx(
            dataset, account, dirs["wallet"] / f"小荷包收支明细_{WALLET_NAME[account]}.xlsx")
    counts["招行储蓄卡"] = write_cmb_debit_csv(dataset, dirs["bank"] / "招商银行储蓄卡_交易明细.csv")
    counts["招行信用卡"] = write_cmb_credit_csv(dataset, dirs["bank"] / "招商银行信用卡_账单明细.csv")
    counts["广发信用卡"] = write_cgb_credit_csv(dataset, dirs["bank"] / "广发银行信用卡_账单明细.csv")
    counts["工行还款卡"] = write_icbc_csv(dataset, dirs["bank"] / "工商银行_交易明细.csv")
    counts["建行生活缴费卡"] = write_ccb_csv(dataset, dirs["bank"] / "建设银行_交易明细.csv")
    counts["美团"] = write_meituan_csv(dataset, dirs["meituan"] / "美团订单明细.csv")
    counts["淘宝天猫"] = write_taobao_csv(dataset, dirs["taobao"] / "淘宝天猫订单明细.csv")
    counts["京东"] = write_jd_csv(dataset, dirs["jd"] / "京东订单明细.csv")
    counts["天天基金"] = write_fund_csv(dataset, dirs["invest"] / "天天基金_交易确认.csv")
    counts["证券"] = write_broker_csv(dataset, dirs["invest"] / "证券_成交明细.csv")
    counts["银行理财"] = write_wealth_csv(dataset, dirs["invest"] / "银行理财_收益明细.csv")
    counts["微信收益"] = write_wechat_yield_csv(
        dataset, dirs["invest"] / "微信零钱通理财通收益明细.csv")
    counts["资产快照"] = write_snapshot_csv(dataset, dirs["invest"] / f"资产快照_{dataset.start.year}.csv")

    (out_dir / "README.md").write_text(README, encoding="utf-8")
    write_summary(dataset, out_dir / "summary.json", counts)
    # 答案层必须在导出之后生成：对账要逐行读回刚写出的原始文件。
    from . import truth
    truth.write_ground_truth(dataset, out_dir)
    return counts
