"""V2 账本的统一错误。code 是稳定契约，message 给人看。"""
from __future__ import annotations

_NOT_FOUND = {'NODE_NOT_FOUND', 'GROUP_NOT_FOUND', 'TX_NOT_FOUND', 'NO_ROOT'}
_CONFLICT = {
    'SAME_PARENT_REQUIRED', 'ALREADY_GROUPED', 'ALREADY_SPLIT', 'NOT_SPLIT', 'CYCLE',
    'NOTHING_TO_UNDO', 'NOTHING_TO_REDO', 'INVARIANT_VIOLATION', 'NOT_IN_GROUP',
    'NOTHING_TO_DISSOLVE', 'NOTHING_TO_UNSPLIT', 'NOTHING_TO_SPLIT', 'JOIN_REQUIRES_TX',
    'MIRROR_HAS_NO_MANUAL',
}


class LedgerError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    @property
    def status(self) -> int:
        if self.code in _NOT_FOUND:
            return 404
        if self.code in _CONFLICT:
            return 409
        return 400
