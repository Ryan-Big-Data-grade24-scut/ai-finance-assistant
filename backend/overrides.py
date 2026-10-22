"""
backend/overrides.py —— 写修正层的唯一入口

设计定位
========
全项目【只有这里】能写 tx_overrides。任何入口（HTTP / MCP / 测试）
想改一笔交易的分类、豁免、资金来源、备注，都必须调用本模块的函数。

为什么必须唯一入口
====================
「原始数据 AI 改不了」是本项目的核心卖点。如果这段校验分散写在
main.py 的路由处理函数和 mcp/server.py 的工具函数里，早晚会漏一处 ——
而漏掉的那一处正好是 AI 走的路。

三道防线
========
① 协议层  schemas.OverrideIn 里没有任何原始字段名。
           MCP / HTTP 的 schema 从这里派生，AI 根本传不进来。
② 运行时  本模块的 FORBIDDEN_FIELDS 黑名单再挡一次。
③ 结构层  本模块不存在任何 UPDATE/DELETE transactions_raw 的代码路径。
           即使前两道被绕过，代码里也没有那条路。
"""
from __future__ import annotations

from typing import Any

from . import db
from .reports._common import row_to_tx_item, type_of
from .schemas import OverrideIn, OverrideResp, TxDetailResp

# ── 绝对不允许出现在修正请求里的字段 ──────────────────────
FORBIDDEN_FIELDS = frozenset({
    # 原始层字段
    "id", "source", "platform_id", "tx_time", "direction", "amount",
    "pay_method", "counterparty", "item", "tx_type", "status",
    "tx_no", "merchant_no", "remark",
    # 派生字段
    "date", "month", "year", "weekday", "hour",
    # 导入元信息
    "dedupe_key", "raw_json", "imported_at", "import_batch",
})


class ForbiddenFieldError(Exception):
    """试图修改原始字段 —— 本项目的招牌行为，不是 bug。"""

    def __init__(self, fields: list[str]):
        self.fields = fields
        super().__init__(f"原始字段不可修改: {', '.join(fields)}")


class TxNotFound(Exception):
    def __init__(self, tx_id: int):
        self.tx_id = tx_id
        super().__init__(f"交易不存在: {tx_id}")


# ── 读取 ───────────────────────────────────────────────────

_TX_SELECT = """
SELECT id, source, tx_time, date, direction, amount, pay_method,
       counterparty, item, category, subcat, is_exempt, funding,
       note, is_overridden
FROM v_tx WHERE id = ?
"""

_OVR_SELECT = """
SELECT tx_id, manual_category, manual_subcat, is_exempt,
       exempt_reason, funding, note, updated_by, updated_at
FROM tx_overrides WHERE tx_id = ?
"""


def _row_to_override(row) -> OverrideResp | None:
    if row is None:
        return None
    return OverrideResp(
        tx_id=row["tx_id"],
        manual_category=row["manual_category"],
        manual_subcat=row["manual_subcat"],
        is_exempt=bool(row["is_exempt"]),
        exempt_reason=row["exempt_reason"],
        funding=row["funding"] or "自有",
        note=row["note"],
        updated_by=row["updated_by"] or "",
        updated_at=row["updated_at"] or "",
    )


# ── 写入：唯一入口 ─────────────────────────────────────────

_UPSERT_SQL = """
INSERT INTO tx_overrides
  (tx_id, manual_category, manual_subcat, is_exempt,
   exempt_reason, funding, note, updated_by, updated_at)
VALUES (?,?,?,?,?,?,?,?, datetime('now'))
ON CONFLICT(tx_id) DO UPDATE SET
  manual_category = excluded.manual_category,
  manual_subcat   = excluded.manual_subcat,
  is_exempt       = excluded.is_exempt,
  exempt_reason   = excluded.exempt_reason,
  funding         = excluded.funding,
  note            = excluded.note,
  updated_by      = excluded.updated_by,
  updated_at      = datetime('now')
"""


def set_override(tx_id: int, data: OverrideIn,
                 updated_by: str) -> OverrideResp:
    """修改一笔交易的修正信息。这是全项目唯一写 tx_overrides 的地方。

    Args:
        tx_id:      transactions_raw.id
        data:       修正内容，模型里【没有】任何原始字段
        updated_by: 'ui'（前端）/ 'mcp'（AI）/ 'test'，用于追溯

    Raises:
        ForbiddenFieldError: 请求里混进了原始字段
        TxNotFound:          tx_id 在原始层不存在
    """
    if not isinstance(tx_id, int) or tx_id <= 0:
        raise TxNotFound(tx_id)

    payload: dict[str, Any] = data.model_dump(exclude_none=True)

    # ── 防线②：运行时黑名单 ──────────────────────────────
    bad = sorted(set(payload) & FORBIDDEN_FIELDS)
    if bad:
        raise ForbiddenFieldError(bad)

    conn = db.get_conn()
    try:
        # 必须存在于原始层
        exists = conn.execute(
            "SELECT 1 FROM transactions_raw WHERE id = ?", (tx_id,)
        ).fetchone()
        if exists is None:
            raise TxNotFound(tx_id)

        with db.transaction(conn) as c:
            # ── 防线③：这条 SQL 只碰 tx_overrides ──────────
            c.execute(_UPSERT_SQL, (
                tx_id,
                payload.get("manual_category"),
                payload.get("manual_subcat"),
                1 if payload.get("is_exempt") else 0,
                payload.get("exempt_reason"),
                payload.get("funding", "自有"),
                payload.get("note"),
                updated_by,
            ))
    finally:
        conn.close()

    return get_override(tx_id)


def get_override(tx_id: int) -> OverrideResp:
    """读一笔修正记录。从未被改过则返回空字段的 OverrideResp。"""
    conn = db.get_conn()
    try:
        row = conn.execute(_OVR_SELECT, (tx_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_override(row) or OverrideResp(tx_id=tx_id, updated_by="")


def get_tx_detail(tx_id: int) -> TxDetailResp:
    """单笔全貌：原始 + 修正并排，方便用户自己核对。"""
    conn = db.get_conn()
    try:
        raw = conn.execute(_TX_SELECT, (tx_id,)).fetchone()
        if raw is None:
            raise TxNotFound(tx_id)
        ovr = conn.execute(_OVR_SELECT, (tx_id,)).fetchone()
    finally:
        conn.close()
    return TxDetailResp(
        raw=row_to_tx_item(raw),
        override=_row_to_override(ovr),
    )


def clear_override(tx_id: int, updated_by: str = "ui") -> None:
    """删除一笔修正记录 —— 恢复到完全按原始数据计算的状态。

    只删 tx_overrides 一行，原始层永远不动。
    这是「撤销」的唯一方式。
    """
    conn = db.get_conn()
    try:
        with db.transaction(conn) as c:
            c.execute("DELETE FROM tx_overrides WHERE tx_id = ?", (tx_id,))
    finally:
        conn.close()
