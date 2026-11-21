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


@dataclass(frozen=True)
class GroupRef:
    """指向一个 +2 净额组。选择 ref 的写法是 `g:<组 id>`。"""
    id: str

    def __str__(self) -> str:
        return f"g:{self.id}"


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
    source: str | None = None
    note: str | None = None      # 口径说明：全额退款 / 内部搬运 / 部分退款（给人看）
    # 人工字段（来自 ledger2_tx；初始化后才有）
    title: str | None = None
    memo: str | None = None      # 人工备注（表里叫 note；这里避开上面的口径说明）
    category_path: list[str] | None = None   # 初始化时写入的完整路径（根以下各级节点名）

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
    note: str = ''                                  # 组自己的备注（不参与金额）
    members: list[Tx] = field(default_factory=list)
    value: Vector | None = None


@dataclass
class Node:
    id: str
    parent_id: str | None
    name: str
    note: str = ''                                  # 节点自己的备注（不参与金额）
    children: list = field(default_factory=list)   # Node | NetGroup | Tx
    value: Vector | None = None
    kind: str = 'node'                              # root | category（平台预设建的，仅标记）| node


@dataclass(frozen=True)
class Scope:
    """读时作用域：范围内交易的 raw id 集合。只有这一个筛选入口。"""
    tx_ids: frozenset[int]

    def contains(self, tx_id: int) -> bool:
        return tx_id in self.tx_ids


def group_value(members) -> Vector:
    """ν(⊕ 成员向量)。"""
    return net([m.value() for m in members])
