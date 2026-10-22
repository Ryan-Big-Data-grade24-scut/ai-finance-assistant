"""
backend/reports/_common.py —— 报告层公共工具

只放「所有报告都需要」的东西，避免 7 个模块各写一遍。
"""
from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta

from ..schemas import TxItem

# 收支方向：前端 type 参数 → 库里 direction 字段
TYPE_TO_DIRECTION = {"expense": "支出", "income": "收入"}

# 库里存的是中文，对外 API 一律用英文枚举
# （必须和 ReportReq.type、URL 参数保持同一套词表，否则前端要写两遍映射）
DIRECTION_TO_TYPE = {
    "支出": "expense",
    "收入": "income",
    "中性": "neutral",
}


def direction_of(tx_type: str) -> str:
    return TYPE_TO_DIRECTION[tx_type]


def type_of(direction: str) -> str:
    return DIRECTION_TO_TYPE.get(direction, "neutral")


def round2(x: float | None) -> float:
    """金额统一两位小数。服务端四舍五入，前端不要再做。"""
    return round(x or 0.0, 2)


def pct(part: float, whole: float) -> float:
    """占比，返回百分数。分母为 0 时返回 0.0 而不是抛异常。"""
    return round(part / whole * 100, 1) if whole else 0.0


def period_bounds(period: str, value: str) -> tuple[str, str]:
    """把 '2026-09' / '2026-W38' / '2026' 展开成 (起, 止) 的闭区间日期。

    week 使用 ISO 周（周一为首日）。
    """
    if period == "month":
        y, m = int(value[:4]), int(value[5:7])
        last = _cal.monthrange(y, m)[1]
        return f"{value}-01", f"{value}-{last:02d}"

    if period == "year":
        return f"{value}-01-01", f"{value}-12-31"

    if period == "week":
        # 2026-W38 → 该周周一
        year_s, week_s = value.split("-W")
        d = date.fromisocalendar(int(year_s), int(week_s), 1)
        return d.isoformat(), (d + timedelta(days=6)).isoformat()

    raise ValueError(f"不支持的周期: {period}")


def where_clause(period: str, value: str, alias: str = "") -> tuple[str, dict]:
    """生成周期过滤的 SQL 片段。

    周和年按 date 范围过滤，月直接用 month 字段（走索引）。
    """
    p = f"{alias}." if alias else ""
    if period == "month":
        return f"{p}month = :period_value", {"period_value": value}
    start, end = period_bounds(period, value)
    return (
        f"{p}date BETWEEN :period_start AND :period_end",
        {"period_start": start, "period_end": end},
    )


def row_to_tx_item(row) -> TxItem:
    """v_tx 一行 → TxItem。

    报告层与 overrides.py 共用的唯一映射器 —— 两边各写一遍迟早对不上口径。
    """
    return TxItem(
        id=row["id"],
        source=row["source"],
        tx_time=row["tx_time"],
        date=row["date"],
        direction=type_of(row["direction"]),
        amount=row["amount"],
        pay_method=row["pay_method"] or "",
        counterparty=row["counterparty"] or "",
        item=row["item"] or "",
        category=row["category"] or "",
        subcat=row["subcat"] or "",
        is_exempt=bool(row["is_exempt"]),
        funding=row["funding"] or "自有",
        note=row["note"] or "",
        is_overridden=bool(row["is_overridden"]),
    )
