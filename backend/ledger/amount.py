"""基于分的整数计算，避免前端/SQLite float 的累积漂移。"""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP


def cents(value: object) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def money(value: int) -> float:
    return float(Decimal(value) / 100)


def net(e: int, i: int) -> tuple[int, int]:
    return (0, i - e) if i >= e else (e - i, 0)
