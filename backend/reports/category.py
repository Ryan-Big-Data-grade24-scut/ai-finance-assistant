"""backend/reports/category.py —— 组件 4：分类环形图 + 二级列表

大类来源优先级：人工明确覆盖 > Live Ledger 当前分类 > 平台原始分类（由 v_tx 视图统一提供）。
小类优先读取人工覆盖；没有人工覆盖时读取 Live Ledger 当前分类。

pct 的分母是【周期总额】，不是各大类之和，
所以各大类 pct 加起来可能小于 100，差额是未分类的部分。
"""
from __future__ import annotations

from .. import db
from ..schemas import CategoryResp, MajorGroup, MinorGroup
from ._common import direction_of, pct, round2, where_clause

_TX_ID_CAP = 200   # tx_ids 上限，超出前端改用 transactions 接口


def get_by_category(req) -> CategoryResp:
    clause, params = where_clause(req.period, req.value)
    params["direction"] = direction_of(req.type)

    conn = db.get_conn()
    try:
        total = round2(conn.execute(
            f"SELECT COALESCE(SUM(amount),0) a FROM v_tx_counted "
            f"WHERE direction = :direction AND {clause}", params
        ).fetchone()["a"])

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
                {**params, "c": m["category"], "s": m["subcat"],
                 "limit": _TX_ID_CAP},
            ).fetchall()
            tx_ids[(m["category"], m["subcat"])] = [r["id"] for r in ids]
    finally:
        conn.close()

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
