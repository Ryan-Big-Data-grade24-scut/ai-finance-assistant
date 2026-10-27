"""
backend/ledger/errors.py —— 账本引擎的统一错误

为什么要放在引擎层而不是边界层：
    前端的失败表现是「节点抖一下 + 一句人话」，
    MCP 的失败表现是「逐笔拒绝 + 理由」。
    两者要的是同一份文案，所以它必须在引擎里生成，
    边界层只做 code → HTTP status 的映射。

message 一律写成人话，不要写 INVALID_PARENT_SCOPE 这种机器码。
"""
from __future__ import annotations


class Code:
    """错误码集中在这里，避免魔法字符串散落各处。"""

    LEDGER_ERROR = "LEDGER_ERROR"

    # ---- 找不到 ----
    BOOK_NOT_FOUND = "BOOK_NOT_FOUND"
    NODE_NOT_FOUND = "NODE_NOT_FOUND"
    TX_NOT_FOUND = "TX_NOT_FOUND"

    # ---- 作用域不对 ----
    TX_NOT_IN_BOOK = "TX_NOT_IN_BOOK"
    CROSS_BOOK = "CROSS_BOOK"
    SAME_PARENT_REQUIRED = "SAME_PARENT_REQUIRED"

    # ---- 操作不适用 ----
    SPLIT_NOT_APPLICABLE = "SPLIT_NOT_APPLICABLE"
    TX_ALREADY_SPLIT = "TX_ALREADY_SPLIT"
    TX_NOT_SPLIT = "TX_NOT_SPLIT"
    SPLIT_ON_NET = "SPLIT_ON_NET"

    # ---- 参数问题 ----
    EMPTY_NODE_NAME = "EMPTY_NODE_NAME"
    INVALID_OP = "INVALID_OP"
    INVALID_DATE_RANGE = "INVALID_DATE_RANGE"

    # ---- 账不平 ----
    INVARIANT_BROKEN = "INVARIANT_BROKEN"

    # ---- 历史状态 ----
    NOTHING_TO_UNDO = "NOTHING_TO_UNDO"
    NOTHING_TO_REDO = "NOTHING_TO_REDO"


class LedgerError(Exception):
    """引擎拒绝了一次操作。

    code   供程序判断（前端可据此决定要不要抖）
    message  给人看的一句话
    detail  附加信息（给人看的细节，不是给机器解析的）
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = Code.LEDGER_ERROR,
        http_status: int = 400,
        detail: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.detail = detail or {}

    def to_payload(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"LedgerError({self.code}: {self.message})"
