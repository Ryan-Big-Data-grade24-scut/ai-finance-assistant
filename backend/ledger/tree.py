"""
backend/ledger/tree.py —— 树结构与聚合（纯数据结构 + 纯函数，零 IO）

代数 §3：从原子到层级。

节点 n := ( id, name, op, parent, children, legs )
                op ∈ { ADD, NET }

只有两种算子（代数 §3.3）：凡是「不该进报表」的东西，都在原子层
（atoms.py）处理掉了，节点层只做聚合。

⚠️ 叶子 = 「持腿的 group 节点」，不单独建 kind='leaf' 的空节点。
   否则 M₀ 要为每笔交易建一个节点，表会爆。与代数 §3.2 兼容：
   叶子节点 children = ∅，legs ≠ ∅。

⚠️ 深度不设上限。引擎的正确性只靠 §3.2 的恒等式，与深度无关。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .atoms import TxRow, legs_for
from .amount import Y, ZERO

OP_ADD = "ADD"
OP_NET = "NET"
OPS = (OP_ADD, OP_NET)

KIND_ROOT = "root"
KIND_GROUP = "group"
KIND_EXTERNAL_INCOME = "external_income"

ROLE_PRIMARY = "primary"
ROLE_MIRROR = "mirror"


@dataclass
class Node:
    """一个节点。叶子 = children 为空、直接持腿的 group 节点。"""

    node_id: str
    parent_id: str | None
    name: str
    op: str = OP_ADD
    kind: str = KIND_GROUP
    sort_key: int = 0

    @property
    def is_root(self) -> bool:
        return self.parent_id is None


@dataclass
class Book:
    """一本账 = 一个区间 + 一棵私有的树。"""

    book_id: str
    name: str
    date_from: str
    date_to: str
    root_node_id: str
    created_by: str = "ui"
    created_at: str = ""


@dataclass
class Tree:
    """账本的树。

    nodes  node_id -> Node
    legs   (tx_id, role) -> node_id      谁在哪
    """

    book_id: str
    root_id: str
    nodes: dict[str, Node] = field(default_factory=dict)
    legs: dict[tuple[int, str], str] = field(default_factory=dict)

    # ---- 基本访问 ----
    def node(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def children_of(self, node_id: str) -> list[str]:
        kids = [n.node_id for n in self.nodes.values() if n.parent_id == node_id]
        kids.sort(key=lambda nid: (self.nodes[nid].sort_key, nid))
        return kids

    def legs_of(self, node_id: str) -> list[tuple[int, str]]:
        """节点直接持有的腿。"""
        out = [(tx_id, role) for (tx_id, role), owner in self.legs.items() if owner == node_id]
        out.sort()
        return out

    def leg_owner(self, tx_id: int, role: str) -> str | None:
        return self.legs.get((tx_id, role))

    def tx_node(self, tx_id: int) -> str | None:
        """一笔交易的主腿在哪个节点。"""
        return self.legs.get((tx_id, ROLE_PRIMARY))

    def path_to_root(self, node_id: str) -> list[str]:
        """从 node_id 一路往上到根。找不到就返回空。"""
        path: list[str] = []
        cur: str | None = node_id
        seen: set[str] = set()
        while cur is not None and cur in self.nodes and cur not in seen:
            seen.add(cur)
            path.append(cur)
            cur = self.nodes[cur].parent_id
        return path

    def descendants(self, node_id: str) -> set[str]:
        """node_id 的整棵子树（含自己）。"""
        out = {node_id}
        stack = [node_id]
        while stack:
            cur = stack.pop()
            for kid in self.children_of(cur):
                if kid not in out:
                    out.add(kid)
                    stack.append(kid)
        return out

    def depth_of(self, node_id: str) -> int:
        path = self.path_to_root(node_id)
        return max(len(path) - 1, 0)

    def external_income_node(self) -> str | None:
        """本账的「外部收入节点」—— I4 规定分裂的镜像腿只能在这。"""
        for n in self.nodes.values():
            if n.kind == KIND_EXTERNAL_INCOME:
                return n.node_id
        return None

    def copy(self) -> "Tree":
        return Tree(
            book_id=self.book_id,
            root_id=self.root_id,
            nodes={k: Node(v.node_id, v.parent_id, v.name, v.op, v.kind, v.sort_key)
                   for k, v in self.nodes.items()},
            legs=dict(self.legs),
        )


@dataclass
class Ledger:
    """一本账的完整状态：账本 + 树 + 成员交易。

    txs 是本账成员的原始事实（只读），腿的值由 atoms 按规则实时算。
    """

    book: Book
    tree: Tree
    txs: dict[int, TxRow] = field(default_factory=dict)

    @property
    def book_id(self) -> str:
        return self.book.book_id

    def leg_value(self, tx_id: int, role: str) -> Y:
        """一条腿的值。**不存储，实时算** —— 规则一改，腿跟着改。"""
        tx = self.txs.get(tx_id)
        if tx is None:
            return Y(ZERO, ZERO)
        split = role == ROLE_MIRROR
        for r, value in legs_for(tx, split=split):
            if r == role:
                return value
        return Y(ZERO, ZERO)

    def is_split(self, tx_id: int) -> bool:
        return (tx_id, ROLE_MIRROR) in self.tree.legs


# ──────────────────────────────────────────────────────────────
#  聚合
# ──────────────────────────────────────────────────────────────

def _post_order(tree: Tree) -> list[str]:
    """自底向上的节点顺序（孩子先于父亲）。不用递归，深度不受限。"""
    order: list[str] = []
    stack = [(tree.root_id, False)]
    while stack:
        nid, expanded = stack.pop()
        if expanded:
            order.append(nid)
            continue
        stack.append((nid, True))
        for kid in reversed(tree.children_of(nid)):
            stack.append((kid, False))
    return order


def aggregate(ledger: Ledger) -> dict[str, Y]:
    """算全树的值。Y_n = op_n( ⨆ children ∪ legs )。

    这是代数 §3.3 的恒等式本身 —— 定义即恒等（I5）。
    """
    tree = ledger.tree
    vals: dict[str, Y] = {}
    for nid in _post_order(tree):
        node = tree.nodes.get(nid)
        if node is None:
            continue
        total = Y()
        for kid in tree.children_of(nid):
            total = total + vals.get(kid, Y())
        for tx_id, role in tree.legs_of(nid):
            total = total + ledger.leg_value(tx_id, role)
        vals[nid] = total.net() if node.op == OP_NET else total
    return vals


def value_of(ledger: Ledger, node_id: str) -> Y:
    return aggregate(ledger).get(node_id, Y())


def build_m0(book: Book, txs: list[TxRow]) -> Ledger:
    """M₀：退化树 —— 只有根，根直接持有全部腿，op = ADD。

    代数 §3.5：「若整棵树只有一层（根 + 全部腿），且 op = ADD，
    这就是平台账单默认做的事。」

    这不是特例，是引擎的起点，也是任何时刻「把树全 collapse 掉」的结果。
    """
    tree = Tree(book_id=book.book_id, root_id=book.root_node_id)
    tree.nodes[book.root_node_id] = Node(
        node_id=book.root_node_id,
        parent_id=None,
        name=book.name or "总账",
        op=OP_ADD,
        kind=KIND_ROOT,
    )
    for tx in txs:
        tree.legs[(tx.tx_id, ROLE_PRIMARY)] = book.root_node_id
    return Ledger(book=book, tree=tree, txs={tx.tx_id: tx for tx in txs})
