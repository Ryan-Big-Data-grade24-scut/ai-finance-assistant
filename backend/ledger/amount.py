"""
backend/ledger/amount.py —— 数值基础（纯函数，零 IO）

代数里的 Y = (e, i)：支出分量与收入分量。

为什么用 Decimal 而不是 float：
    全仓库其它地方用 float + round2()，那没问题。
    但引擎要跑「审计链必须闭合」这种恒等式断言 —— float 下
    1777.42 这类闭合会 flaky。所以引擎内部一律 Decimal，
    只在 HTTP 边界转 float（to_float 是唯一的出口）。

为什么 +₁ / +₂ 只在这里实现：
    全仓库不得再手写 `if i >= e`。净额化的规则只有一处，
    才能保证 I6（NET 幂等）在任何地方都成立。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

Q2 = Decimal("0.01")
ZERO = Decimal("0.00")


def d(x: float | int | str | Decimal) -> Decimal:
    """转 Decimal。走 str() 中转，避免 float 的二进制误差。"""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def q2(x: Decimal) -> Decimal:
    """两位小数，四舍五入。"""
    return d(x).quantize(Q2, rounding=ROUND_HALF_UP)


def to_float(x: Decimal) -> float:
    """Decimal → float。HTTP 边界的唯一出口。"""
    return float(q2(x))


@dataclass(frozen=True, slots=True)
class Y:
    """结果 Y = (e, i)：支出分量与收入分量。代数 §1.3。

    e  支出（花了多少）
    i  收入（进了多少）
    净差 i - e 是派生量，随时可算，不单独存储。
    """

    e: Decimal = ZERO
    i: Decimal = ZERO

    # ---- +₁ 叠加：纯双边累加，不做任何判断 ----
    def __add__(self, other: "Y") -> "Y":
        return Y(self.e + other.e, self.i + other.i)

    def __sub__(self, other: "Y") -> "Y":
        return Y(self.e - other.e, self.i - other.i)

    def __mul__(self, k: int) -> "Y":
        return Y(self.e * k, self.i * k)

    # ---- +₂ 净额：先加总，再取净额。净额恒为正，归哪边由符号定 ----
    def net(self) -> "Y":
        if self.i >= self.e:
            return Y(ZERO, self.i - self.e)
        return Y(self.e - self.i, ZERO)

    @property
    def net_value(self) -> Decimal:
        """净值 i - e。正 = 净流入。"""
        return self.i - self.e

    @property
    def is_zero(self) -> bool:
        return self.e == ZERO and self.i == ZERO

    @property
    def magnitude(self) -> Decimal:
        """规模度量 |e| + |i|。星图用它决定扇形角度。"""
        return abs(self.e) + abs(self.i)

    def as_float(self) -> tuple[float, float]:
        return to_float(self.e), to_float(self.i)

    def as_dict(self) -> dict:
        return {"e": to_float(self.e), "i": to_float(self.i)}

    def __str__(self) -> str:  # pragma: no cover - 调试用
        return f"({self.e}, {self.i})"
