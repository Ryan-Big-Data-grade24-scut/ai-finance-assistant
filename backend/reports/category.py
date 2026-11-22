"""backend/reports/category.py —— 组件 4：分类环形图 + 二级列表

分类口径优先读「账本代数 V2」的节点树（根 → 大类 → 小类），
尚未初始化的流水退回平台分类，避免金额在初始化前凭空消失。

pct 的分母是【周期总额】，不是各大类之和，
所以各大类 pct 加起来可能小于 100，差额是未分类的部分。
"""
from __future__ import annotations

from .. import db
from ..schemas import CategoryResp, MajorGroup, MinorGroup
from ._common import direction_of, pct, round2, where_clause
from .ledger_categories import category_breakdown, ledger_ready

_TX_ID_CAP = 200   # tx_ids 上限，超出前端改用 transactions 接口


def _platform_breakdown(conn, clause: str, params: dict, total: float) -> CategoryResp:
    """没有 V2 账本表时的兼容口径（平台分类 / 人工覆盖）。"""
    majors = conn.execute(
        "SELECT category, ROUND(SUM(amount),2) AS amount, COUNT(*) AS cnt "
        f"FROM v_tx_counted WHERE direction = :direction AND {clause} "
        "GROUP BY category ORDER BY amount DESC",
        params,
    ).fetchall()

    minors = conn.execute(
        "SELECT category, subcat, ROUND(SUM(amount),2) AS amount, COUNT(*) AS cnt "
        f"FROM v_tx_counted "
        f"WHERE direction = :direction AND {clause} AND subcat != '' "
        "GROUP BY category, subcat ORDER BY amount DESC",
        params,
    ).fetchall()

    tx_ids: dict[tuple, list[int]] = {}
    for m in minors:
        ids = conn.execute(
            "SELECT id FROM v_tx_counted "
            f"WHERE direction = :direction AND {clause} "
            "AND category = :c AND subcat = :s LIMIT :limit",
            {**params, "c": m["category"], "s": m["subcat"], "limit": _TX_ID_CAP},
        ).fetchall()
        tx_ids[(m["category"], m["subcat"])] = [r["id"] for r in ids]

    grouped: dict[str, list] = {}
    for m in minors:
        grouped.setdefault(m["category"], []).append(m)

    return CategoryResp(
        total=total,
        majors=[
            MajorGroup(
                major=r["category"],
                amount=round2(r["amount"]),
                pct=pct(r["amount"], total),
                count=r["cnt"],
                minors=[
                    MinorGroup(
                        minor=s["subcat"],
                        amount=round2(s["amount"]),
                        pct=pct(s["amount"], total),
                        count=s["cnt"],
                        tx_ids=tx_ids.get((r["category"], s["subcat"]), []),
                    )
                    for s in grouped.get(r["category"], [])
                ],
            )
            for r in majors
        ],
    )


def get_by_category(req) -> CategoryResp:
    clause, params = where_clause(req.period, req.value)
    params["direction"] = direction_of(req.type)

    conn = db.get_conn()
    try:
        total = round2(conn.execute(
            f"SELECT COALESCE(SUM(amount),0) a FROM v_tx_counted "
            f"WHERE direction = :direction AND {clause}", params
        ).fetchone()["a"])
        if ledger_ready(conn):
            return category_breakdown(conn, clause, params, total)
        return _platform_breakdown(conn, clause, params, total)
    finally:
        conn.close()
