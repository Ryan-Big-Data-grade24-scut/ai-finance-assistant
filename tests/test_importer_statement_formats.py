"""银行卡 / 电商 / 投资 账单格式的回归测试。

全部使用虚构数据，不含任何真实账单内容。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import importer
from backend.importer.base import ParseIssues


# ── 虚构样本 ──────────────────────────────────────────────────

DEBIT_CSV = """\
# 招商银行个人账户交易明细（虚构样本）
# 账号：6225 **** **** 0000
# 查询期间：20260101-20261125
交易日期,交易时间,交易类型,支出金额,收入金额,账户余额,对方户名,对方账号,摘要,交易流水号
2026-01-08,09:00:02,转账,2900.00,,42100.00,某某汽车金融,/,汽车贷款月供,D20260108001
2026-01-10,10:05:13,收入,,28000.00,70100.00,某某科技有限公司,**** 0001,工资,D20260110002
2026-01-10,12:30:43,转账,2300.00,,67800.00,支付宝,/,转账,D20260110003
"""

CREDIT_CSV = """\
# 招商银行信用卡交易明细（虚构样本）
# 卡号：4392 **** **** 0000
# 账单周期：2026-01-01 至 2026-11-25
交易日,记账日,交易类型,交易金额,卡号后四位,商户名称,商户编号,交易描述,交易流水号
2026-01-01,2026-01-01,消费,37.27,0000,某某咖啡,M0001,拿铁,C20260101001
2026-01-02,2026-01-02,还款,5000.00,0000,本人还款,/,信用卡还款,C20260102002
2026-01-03,2026-01-03,退款,12.00,0000,某某咖啡,M0001,退款,C20260103003
2026-01-04,2026-01-04,分期,240.00,0000,某某书店,M0002,分期购买,C20260104004
"""

MEITUAN_CSV = """\
# 美团订单明细（虚构样本）
订单号,下单时间,商家名称,商家ID,商品,订单状态,订单金额,实付金额,支付方式,退款金额
M20260101001,2026-01-02 12:26:27,某某餐饮,M8142,外卖订单,已完成,89.84,89.84,支付宝,0.00
M20260101002,2026-01-03 19:02:11,某某服饰,M8143,服饰,交易关闭,120.00,0.00,,0.00
"""

TAOBAO_CSV = """\
# 淘宝/天猫订单明细（虚构样本）
订单编号,订单状态,订单创建时间,商品名称,店铺名称,店铺ID,商品数量,订单金额,实付金额,支付方式,运费
T20260101001,交易成功,2026-01-01 19:46:49,厨房小电,某某旗舰店,M4767,1,320.17,320.17,支付宝,0.00
T20260101002,交易关闭,2026-01-02 10:00:00,已取消商品,某某服饰店,M9981,1,141.16,0.00,,0.00
"""

FUND_CSV = """\
# 天天基金交易确认（虚构样本）
确认日期,基金代码,基金名称,业务类型,确认金额,确认份额,确认净值,手续费,交易单号
2026-01-15,000001,某某成长混合,申购确认,2500.00,1057.73,2.3600,3.75,FD20260115001
2026-03-16,000001,某某成长混合,赎回确认,1492.50,497.50,3.0000,7.50,FD20260316001
"""

BROKER_CSV = """\
# 证券成交明细（虚构样本）
成交日期,证券代码,证券名称,买卖方向,成交数量,成交价格,成交金额,手续费,实现盈亏
2026-02-18,000651,某某电器,买入,300,41.11,12333.00,5.00,
2026-04-06,601318,某某保险,卖出,200,64.62,12924.00,11.46,1518.54
"""

YIELD_CSV = """\
# 微信零钱通/理财通收益明细（虚构样本）
日期,产品,收益金额,产品余额
2026-01-01,理财通,32.50,15032.50
2026-01-01,零钱通,4.20,1204.20
"""

JD_CSV = """\
# 京东订单明细（虚构样本）
订单号,订单时间,商品名称,店铺,店铺ID,订单金额,实付金额,支付方式,订单状态
J20260114001,2026-01-14 21:12:37,零食大礼包,某某超市,M1595,178.60,178.60,微信支付,已完成
"""

SNAPSHOT_CSV = """\
# 资产快照（虚构样本）
日期,账户类型,账户名称,持仓市值,持仓成本,浮动盈亏,浮动盈亏率(%),现金及已派发收益,已实现盈亏及收益,合计盈亏及收益
2026-01-31,基金,某某成长混合,19843.26,19700.01,143.25,0.73,0.00,0.00,143.25
"""


def write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


# ── 识别 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("name,content,expected", [
    ("招商银行储蓄卡_交易明细.csv", DEBIT_CSV, "bank_cmb_savings"),
    ("招商银行信用卡_账单明细.csv", CREDIT_CSV, "bank_cmb_credit"),
    ("广发银行信用卡_账单明细.csv", CREDIT_CSV.replace("招商银行", "广发银行"), "bank_cgb_credit"),
    ("工商银行_交易明细.csv", DEBIT_CSV.replace("招商银行", "中国工商银行"), "bank_icbc"),
    ("建设银行_交易明细.csv", DEBIT_CSV.replace("招商银行", "中国建设银行"), "bank_ccb"),
    ("美团订单明细.csv", MEITUAN_CSV, "meituan"),
    ("淘宝天猫订单明细.csv", TAOBAO_CSV, "taobao"),
    ("京东订单明细.csv", JD_CSV, "jd"),
    ("天天基金_交易确认.csv", FUND_CSV, "fund"),
    ("证券_成交明细.csv", BROKER_CSV, "broker"),
    ("微信零钱通理财通收益明细.csv", YIELD_CSV, "yield"),
    ("资产快照_2026.csv", SNAPSHOT_CSV, "asset_snapshot"),
])
def test_detect_by_content(tmp_path: Path, name: str, content: str, expected: str):
    path = write(tmp_path, name, content)
    platform_id, _confidence = importer.detect(path)
    assert platform_id == expected


def test_detect_prefers_content_over_filename(tmp_path: Path):
    """文件名骗人时以表头为准。"""
    path = write(tmp_path, "支付宝.csv", DEBIT_CSV)
    platform_id, _ = importer.detect(path)
    assert platform_id == "bank_cmb_savings"


# ── 方向与金额 ────────────────────────────────────────────────

def test_bank_debit_direction_from_amount_columns(tmp_path: Path):
    path = write(tmp_path, "招商银行储蓄卡_交易明细.csv", DEBIT_CSV)
    rows = importer.parse_file(path, "bank_cmb_savings")
    assert [r.direction for r in rows] == ["支出", "收入", "支出"]
    assert [r.amount for r in rows] == [2900.00, 28000.00, 2300.00]
    assert rows[0].counterparty == "某某汽车金融"
    assert rows[0].item == "汽车贷款月供"
    assert rows[0].tx_no == "D20260108001"
    assert rows[0].tx_time == "2026-01-08 09:00:02"


def test_bank_credit_direction_by_transaction_type(tmp_path: Path):
    path = write(tmp_path, "招商银行信用卡_账单明细.csv", CREDIT_CSV)
    rows = importer.parse_file(path, "bank_cmb_credit")
    assert [r.direction for r in rows] == ["支出", "中性", "收入", "支出"]
    assert [r.amount for r in rows] == [37.27, 5000.00, 12.00, 240.00]
    # 账单只有日期没有时刻，不能编造时间
    assert all(r.tx_time.endswith("00:00:00") for r in rows)


def test_ecommerce_drops_closed_orders(tmp_path: Path):
    path = write(tmp_path, "美团订单明细.csv", MEITUAN_CSV)
    rows = importer.parse_file(path, "meituan")
    assert len(rows) == 1
    assert rows[0].amount == 89.84
    assert rows[0].counterparty == "某某餐饮"
    assert rows[0].merchant_no == "M8142"


def test_taobao_uses_paid_amount(tmp_path: Path):
    path = write(tmp_path, "淘宝天猫订单明细.csv", TAOBAO_CSV)
    rows = importer.parse_file(path, "taobao")
    assert len(rows) == 1
    assert rows[0].amount == 320.17


def test_fund_and_broker_are_asset_moves(tmp_path: Path):
    fund_rows = importer.parse_file(write(tmp_path, "天天基金_交易确认.csv", FUND_CSV), "fund")
    assert [r.direction for r in fund_rows] == ["中性", "收入"]
    assert [r.amount for r in fund_rows] == [2500.00, 1492.50]

    broker_rows = importer.parse_file(write(tmp_path, "证券_成交明细.csv", BROKER_CSV), "broker")
    # 买卖都是资产形态转换，实现盈亏不是现金流
    assert [r.direction for r in broker_rows] == ["中性", "中性"]
    assert [r.amount for r in broker_rows] == [12333.00, 12924.00]


def test_yield_rows_are_income_and_keep_product_domain(tmp_path: Path):
    path = write(tmp_path, "微信零钱通理财通收益明细.csv", YIELD_CSV)
    rows = importer.parse_file(path, "yield")
    assert [r.direction for r in rows] == ["收入", "收入"]
    # 不同产品同日同额不能互相覆盖
    assert len({r.dedupe_key for r in rows}) == 2


def test_asset_snapshot_is_rejected_with_reason(tmp_path: Path):
    path = write(tmp_path, "资产快照_2026.csv", SNAPSHOT_CSV)
    with pytest.raises(ValueError) as excinfo:
        importer.parse_file(path, "asset_snapshot")
    assert "资产快照" in str(excinfo.value)


# ── 解析问题要如实报告 ────────────────────────────────────────

def test_missing_amount_is_reported(tmp_path: Path):
    content = DEBIT_CSV + "2026-01-11,10:00:00,消费,,,100.00,某商户,/,某商品,D20260111001\n"
    path = write(tmp_path, "招商银行储蓄卡_交易明细.csv", content)
    issues = ParseIssues()
    rows = importer.parse_file(path, "bank_cmb_savings", issues=issues)
    assert len(rows) == 3
    assert issues.failed == 1
    assert "支出金额" in (issues.first_error or "")


def test_gbk_encoding_is_supported(tmp_path: Path):
    path = tmp_path / "招商银行储蓄卡_交易明细.csv"
    path.write_bytes(DEBIT_CSV.encode("gb18030"))
    rows = importer.parse_file(path, "bank_cmb_savings")
    assert len(rows) == 3
