"""读出来的形状：Tx / NetGroup / Node / Scope。

类不是表的镜像，而是「扁平行装配成树之后」的产物（装配发生在 reader 里）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .algebra import Vector, atom, mirror, net


@dataclass(frozen=True)
class TxRef:
    """指向一笔账本交易。primary = 原始交易，mirror = 分裂副本 t′。"""
    tx_id: int
    role: str = 'primary'

    @property
    def key(self) -> tuple[int, str]:
        return (self.tx_id, self.role)

    def __str__(self) -> str:
        return f"t#{self.tx_id}" if self.role == 'primary' else f"t'#{self.tx_id}"


@dataclass
class Tx:
    id: int
    role: str
    amount: int
    direction: int
    tx_time: str
    date: str
    category: str | None = None
    counterparty: str | None = None
    item: str | None = None

    @property
    def key(self) -> tuple[int, str]:
        return (self.id, self.role)

    @property
    def label(self) -> str:
        return f"t#{self.id}" if self.role == 'primary' else f"t'#{self.id}"

    def value(self) -> Vector:
        """⟦τ⟧ = atom(τ)；mirror 取 μ(atom) —— 算出来的，不是存着的（N3 / N19）。"""
        v = atom(self.amount, self.direction)
        return mirror(v) if self.role == 'mirror' else v


@dataclass
class NetGroup:
    id: str
    name: str
    node_id: str
    members: list[Tx] = field(default_factory=list)
    value: Vector | None = None


@dataclass
class Node:
    id: str
    parent_id: str | None
    name: str
    children: list = field(default_factory=list)   # Node | NetGroup | Tx
    value: Vector | None = None


@dataclass(frozen=True)
class Scope:
    """读时作用域：范围内交易的 raw id 集合。只有这一个筛选入口。"""
    tx_ids: frozenset[int]

    def contains(self, tx_id: int) -> bool:
        return tx_id in self.tx_ids


def group_value(members) -> Vector:
    """ν(⊕ 成员向量)。"""
    return net([m.value() for m in members])
