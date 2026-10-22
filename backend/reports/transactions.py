"""backend/reports/transactions.py —— 组件 7：明细列表

所有跳转的承接页。筛选维度全在这里。

约定
====
include_exempt=False（默认）→ 被豁免的条目【不出现】。
    豁免的意义就是不参与统计。
date_from / date_to 【覆盖】period，不是叠加。
"""
from __future__ import annotations

from .. import db
from ..schemas import PaginatedResp, PendingResp
from ._common import direction_of, period_bounds, row_to_tx_item, type_of

PENDING_CAP = 50   # 待分类清单是「待办」不是全量导出，服务端硬收口


def query_transactions(q) -> PaginatedResp:
    w = ["1=1"]
    p: dict = {}

    if q.date_from or q.date_to:
        # 显式日期区间覆盖 period
        start = q.date_from or "0000-01-01"
        end = q.date_to or "9999-12-31"
        w.append("date BETWEEN :s AND :e")
        p["s"], p["e"] = start, end
    else:
        w.append("date BETWEEN :s AND :e")
        p["s"], p["e"] = period_bounds(q.period, q.value)

    w.append("direction = :direction")
    p["direction"] = direction_of(q.type)

    if q.category:
        w.append("category = :category")
        p["category"] = q.category
    if q.subcat:
        w.append("subcat = :subcat")
        p["subcat"] = q.subcat
    if q.pay_method:
        w.append("pay_method = :pm")
        p["pm"] = q.pay_method
    if q.source:
        w.append("source = :src")
        p["src"] = q.source
    if q.keyword:
        w.append("(counterparty LIKE :kw OR item LIKE :kw OR note LIKE :kw)")
        p["kw"] = f"%{q.keyword}%"
    if not q.include_exempt:
        w.append("is_exempt = 0")

    where = " AND ".join(w)
    offset = (q.page - 1) * q.size

    conn = db.get_conn()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM v_tx WHERE {where}", p
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT id, source, tx_time, date, direction, amount, pay_method, "
            "counterparty, item, category, subcat, is_exempt, funding, "
            "note, is_overridden "
            f"FROM v_tx WHERE {where} ORDER BY tx_time DESC, id DESC "
            "LIMIT :limit OFFSET :offset",
            {**p, "limit": q.size, "offset": offset},
        ).fetchall()
    finally:
        conn.close()

    return PaginatedResp(
        total=total, page=q.page, size=q.size,
        items=[row_to_tx_item(r) for r in rows],
    )


def list_pending(limit: int = 20) -> PendingResp:
    """未人工修正过的交易（is_overridden = 0），按 tx_time 降序。

    给 MCP 的 list_pending_classification 用 —— AI 帮用户分类时的候选清单。
    这份清单的口径与 HTTP 的 /api/report/transactions 完全一致（同查 v_tx）。
    """
    n = max(1, min(limit, PENDING_CAP))

    conn = db.get_conn()
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM v_tx WHERE is_overridden = 0"
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT id, source, tx_time, date, direction, amount, pay_method, "
            "counterparty, item, category, subcat, is_exempt, funding, "
            "note, is_overridden "
            "FROM v_tx WHERE is_overridden = 0 "
            "ORDER BY tx_time DESC, id DESC LIMIT :limit",
            {"limit": n},
        ).fetchall()
    finally:
        conn.close()

    return PendingResp(total=total, items=[row_to_tx_item(r) for r in rows])
