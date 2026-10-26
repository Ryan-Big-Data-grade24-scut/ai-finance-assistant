"""
backend/ledger/ops.py —— 原子操作（纯函数，零 IO）

**每个 op 都是 `Ledger -> OpResult` 的纯函数**：不改入参，返回新对象。
不变量由 `invariants.py` 在 op 之后统一检查 —— op 自己不做检查，
它只管改结构；「改完再验，不过就整体拒绝」是 service 层的事。

八个 op（与 `schemas.py` 第 6 节的判别联合一一对应）：

    1  set_op        切节点的 op（ADD ↔ NET）
    2  split         分裂一笔交易（仅外部额度）
    3  unsplit       把分裂的两条腿合回一条
    4  create_node   建子节点（把若干腿从父节点挪到新节点）
    5  dissolve_node 解散节点（成员挪回父节点，节点删除）
    6  reassign      改交易归属（腿从 A 挪到 B）
    7  attach_tx     把一笔交易纳入本账
    8  detach_tx     把一笔交易移出本账

⚠️ **不实现**：对已聚合结果（+₂ 对象）的分裂。代数 §2.6 说「所有判断都在
   原子层完成」，这是唯一的例外；`02_引擎.md` §4.3 已决定不实现。
   不实现 = 把例外排除在系统外，比实现一个没定义的语义安全。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .atoms import legs_for, split_applicable
from .amount import Y
from .errors import Code, LedgerError
from .tree import (
    KIND_EXTERNAL_INCOME,
    KIND_GROUP,
    KIND_ROOT,
    Ledger,
    Node,
    OP_ADD,
    OP_NET,
    ROLE_MIRROR,
    ROLE_PRIMARY,
    Tree,
    aggregate,
)


@dataclass
class OpResult:
    """一次 op 的结果。

    tree     新的树（入参的树没有被修改）
    ledger   新的账本状态（txs 可能变，见 attach/detach）
    warnings 人话警告（如「这次让总账少了 12.60」）。**不是错误**。
    root_before / root_after  让调用方能自己报「变了多少」
    """

    ledger: Ledger
    warnings: list[str] = field(default_factory=list)
    root_before: Y = Y()
    root_after: Y = Y()

    @property
    def tree(self) -> Tree:
        return self.ledger.tree

    @property
    def delta(self) -> Y:
        return self.root_after - self.root_before


# ──────────────────────────────────────────────────────────────
#  工具
# ──────────────────────────────────────────────────────────────

def _root_value(ledger: Ledger) -> Y:
    return aggregate(ledger).get(ledger.tree.root_id, Y())


def _clone(ledger: Ledger) -> Ledger:
    """深拷贝出可改的副本。op 绝不碰入参。"""
    return Ledger(book=ledger.book, tree=ledger.tree.copy(), txs=dict(ledger.txs))


def next_node_id(book_id: str, tree: Tree) -> str:
    """生成下一个节点 id：'{book_id}:n0007'。

    自带归属前缀 ⇒ 跨账本不可能撞；即使忘了传 book_id 也能从
    node_id 反解归属，不一致就报 CROSS_BOOK。
    """
    max_seq = 0
    for nid in tree.nodes:
        if not nid.startswith(f"{book_id}:n"):
            continue
        try:
            seq = int(nid.rsplit(":n", 1)[1])
        except ValueError:
            continue
        max_seq = max(max_seq, seq)
    return f"{book_id}:n{max_seq + 1:04d}"


def _node_of(ledger: Ledger, node_id: str) -> Node:
    node = ledger.tree.node(node_id)
    if node is None:
        raise LedgerError(
            f"账本里没有这个节点（{node_id}）。",
            code=Code.NODE_NOT_FOUND,
            http_status=404,
            detail={"node_id": node_id},
        )
    return node


def _require_same_book(ledger: Ledger, node_id: str) -> None:
    if not node_id.startswith(f"{ledger.book_id}:"):
        raise LedgerError(
            "这个节点不属于当前账本，跨账操作是不允许的。",
            code=Code.CROSS_BOOK,
            http_status=400,
            detail={"node_id": node_id, "book_id": ledger.book_id},
        )


def _external_income_node(ledger: Ledger, *, create: bool = False) -> str | None:
    """本账的「外部收入节点」。

    I4：分裂产生的镜像腿**只能**计入它，没有第二种可能。
    所以它是**唯一**的：按 kind 找，找到多个就是树被污染了。
    """
    tree = ledger.tree
    found = [n.node_id for n in tree.nodes.values() if n.kind == KIND_EXTERNAL_INCOME]
    if len(found) > 1:
        raise LedgerError(
            "这本账里出现了多个「外部收入节点」，账已经被污染了。",
            code=Code.INVARIANT_BROKEN,
            http_status=500,
            detail={"nodes": found},
        )
    if found:
        return found[0]
    if not create:
        return None
    nid = next_node_id(ledger.book_id, tree)
    tree.nodes[nid] = Node(
        node_id=nid,
        parent_id=tree.root_id,
        name="外部收入",
        op=OP_ADD,
        kind=KIND_EXTERNAL_INCOME,
        sort_key=9999,
    )
    return nid


def _check_parent(ledger: Ledger, parent_id: str) -> Node:
    _require_same_book(ledger, parent_id)
    parent = _node_of(ledger, parent_id)
    if parent.kind == KIND_EXTERNAL_INCOME:
        raise LedgerError(
            "「外部收入节点」是分裂镜像腿的专用落点，不能往里塞别的东西。",
            code=Code.INVALID_OP,
            http_status=400,
            detail={"node_id": parent_id},
        )
    return parent


def _tx_in_book(ledger: Ledger, tx_id: int) -> None:
    if tx_id not in ledger.txs:
        raise LedgerError(
            f"这笔交易（{tx_id}）不在当前账本里。",
            code=Code.TX_NOT_IN_BOOK,
            http_status=400,
            detail={"tx_id": tx_id, "book_id": ledger.book_id},
        )


# ──────────────────────────────────────────────────────────────
#  1  set_op
# ──────────────────────────────────────────────────────────────

def set_op(ledger: Ledger, node_id: str, op: str) -> OpResult:
    """切节点的聚合方式：ADD ↔ NET。

    这是「状态」不是「动作」：节点的值只由 op 和子节点值决定。
    切回 ADD 能还原结构，但**数值不一定还原**（净额化有损）——
    还原靠操作日志，不靠算术。
    """
    if op not in (OP_ADD, OP_NET):
        raise LedgerError(
            f"聚合方式只能是 {OP_ADD} 或 {OP_NET}，收到的是「{op}」。",
            code=Code.INVALID_OP,
            http_status=400,
            detail={"op": op},
        )
    new = _clone(ledger)
    before = _root_value(ledger)
    node = _node_of(new, node_id)
    node.op = op
    after = _root_value(new)
    return OpResult(
        ledger=new,
        warnings=_value_warnings(before, after),
        root_before=before,
        root_after=after,
    )


# ──────────────────────────────────────────────────────────────
#  2  split
# ──────────────────────────────────────────────────────────────

def split(ledger: Ledger, tx_id: int) -> OpResult:
    """分裂一笔交易：ℓ₁（去路）留在原节点，ℓ₂（镜像收入）进外部收入节点。

    适用域**严格限定**（代数 §2.4）：仅当 τ 涉及外部额度
    （`tx_overrides.funding` ∈ {亲情卡, 他人代付}）。
    退款笔不适用（代数 §2.5 退款优先）。

    I4：镜像腿的去向唯一 —— 就是外部收入节点。
    所以 `reassign` 只搬主腿，镜像腿**永远**留在外部收入节点。
    """
    _tx_in_book(ledger, tx_id)
    tx = ledger.txs[tx_id]
    if not split_applicable(tx):
        raise LedgerError(
            "这笔交易不涉及外部额度，不能分裂（分裂只对亲情卡/他人代付有意义）。",
            code=Code.SPLIT_NOT_APPLICABLE,
            http_status=400,
            detail={"tx_id": tx_id, "funding": tx.funding},
        )
    if ledger.is_split(tx_id):
        raise LedgerError(
            "这笔交易已经分裂过了。",
            code=Code.TX_ALREADY_SPLIT,
            http_status=400,
            detail={"tx_id": tx_id},
        )
    new = _clone(ledger)
    before = _root_value(ledger)
    ext = _external_income_node(new, create=True)
    ext_node = new.tree.nodes[ext]
    if ext_node.op != OP_ADD:
        raise LedgerError(
            "「外部收入节点」必须是叠加态 —— 净额化会让镜像腿消失，账就不可审计了。",
            code=Code.SPLIT_ON_NET,
            http_status=400,
            detail={"node_id": ext},
        )
    new.tree.legs[(tx_id, ROLE_MIRROR)] = ext
    after = _root_value(new)
    return OpResult(
        ledger=new,
        warnings=_value_warnings(before, after),
        root_before=before,
        root_after=after,
    )


# ──────────────────────────────────────────────────────────────
#  3  unsplit
# ──────────────────────────────────────────────────────────────

def unsplit(ledger: Ledger, tx_id: int) -> OpResult:
    """把分裂的两条腿合回一条：删掉镜像腿。

    数值**会变**（分裂本身不守恒），这是预期行为，不是 bug。
    """
    _tx_in_book(ledger, tx_id)
    if not ledger.is_split(tx_id):
        raise LedgerError(
            "这笔交易没有分裂过，没有可合并的镜像腿。",
            code=Code.TX_NOT_SPLIT,
            http_status=400,
            detail={"tx_id": tx_id},
        )
    new = _clone(ledger)
    before = _root_value(ledger)
    del new.tree.legs[(tx_id, ROLE_MIRROR)]
    after = _root_value(new)
    return OpResult(
        ledger=new,
        warnings=_value_warnings(before, after),
        root_before=before,
        root_after=after,
    )


# ──────────────────────────────────────────────────────────────
#  4  create_node
# ──────────────────────────────────────────────────────────────

def create_node(
    ledger: Ledger,
    parent_id: str,
    name: str,
    *,
    op: str = OP_ADD,
    tx_ids: list[int] | None = None,
    node_id: str | None = None,
) -> OpResult:
    """建子节点：把若干腿从父节点挪到新节点。

    语义（`02_引擎.md` §4.4）：把父节点的一部分划出来，给它一个名字和 op。
    实现上就是树的重挂。

    ⚠️ **建节点必须显式声明新节点的 op** —— 因为如果父节点是 NET、
       新节点是 ADD（或反过来），聚合方式变了，总账的值就变了。
    引擎会把变化量报出来。
    """
    if not (name or "").strip():
        raise LedgerError(
            "分组得有个名字。",
            code=Code.EMPTY_NODE_NAME,
            http_status=400,
        )
    if op not in (OP_ADD, OP_NET):
        raise LedgerError(
            f"聚合方式只能是 {OP_ADD} 或 {OP_NET}，收到的是「{op}」。",
            code=Code.INVALID_OP,
            http_status=400,
            detail={"op": op},
        )
    new = _clone(ledger)
    before = _root_value(ledger)
    _check_parent(new, parent_id)
    nid = node_id or next_node_id(ledger.book_id, new.tree)
    if nid in new.tree.nodes:
        raise LedgerError(
            f"节点 id 已存在（{nid}）。",
            code=Code.INVALID_OP,
            http_status=409,
            detail={"node_id": nid},
        )
    new.tree.nodes[nid] = Node(
        node_id=nid,
        parent_id=parent_id,
        name=name.strip(),
        op=op,
        kind=KIND_GROUP,
    )
    moved = 0
    for tx_id in tx_ids or []:
        _tx_in_book(new, tx_id)
        cur = new.tree.tx_node(tx_id)
        if cur != parent_id:
            raise LedgerError(
                f"交易 {tx_id} 现在不在 {parent_id} 下，没法挪进新分组。",
                code=Code.SAME_PARENT_REQUIRED,
                http_status=400,
                detail={"tx_id": tx_id, "actual_parent": cur, "expected_parent": parent_id},
            )
        new.tree.legs[(tx_id, ROLE_PRIMARY)] = nid
        moved += 1
    after = _root_value(new)
    return OpResult(
        ledger=new,
        warnings=_value_warnings(before, after),
        root_before=before,
        root_after=after,
    )


# ──────────────────────────────────────────────────────────────
#  5  dissolve_node
# ──────────────────────────────────────────────────────────────

def dissolve_node(ledger: Ledger, node_id: str) -> OpResult:
    """解散分组：成员挪回父节点，节点删除。

    「有子节点的节点删不掉」是结构约束 —— 所以解散必须递归地把
    子树里的腿全部提升到父节点，而不是只挪一层。
    """
    new = _clone(ledger)
    before = _root_value(ledger)
    node = _node_of(new, node_id)
    if node.is_root:
        raise LedgerError(
            "根节点不能解散 —— 解散整本账请用「回到 M₀」。",
            code=Code.INVALID_OP,
            http_status=400,
            detail={"node_id": node_id},
        )
    if node.kind == KIND_EXTERNAL_INCOME:
        raise LedgerError(
            "「外部收入节点」不能解散 —— 它是分裂镜像腿的专用落点。",
            code=Code.INVALID_OP,
            http_status=400,
            detail={"node_id": node_id},
        )
    parent_id = node.parent_id
    doomed = new.tree.descendants(node_id)
    for nid in doomed:
        for tx_id, role in new.tree.legs_of(nid):
            if role != ROLE_PRIMARY:
                continue          # 镜像腿永远留在外部收入节点
            new.tree.legs[(tx_id, ROLE_PRIMARY)] = parent_id
    for nid in doomed:
        new.tree.nodes.pop(nid, None)
    after = _root_value(new)
    return OpResult(
        ledger=new,
        warnings=_value_warnings(before, after),
        root_before=before,
        root_after=after,
    )


# ──────────────────────────────────────────────────────────────
#  6  reassign
# ──────────────────────────────────────────────────────────────

def reassign(ledger: Ledger, tx_id: int, to_node_id: str) -> OpResult:
    """改交易归属：主腿从当前节点挪到 to_node_id。

    这是 AI 分类插件**唯一**的写操作。

    ⚠️ **只搬主腿**。镜像腿按 I4 永远钉在外部收入节点 ——
       否则分裂的去向就不再唯一，I3/I4 会崩。
    """
    _tx_in_book(ledger, tx_id)
    new = _clone(ledger)
    before = _root_value(ledger)
    _require_same_book(new, to_node_id)
    _node_of(new, to_node_id)
    cur = new.tree.tx_node(tx_id)
    if cur is None:
        raise LedgerError(
            f"交易 {tx_id} 没有腿 —— 它可能已经不在本账里了。",
            code=Code.TX_NOT_IN_BOOK,
            http_status=400,
            detail={"tx_id": tx_id},
        )
    if cur == to_node_id:
        raise LedgerError(
            "这笔交易已经在那个节点里了，不用挪。",
            code=Code.SAME_PARENT_REQUIRED,
            http_status=400,
            detail={"tx_id": tx_id, "node_id": to_node_id},
        )
    if new.tree.nodes[to_node_id].kind == KIND_EXTERNAL_INCOME:
        raise LedgerError(
            "不能把交易挪进「外部收入节点」—— 那里只放分裂产生的镜像腿。",
            code=Code.INVALID_OP,
            http_status=400,
            detail={"node_id": to_node_id},
        )
    new.tree.legs[(tx_id, ROLE_PRIMARY)] = to_node_id
    after = _root_value(new)
    return OpResult(
        ledger=new,
        warnings=_value_warnings(before, after),
        root_before=before,
        root_after=after,
    )


# ──────────────────────────────────────────────────────────────
#  7 / 8  attach / detach
# ──────────────────────────────────────────────────────────────

def attach_tx(ledger: Ledger, tx_id: int, tx_row, node_id: str | None = None) -> OpResult:
    """把一笔交易纳入本账（成员集显式化）。

    账本的成员集在建账时按区间冻结，之后不可改区间；新交易进来
    只能靠显式 attach。
    """
    new = _clone(ledger)
    before = _root_value(ledger)
    if tx_id in new.txs:
        raise LedgerError(
            f"交易 {tx_id} 已经在这本账里了。",
            code=Code.INVALID_OP,
            http_status=400,
            detail={"tx_id": tx_id},
        )
    target = node_id or new.tree.root_id
    _require_same_book(new, target)
    _node_of(new, target)
    new.txs[tx_id] = tx_row
    new.tree.legs[(tx_id, ROLE_PRIMARY)] = target
    after = _root_value(new)
    return OpResult(
        ledger=new,
        warnings=_value_warnings(before, after),
        root_before=before,
        root_after=after,
    )


def detach_tx(ledger: Ledger, tx_id: int) -> OpResult:
    """把一笔交易移出本账（腿全部撤掉）。"""
    _tx_in_book(ledger, tx_id)
    new = _clone(ledger)
    before = _root_value(ledger)
    for role in (ROLE_PRIMARY, ROLE_MIRROR):
        new.tree.legs.pop((tx_id, role), None)
    new.txs.pop(tx_id, None)
    after = _root_value(new)
    return OpResult(
        ledger=new,
        warnings=_value_warnings(before, after),
        root_before=before,
        root_after=after,
    )


# ──────────────────────────────────────────────────────────────
#  警告文案
# ──────────────────────────────────────────────────────────────

def _value_warnings(before: Y, after: Y) -> list[str]:
    """值变了就说清楚变了多少 —— 净额化有损，不报出来用户会懵。"""
    d = after.net_value - before.net_value
    if d == 0:
        return []
    sign = "多了" if d > 0 else "少了"
    return [f"这次操作让总账{sign} {abs(d)} 元。"]


def all_legs_of(ledger: Ledger, tx_id: int) -> list[tuple[str, Y]]:
    """这笔交易在本账里实际持有的腿（引擎视角，含镜像）。"""
    tx = ledger.txs.get(tx_id)
    if tx is None:
        return []
    out: list[tuple[str, Y]] = []
    for role, _ in legs_for(tx, split=ledger.is_split(tx_id)):
        if (tx_id, role) in ledger.tree.legs:
            out.append((role, ledger.leg_value(tx_id, role)))
    return out
