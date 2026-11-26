"""
backend/importer/statements.py —— 银行卡 / 电商 / 投资 三类账单适配

为什么要有这一层：
    支付宝、微信、小荷包只能看到「钱包里发生了什么」，看不到工资卡、信用卡、
    电商订单和投资账户。只导这三家，账本就只剩消费，房贷、车贷、工资、
    基金定投全部缺失——这不是一本完整的账。

设计约定（与 base.py 一致）：
    1. 归一化是索引不是替代：原始整行仍进 raw_json。
    2. 状态为关闭/失败/未支付的行不入账。
    3. 「自家账户之间的挪动」和「资产形态转换」标中性/中性语义，
       避免和同一笔钱的消费副本重复计算；真正的消费与收入保持原样。

这些格式来自虚构合成账单，也尽量贴近各家的常见导出形态；真实银行的
表头可能不同，遇到无法识别的文件会明确报错，不做静默猜测。
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator

from .base import (
    ParseIssues,
    Row,
    clean,
    norm_amount,
    norm_time,
    should_drop,
)

# ── 信用卡账单里的交易类型 → 收支方向 ────────────────────────
# 消费/分期是真的花出去；退款是收回；还款只是把钱还给自己已经记过的消费，
# 再记一次就重复了，所以标中性。
_CREDIT_DIRECTION = {
    "消费": "支出",
    "分期": "支出",
    "分期付款": "支出",
    "退款": "收入",
    "退货": "收入",
    "冲正": "收入",
    "还款": "中性",
    "信用卡还款": "中性",
}

# ── 投资业务 → 收支方向 ──────────────────────────────────────
# 申购/买入是资产换个形态，不是消费；收益发放才是收入。
_FUND_DIRECTION = {
    "申购确认": "中性",
    "认购确认": "中性",
    "赎回确认": "收入",
    "转换转入": "中性",
    "转换转出": "中性",
}
_BROKER_DIRECTION = {"买入": "中性", "卖出": "中性", "申购": "中性", "赎回": "中性"}


# ── 读文件的小工具 ────────────────────────────────────────────

def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """读 CSV，返回 (前置说明行, 数据行字典)。

    真实银行导出前面常有若干 '#' 开头的说明行；编码多为 GB18030，
    也可能是 UTF-8 带 BOM。两种都试，谁先解开用谁。
    """
    path = Path(path)
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError(f"无法解码文件（既不是 UTF-8 也不是 GB18030）：{path.name}")

    preamble: list[str] = []
    lines = text.splitlines()
    header_index = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            preamble.append(stripped.lstrip("#").strip())
            continue
        header_index = i
        break
    if header_index is None:
        return preamble, []

    reader = csv.DictReader(lines[header_index:])
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        row = {
            (key or "").strip(): ("" if value is None else str(value))
            for key, value in raw_row.items()
            if key is not None
        }
        if any(value.strip() for value in row.values()):
            rows.append(row)
    return preamble, rows


def _row_value(row: dict[str, str], *names: str) -> str:
    """按候选列名顺序取值，取第一个非空的。"""
    for name in names:
        value = clean(row.get(name))
        if value:
            return value
    return ""


def _combine_date_time(date_text: str, time_text: str) -> str | None:
    date_text = clean(date_text)
    time_text = clean(time_text)
    if not date_text:
        return None
    # 有的导出把「日期 时间」写在一列里（例如电商订单时间），别再补一次时间。
    if " " in date_text:
        return norm_time(date_text.replace("T", " "))
    if not time_text:
        return f"{date_text} 00:00:00"
    time_text = time_text.split(" ")[-1]
    if len(time_text) == 5:
        time_text += ":00"
    return norm_time(f"{date_text} {time_text}")


# ── 银行卡（借记卡：储蓄卡 / 还款卡 / 生活缴费卡） ─────────────

def parse_bank_debit(path: Path, platform_id: str = "bank_generic",
                     issues: ParseIssues | None = None) -> Iterator[Row]:
    """借记卡流水：支出金额 / 收入金额 两列分开给，方向直接由列决定。

    「自家账户之间的转账」在这里仍然是支出/收入（这是账户层面的事实），
    是不是自己转给自己的，由账本分类层判断，不在解析层猜。
    """
    preamble, rows = _read_csv(path)
    for index, row in enumerate(rows, 1):
        tx_time = _combine_date_time(
            _row_value(row, "交易日期", "记账日期", "日期"),
            _row_value(row, "交易时间", "交易时间戳"),
        )
        if not tx_time:
            if issues:
                issues.add(index, "缺少交易日期")
            continue
        status = _row_value(row, "交易状态", "状态")
        if should_drop(status):
            continue
        out_amount = norm_amount(_row_value(row, "支出金额", "支出"))
        in_amount = norm_amount(_row_value(row, "收入金额", "收入"))
        if out_amount:
            amount, direction = out_amount, "支出"
        elif in_amount:
            amount, direction = in_amount, "收入"
        else:
            if issues:
                issues.add(index, "支出金额与收入金额均为空")
            continue
        yield Row(
            platform_id=platform_id,
            source=path.name,
            tx_time=tx_time,
            direction=direction,
            amount=amount,
            pay_method=_row_value(row, "交易类型", "业务类型"),
            counterparty=_row_value(row, "对方户名", "对方名称", "交易对手"),
            item=_row_value(row, "摘要", "交易摘要", "用途"),
            tx_type=_row_value(row, "交易类型", "业务类型"),
            status=status or "交易成功",
            tx_no=_row_value(row, "交易流水号", "流水号", "交易流水"),
            remark=_row_value(row, "对方账号", "对方账户"),
            raw=dict(row),
        )


# ── 信用卡账单 ────────────────────────────────────────────────

def parse_bank_credit(path: Path, platform_id: str = "bank_credit",
                      issues: ParseIssues | None = None) -> Iterator[Row]:
    """信用卡账单：只有一列金额，方向由「交易类型」决定。

    账单里通常只有交易日没有具体时间，时间缺失就记 00:00:00，
    不编造时间——编出来的时间会污染时段筛选。
    """
    preamble, rows = _read_csv(path)
    for index, row in enumerate(rows, 1):
        tx_time = _combine_date_time(
            _row_value(row, "交易日", "交易日期", "记账日", "日期"),
            _row_value(row, "交易时间"),
        )
        if not tx_time:
            if issues:
                issues.add(index, "缺少交易日")
            continue
        status = _row_value(row, "交易状态", "状态")
        if should_drop(status):
            continue
        amount = norm_amount(_row_value(row, "交易金额", "金额"))
        if not amount:
            if issues:
                issues.add(index, "交易金额为空")
            continue
        tx_type = _row_value(row, "交易类型", "业务类型")
        direction = _CREDIT_DIRECTION.get(tx_type, "支出")
        yield Row(
            platform_id=platform_id,
            source=path.name,
            tx_time=tx_time,
            direction=direction,
            amount=amount,
            pay_method="信用卡",
            counterparty=_row_value(row, "商户名称", "商户名", "对方户名"),
            item=_row_value(row, "交易描述", "商品说明", "摘要"),
            tx_type=tx_type,
            status=status or "交易成功",
            tx_no=_row_value(row, "交易流水号", "流水号"),
            merchant_no=_row_value(row, "商户编号", "商户号"),
            remark=_row_value(row, "卡号后四位", "卡号"),
            raw=dict(row),
        )


# ── 电商订单（美团 / 淘宝天猫 / 京东） ────────────────────────

def parse_ecommerce(path: Path, platform_id: str = "ecommerce",
                    issues: ParseIssues | None = None) -> Iterator[Row]:
    """电商订单：未支付/关闭的订单不入账，已支付的按实付金额记支出。

    订单金额和实付金额可能不同（优惠、运费）：账本记的是实付金额，
    订单金额留在 raw_json 里。
    """
    preamble, rows = _read_csv(path)
    for index, row in enumerate(rows, 1):
        status = _row_value(row, "订单状态", "状态")
        if should_drop(status) or status in ("未支付", "待支付"):
            continue
        tx_time = _combine_date_time(
            _row_value(row, "下单时间", "订单时间", "订单创建时间", "创建时间"),
            "",
        )
        if not tx_time:
            if issues:
                issues.add(index, "缺少下单时间")
            continue
        paid = norm_amount(_row_value(row, "实付金额", "实付款", "支付金额"))
        if not paid:
            if issues:
                issues.add(index, "实付金额为空")
            continue
        order_no = _row_value(row, "订单号", "订单编号", "交易单号")
        merchant = _row_value(row, "商家名称", "店铺名称", "店铺", "卖家名称")
        yield Row(
            platform_id=platform_id,
            source=path.name,
            tx_time=tx_time,
            direction="支出",
            amount=paid,
            pay_method=_row_value(row, "支付方式"),
            counterparty=merchant,
            item=_row_value(row, "商品", "商品名称", "商品名", "宝贝标题"),
            tx_type="电商订单",
            status=status or "交易成功",
            tx_no=order_no,
            merchant_no=_row_value(row, "商家ID", "店铺ID"),
            raw=dict(row),
        )


# ── 基金 / 证券 / 理财 ────────────────────────────────────────

def parse_fund(path: Path, platform_id: str = "fund",
               issues: ParseIssues | None = None) -> Iterator[Row]:
    """基金交易确认：申购/赎回是资产形态转换，不是消费。"""
    preamble, rows = _read_csv(path)
    for index, row in enumerate(rows, 1):
        tx_time = _combine_date_time(_row_value(row, "确认日期", "交易日期", "日期"), "")
        if not tx_time:
            if issues:
                issues.add(index, "缺少确认日期")
            continue
        amount = norm_amount(_row_value(row, "确认金额", "交易金额", "金额"))
        if not amount:
            if issues:
                issues.add(index, "确认金额为空")
            continue
        business = _row_value(row, "业务类型", "交易类型", "业务名称")
        direction = _FUND_DIRECTION.get(business, "中性")
        name = _row_value(row, "基金名称", "产品名称", "名称")
        yield Row(
            platform_id=platform_id,
            source=path.name,
            tx_time=tx_time,
            direction=direction,
            amount=amount,
            pay_method="基金",
            counterparty=name,
            item=f"{business}·{name}" if business else name,
            tx_type=business,
            status="交易成功",
            tx_no=_row_value(row, "交易单号", "订单号", "流水号"),
            remark=_row_value(row, "基金代码", "代码"),
            raw=dict(row),
        )


def parse_broker(path: Path, platform_id: str = "broker",
                 issues: ParseIssues | None = None) -> Iterator[Row]:
    """证券成交明细：买入卖出都是资产形态转换；盈亏不是现金流。"""
    preamble, rows = _read_csv(path)
    for index, row in enumerate(rows, 1):
        tx_time = _combine_date_time(_row_value(row, "成交日期", "交易日期", "日期"), "")
        if not tx_time:
            if issues:
                issues.add(index, "缺少成交日期")
            continue
        amount = norm_amount(_row_value(row, "成交金额", "发生金额", "金额"))
        if not amount:
            if issues:
                issues.add(index, "成交金额为空")
            continue
        side = _row_value(row, "买卖方向", "交易方向", "业务名称")
        name = _row_value(row, "证券名称", "产品名称", "名称")
        direction = _BROKER_DIRECTION.get(side, "中性")
        yield Row(
            platform_id=platform_id,
            source=path.name,
            tx_time=tx_time,
            direction=direction,
            amount=amount,
            pay_method="证券",
            counterparty=name,
            item=f"{side}·{name}" if side else name,
            tx_type=side,
            status="交易成功",
            tx_no=_row_value(row, "成交编号", "流水号", "合同编号"),
            remark=_row_value(row, "证券代码", "代码"),
            raw=dict(row),
        )


def parse_snapshot(path: Path, platform_id: str = "asset_snapshot",
                   issues: ParseIssues | None = None) -> Iterator[Row]:
    """资产快照：某一时点的持仓表，不是流水。

    强行按流水导入会把「持仓市值」当成支出，账会凭空多出一大笔开销。
    所以这里明确拒绝，并说明原因。
    """
    raise ValueError(
        f"{path.name} 是资产快照（某一时点的持仓表），不是交易流水，"
        "无法导入为交易。快照请在账本的资产视图里查看。"
    )


def parse_yield(path: Path, platform_id: str = "yield",
                issues: ParseIssues | None = None) -> Iterator[Row]:
    """理财/零钱通收益明细：这是收入。

    同一文件里可能混了多个产品（零钱通、理财通），用 product 做去重域，
    免得不同产品的同日同额收益互相覆盖。
    """
    preamble, rows = _read_csv(path)
    for index, row in enumerate(rows, 1):
        tx_time = _combine_date_time(_row_value(row, "日期", "入账日期", "收益日期"), "")
        if not tx_time:
            if issues:
                issues.add(index, "缺少日期")
            continue
        amount = norm_amount(_row_value(row, "收益金额", "金额", "收益"))
        if not amount:
            if issues:
                issues.add(index, "收益金额为空")
            continue
        product = _row_value(row, "产品", "产品名称", "账户名称")
        yield Row(
            platform_id=platform_id,
            source=path.name,
            tx_time=tx_time,
            direction="收入",
            amount=amount,
            pay_method="理财",
            counterparty=product,
            item=f"{product}收益" if product else "理财收益",
            tx_type="收益",
            status="交易成功",
            tx_no=f"{product}#{tx_time[:10]}" if product else "",
            remark=_row_value(row, "备注", "说明"),
            wallet_domain=product,
            raw=dict(row),
        )


# ── 平台识别 ──────────────────────────────────────────────────

# 借记卡：靠「支出金额 + 收入金额」两列判断
_DEBIT_COLUMNS = {"交易日期", "支出金额", "收入金额"}
# 信用卡：靠「交易日 + 商户名称」
_CREDIT_COLUMNS = {"交易日", "商户名称", "交易流水号"}
# 电商：靠订单号 + 实付金额
_ECOMMERCE_COLUMNS = {"实付金额"}
# 投资
_FUND_COLUMNS = {"基金代码", "确认份额"}
_BROKER_COLUMNS = {"证券代码", "成交数量"}
_YIELD_COLUMNS = {"收益金额"}

# 机构 → 平台 ID（读前置说明里的银行名，读不到就退回文件名）
_INSTITUTIONS = [
    ("招商银行个人账户", "bank_cmb_savings"),
    ("招商银行储蓄卡", "bank_cmb_savings"),
    ("招商银行信用卡", "bank_cmb_credit"),
    ("中国工商银行", "bank_icbc"),
    ("工商银行", "bank_icbc"),
    ("中国建设银行", "bank_ccb"),
    ("建设银行", "bank_ccb"),
    ("广发银行", "bank_cgb_credit"),
]


def _platform_from_preamble(preamble: list[str], filename: str) -> str | None:
    text = " ".join(preamble) + " " + filename
    for keyword, platform_id in _INSTITUTIONS:
        if keyword in text:
            return platform_id
    return None


def detect_csv_platform(path: Path) -> str | None:
    """按 CSV 表头 + 前置说明判断平台；认不出来返回 None。

    文件名只作为最后的补充证据：真实导出常常把账户类型写进文件名，
    但表头证据优先。
    """
    path = Path(path)
    try:
        preamble, rows = _read_csv(path)
    except Exception:
        return None
    if not rows:
        return None
    columns = set(rows[0].keys())

    if _DEBIT_COLUMNS.issubset(columns):
        return _platform_from_preamble(preamble, path.name) or "bank_debit"
    if _CREDIT_COLUMNS.issubset(columns):
        return _platform_from_preamble(preamble, path.name) or "bank_credit"
    if _FUND_COLUMNS.issubset(columns):
        return "fund"
    if _BROKER_COLUMNS.issubset(columns):
        return "broker"
    if "收益金额" in columns and ("产品余额" in columns or "账户余额" in columns):
        return "yield"
    if _ECOMMERCE_COLUMNS.issubset(columns):
        if "订单编号" in columns:
            return "taobao"
        if "商品" in columns and "商家名称" in columns:
            return "meituan"
        if "店铺" in columns:
            return "jd"
        return "ecommerce"
    # 支付宝主账单（GB18030 CSV）：交易时间 + 收/支 + 金额。
    if {"交易时间", "收/支", "金额"}.issubset(columns):
        return "alipay"
    # 资产快照：持仓表，不是流水，单独识别以便给出清楚的提示。
    if {"账户类型", "持仓市值"}.issubset(columns):
        return "asset_snapshot"
    return None
