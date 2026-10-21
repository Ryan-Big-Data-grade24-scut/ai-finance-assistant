"""
backend/schemas.py —— 唯一契约

这份文件是整个项目的"单一真相源"：
    FastAPI 校验  ─┐
    OpenAPI /docs ─┼─→ 从这里自动生成
    MCP 工具 schema ┤
    前端 types.ts ─┘  （人工同步翻译，结构必须一一对应）

三条纪律：
    1. 形状（字段名/类型/必填）只在这里定义，不要在文档里重复
    2. 语义（单位/精度/空值/排序）在字段 description 里写清
    3. OverrideIn 永远不声明任何原始字段名 —— 这是"AI 改不了原始数据"的第一道防线
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ============================================================================
#  公共枚举
# ============================================================================

Period = Literal["week", "month", "year"]
TxType = Literal["expense", "income"]
TxCat = Literal["expense", "income", "neutral"]
Month = str
Week = str
Year = str

# 金额口径说明（全局约定，写在文档里，这里只做约束）
#   - 金额单位一律「元」，float，两位小数
#   - 服务端 round(x, 2) 之后再返回，前端不做二次四舍五入
#   - 占比/环比一律「百分数」，17.2 表示 17.2%，不是 0.172


# ============================================================================
#  1. 导入
# ============================================================================

class ImportPreviewResp(BaseModel):
    """导入预览 —— 用户上传文件后，先看这个再决定是否入库"""

    batch_id: str = Field(description="本次预览的批次号，确认入库时带上")
    platform_id: str = Field(description="识别出的平台，如 'wechat' / 'alipay' / 'meituan'")
    platform_name: str = Field(description="平台显示名，如 '微信支付'")
    confidence: float = Field(
        description="平台识别置信度 0~1。低于 0.6 时前端应弹窗让用户确认平台"
    )
    total: int = Field(description="解析到的总行数")
    by_status: dict[str, int] = Field(
        description="按原始状态分布，如 {'交易成功': 1980, '已关闭': 20}"
    )
    by_direction: dict[str, int] = Field(description="按收支方向分布")
    date_from: str = Field(description="最早交易日期 YYYY-MM-DD")
    date_to: str = Field(description="最晚交易日期 YYYY-MM-DD")
    amount_sum: float = Field(description="支出合计（元），仅作参考，不入库")
    sample: list[dict] = Field(description="前 5 行原始数据，供用户肉眼核对")
    warnings: list[str] = Field(
        description="解析告警，如 ['第 12 行金额列无法解析，已跳过']"
    )


class ImportCommitResp(BaseModel):
    """导入结果 —— 必须暴露 skipped，不允许静默吞掉重复"""

    batch_id: str
    inserted: int = Field(description="实际新写入的行数")
    skipped: int = Field(
        description="因 dedupe_key 命中而跳过的重复笔数。"
                    "⚠️ 兜底 key 有误判风险，这个数明显异常时用户需要人工核查"
    )
    failed: int = Field(description="解析失败被丢弃的行数")
    first_error: str | None = Field(
        description="第一条失败的行号与原因。导入为全事务，失败则整体回滚"
    )
    date_from: str
    date_to: str


class ImportHistoryResp(BaseModel):
    batches: list[dict] = Field(description="按时间倒序的导入批次")


# ============================================================================
#  2. 报告 —— 公共入参
# ============================================================================

class ReportReq(BaseModel):
    """所有报告接口的公共入参"""

    type: TxType = Field(description="expense 支出 / income 收入")
    period: Period = Field(description="week / month / year")
    value: str = Field(
        description="week → '2026-W38'；month → '2026-09'；year → '2026'",
        pattern=r"^\d{4}(-W\d{2}|-\d{2})?$",
        examples=["2026-09", "2026-W38", "2026"],
    )


# ============================================================================
#  3. 报告 —— 响应模型
# ============================================================================

class SummaryResp(BaseModel):
    """周期总览"""

    period: str = Field(description="实际使用的区间标识，如 '2026-09'")
    total: float = Field(description="本期合计（元，两位小数）")
    prev: float = Field(description="上期合计（元）")
    delta: float = Field(description="total - prev，可为负")
    rate: float | None = Field(
        description="环比百分比。17.2 表示 +17.2%。prev=0 时为 null，"
                    "前端显示 '—'，不要显示 0%"
    )
    count: int = Field(description="交易笔数，不是天数")


class DayPoint(BaseModel):
    date: str = Field(description="YYYY-MM-DD")
    amount: float = Field(description="当日合计（元）")
    count: int = Field(description="当日笔数")


class CalendarResp(BaseModel):
    """每日日历

    ⚠️ 约定：无消费的日期【不返回】（不是 amount=0），前端自己补零渲染
    """
    days: list[DayPoint] = Field(description="按 date 升序")


class TrendBar(BaseModel):
    label: str = Field(description="区间标识，如 '2026-09'")
    amount: float = Field(description="该区间合计（元）")
    count: int = Field(description="该区间笔数")


class TrendResp(BaseModel):
    """柱状图 + 均值线"""

    bars: list[TrendBar] = Field(
        description="按时间升序。⚠️ 当期未结束也照常返回（部分月数据）"
    )
    avg: float = Field(description="各 bar.amount 的算术平均（元）")
    n: int = Field(description="bar 的数量")


class MinorGroup(BaseModel):
    minor: str = Field(description="小类名。无细分时上游不会产生本项")
    amount: float = Field(description="小类合计（元）")
    pct: float = Field(description="占周期总额百分比，15.8 表示 15.8%")
    count: int = Field(description="小类笔数")
    tx_ids: list[int] = Field(
        description="该小类下的 tx_id 列表，供前端点击跳转明细。"
                    "上限 200 个，超出时前端改用 /report/transactions 过滤查询"
    )


class MajorGroup(BaseModel):
    major: str = Field(description="大类名。来源 manual_category > tx_type > '其他'")
    amount: float = Field(description="大类合计（元）")
    pct: float = Field(description="占周期总额百分比")
    count: int = Field(description="大类笔数")
    minors: list[MinorGroup] = Field(
        description="小类，按 amount 降序。无小类时为空列表，不是 null"
    )


class CategoryResp(BaseModel):
    """分类环形图 + 二级列表"""

    total: float = Field(description="周期总额（元），用于算环形图总圆")
    majors: list[MajorGroup] = Field(description="按 amount 降序")


class MethodGroup(BaseModel):
    pay_method: str = Field(description="支付方式原始字符串，未做归并")
    amount: float = Field(description="该方式合计（元）")
    count: int = Field(description="该方式笔数")


class MethodResp(BaseModel):
    groups: list[MethodGroup] = Field(description="按 amount 降序")


class RankItem(BaseModel):
    tx_id: int
    amount: float = Field(description="金额（元）")
    date: str = Field(description="YYYY-MM-DD")
    tx_time: str = Field(description="YYYY-MM-DD HH:MM:SS")
    counterparty: str = Field(description="交易对方，可能为空字符串")
    tx_type: str = Field(description="平台原始分类")
    source: str = Field(description="来源平台，如 '微信支付'")


class IncomeRankResp(BaseModel):
    items: list[RankItem] = Field(description="按 amount 降序，最多 50 条")


# ============================================================================
#  4. 明细列表
# ============================================================================

class TxItem(BaseModel):
    id: int
    source: str
    tx_time: str
    date: str
    direction: TxCat
    amount: float
    pay_method: str
    counterparty: str
    item: str
    category: str = Field(description="修正后的大类")
    subcat: str = Field(description="修正后的小类，可能是空字符串")
    is_exempt: bool
    funding: str
    note: str = Field(description="人工/AI 写的单笔备注")
    is_overridden: bool = Field(description="是否被修正过，用于 UI 打标")


class TxQuery(BaseModel):
    """明细查询 —— 所有筛选维度都在这里"""

    type: TxType
    period: Period
    value: str

    category: str | None = Field(default=None, description="按大类过滤")
    subcat: str | None = Field(default=None, description="按小类过滤")
    pay_method: str | None = Field(default=None, description="按支付方式精确匹配")
    source: str | None = Field(default=None, description="按来源平台过滤")
    keyword: str | None = Field(
        default=None,
        description="模糊搜索 counterparty / item / remark 三列",
    )
    date_from: str | None = Field(default=None, description="覆盖 period，指定起日")
    date_to: str | None = Field(default=None, description="覆盖 period，指定止日")
    include_exempt: bool = Field(
        default=False,
        description="是否包含被豁免的条目。默认 false —— 豁免的意义就是不参与统计",
    )
    page: int = Field(default=1, ge=1)
    size: int = Field(default=50, ge=1, le=200)


class PaginatedResp(BaseModel):
    total: int = Field(description="符合条件的总条数")
    page: int
    size: int
    items: list[TxItem] = Field(description="按 tx_time 降序")


class PendingResp(BaseModel):
    """未修正交易清单 —— MCP 的 list_pending_classification 专用"""

    total: int = Field(
        description="未修正交易的总数。items 被截断时也返回全量数，"
                    "AI 靠它判断要不要继续处理"
    )
    items: list[TxItem] = Field(description="按 tx_time 降序，服务端最多返回 50 条")


# ============================================================================
#  5. 修正 —— 写入口
# ============================================================================

class OverrideIn(BaseModel):
    """修正入参

    ⚠️⚠️ 这里【没有】任何原始字段名 —— amount / tx_time / direction 等
    一个都没有。这不是遗漏，是刻意的：
    MCP 客户端靠这个模型生成工具 schema，AI 从协议层面就传不进来。

    extra="forbid" 的作用：Pydantic 默认会【静默丢弃】未声明字段。
    如果不禁止，别人传 {"manual_category": "x", "amount": 99999} 会
    得到一个「成功」响应 —— 分类改了，金额被悄悄忽略，调用方以为生效了。
    禁止之后这类请求在入口就被拒，并明确指出是哪个字段非法。
    """

    model_config = ConfigDict(extra="forbid")

    manual_category: str | None = Field(default=None, description="手动大类")
    manual_subcat: str | None = Field(default=None, description="手动小类")
    is_exempt: bool = Field(default=False, description="true = 不计入消费/收入统计")
    exempt_reason: str | None = Field(default=None, description="豁免理由，给人看的")
    funding: str = Field(default="自有", description="自有 / 亲情卡 / 他人代付")
    note: str | None = Field(default=None, description="单笔备注")


class OverrideResp(BaseModel):
    tx_id: int
    manual_category: str | None = None
    manual_subcat: str | None = None
    is_exempt: bool = False
    exempt_reason: str | None = None
    funding: str = "自有"
    note: str | None = None
    updated_by: str = Field(description="'ui' 前端 / 'mcp' AI —— 可追溯谁改的")
    updated_at: str


class TxDetailResp(BaseModel):
    """单笔全貌 —— 原始 + 修正并排，方便用户核对"""

    raw: TxItem = Field(description="原始记录（永远只读）")
    override: OverrideResp | None = Field(
        description="修正记录。为 null 表示这笔从未被改过"
    )


# ============================================================================
#  样例数据 —— 队友对着这个看，比看文档快
#
#  ⚠️ 全部为虚构示例数据，不含任何真实账本信息。
#     格式、单位、精度、空值约定均为真，只是数字是编的。
# ============================================================================

EXAMPLE_SUMMARY = {
    "period": "2026-09",
    "total": 1900.00,
    "prev": 2800.00,
    "delta": -900.00,
    "rate": -32.1,          # -32.1 就是 -32.1%，不是 -0.321
    "count": 82,            # 笔数，不是天数
}

EXAMPLE_CALENDAR = {
    "days": [
        {"date": "2026-09-01", "amount": 720.50, "count": 8},
        {"date": "2026-09-02", "amount": 100.10, "count": 2},
        {"date": "2026-09-05", "amount": 300.74, "count": 4},
        {"date": "2026-09-06", "amount": 174.16, "count": 3},
    ]
    # ⚠️ 9/3、9/4 无消费 → 整个日期不出现，不是 amount=0
}

EXAMPLE_TREND = {
    "bars": [
        {"label": "2026-05", "amount": 2100.00, "count": 145},
        {"label": "2026-06", "amount": 3200.00, "count": 271},
        {"label": "2026-07", "amount": 1500.00, "count": 188},
        {"label": "2026-08", "amount": 2800.00, "count": 176},
        {"label": "2026-09", "amount": 1900.00, "count": 82},
    ],
    "avg": 2300.00,          # bars 里 5 个 amount 的算术平均 = 11500/5
    "n": 5,
}

EXAMPLE_CATEGORY = {
    "total": 1900.00,
    "majors": [
        {
            "major": "餐饮",
            "amount": 800.00,
            "pct": 42.1,
            "count": 45,
            "minors": [
                {"minor": "美团", "amount": 300.00, "pct": 15.8,
                 "count": 18, "tx_ids": [1, 5, 9]},
                {"minor": "某快餐", "amount": 200.00, "pct": 10.5,
                 "count": 10, "tx_ids": [23, 41]},
            ],
        },
        {
            # ⚠️ 各 major 的 pct 加起来 < 100，差额是「未匹配到分类」的部分
            "major": "购物",
            "amount": 150.00,
            "pct": 7.9,
            "count": 8,
            "minors": [],       # ⚠️ 无小类 → 空列表，不是 null
        },
    ],
}

EXAMPLE_METHOD = {
    "groups": [
        {"pay_method": "花呗", "amount": 500.00, "count": 6},
        {"pay_method": "储蓄卡(1234)", "amount": 300.00, "count": 1},
        {"pay_method": "亲情卡", "amount": 300.00, "count": 11},
        {"pay_method": "小荷包", "amount": 20.00, "count": 1},
    ]
}

EXAMPLE_INCOME_RANK = {
    "items": [
        {"tx_id": 7, "amount": 800.00, "date": "2026-09-06",
         "tx_time": "2026-09-06 14:49:00",
         "counterparty": "[亲友A]", "tx_type": "转账红包", "source": "支付宝"},
        {"tx_id": 19, "amount": 500.00, "date": "2026-09-12",
         "tx_time": "2026-09-12 23:24:00",
         "counterparty": "[亲友B]", "tx_type": "转账红包", "source": "支付宝"},
        # ⚠️ counterparty 为空字符串（有些交易拿不到对方名）
        {"tx_id": 3, "amount": 300.00, "date": "2026-09-10",
         "tx_time": "2026-09-10 19:16:00",
         "counterparty": "", "tx_type": "转账红包", "source": "微信支付"},
    ]
}
