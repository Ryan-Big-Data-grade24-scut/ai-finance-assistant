"""原型专用错误类型。

一条错误 = 一个 code + 一句人话 + 可选的定位信息。
读写两侧共用同一份文案。
"""
from __future__ import annotations


class LedgerError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'LedgerError({self.code!r}, {self.message!r}, {self.detail!r})'
