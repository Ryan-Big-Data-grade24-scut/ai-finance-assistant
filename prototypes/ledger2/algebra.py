"""纯代数：零 IO。所有向量都是整数分。

约定
----
向量 (e, i)：**e = 支出，i = 收入**（与 docs/设计/第二轮/05_账本数据结构.md 一致）。
atom(支出 a) = (a, 0)；atom(收入 a) = (0, a)；中性记支出。
+1 = 分量相加。
+2 = ν：先 +1 相加，再两个分量同时减去较小者。
μ（镜像）= 两个分量互换 —— 分裂副本 t′ 的向量「刚好反过来」。

ν 的直接后果（容易想歪，记牢）：
只有当组里**同时有收入和支出**时才会发生抵消；同向的两笔不抵消。
    ν(支出30, 收入50) = (0, 20)   → 净收入 20
    ν(支出20, 支出25) = (45, 0)   → 原样相加
    ν(支出100, 收入100) = (0, 0)  → 完全抵消
"""
from __future__ import annotations

Vector = tuple[int, int]


def atom(amount: int, direction: int) -> Vector:
    """N4：`[]` 就是 atom。

    direction > 0 → 收入 (0, a)；direction <= 0 → 支出 (a, 0)。
    中性（0）按第一轮定下的口径记支出。
    """
    if direction > 0:
        return (0, amount)
    return (amount, 0)


def mirror(v: Vector) -> Vector:
    """μ：分裂副本 t′ 的 IE 向量刚好反过来（N19）。"""
    return (v[1], v[0])


def plus1(values) -> Vector:
    """+1：分量相加。"""
    e = 0
    i = 0
    for v in values:
        e += v[0]
        i += v[1]
    return (e, i)


def net(values) -> Vector:
    """ν：+2。先 +1，再两个分量同时减去较小者。

    性质：ν(ν(x)) = ν(x) —— 任意嵌套幂等，所以组套组不会多扣一次。
    """
    e, i = plus1(values)
    m = min(e, i)
    return (e - m, i - m)


def yuan(cents: int) -> str:
    """分 → 元（只给人看；引擎内部一律用整数分）。"""
    sign = '-' if cents < 0 else ''
    c = abs(cents)
    return f'{sign}{c // 100}.{c % 100:02d}'
