"""
backend/ledger/service.py —— HTTP 与 MCP **共用**的读写接口

**唯一写路径是 `apply_op`**。前端和 AI 插件走同一套校验、同一套错误文案。

流程（每一次写操作都一样）：

    开事务 → 读回状态 → 执行 op → 跑不变量 → 不过就抛 → 回滚
           → 写树 + 写操作日志 + 更新撤销栈 → 提交 → 返回权威树

「不通过就拒绝」是**免费获得的**：不变量一抛异常，`db.transaction` 自动 ROLLBACK。

**响应一律带整棵树**：前端乐观更新后必须用引擎返回的权威树覆盖
（「前端不算钱」）。
"""
from __future__ import annotations

import sqlite3

from ..db import get_conn, transaction
from . import loader, ops, store
from .atoms import TxRow
from .errors import Code, LedgerError
from .invariants import assert_all, check_all
from .tree import Book, Ledger, Tree, aggregate, build_m0

# ---- 八个 op 的名字（与 schemas.py 的判别联合一一对应）----
OP_SET_OP = "set_op"
OP_SPLIT = "split"
OP_UNSPLIT = "unsplit"
OP_CREATE_NODE = "create_node"
OP_DISSOLVE_NODE = "dissolve_node"
OP_REASSIGN = "reassign"
OP_ATTACH_TX = "attach_tx"
OP_DETACH_TX = "detach_tx"

ALL_OPS = (
    OP_SET_OP, OP_SPLIT, OP_UNSPLIT, OP_CREATE_NODE,
    OP_DISSOLVE_NODE, OP_REASSIGN, OP_ATTACH_TX, OP_DETACH_TX,
)


# ──────────────────────────────────────────────────────────────
#  读
# ──────────────────────────────────────────────────────────────

def list_books() -> list[Book]:
    conn = get_conn()
    try:
        return loader.list_books(conn)
    finally:
        conn.close()


def load_ledger(conn: sqlite3.Connection, book_id: str) -> Ledger:
    """把一本账读成内存里的 Ledger。"""
    book = loader.require_book(conn, book_id)
    tree = loader.load_tree(conn, book)
    txs = loader.load_book_txs(conn, book_id)
    return Ledger(book=book, tree=tree, txs={tx.tx_id: tx for tx in txs})


def tree_payload(ledger: Ledger, focus_node_id: str | None = None) -> dict:
    """整棵树的权威快照 —— 节点、值、腿。

    `focus_node_id` 只是**给前端一个焦点提示**，不裁剪返回内容 ——
    裁剪会让前端维护两份树。
    """
    tree = ledger.tree
    vals = aggregate(ledger)
    nodes = []
    for nid, node in tree.nodes.items():
        y = vals.get(nid)
        nodes.append({
            "node_id": nid,
            "parent_id": node.parent_id,
            "name": node.name,
            "op": node.op,
            "kind": node.kind,
            "depth": tree.depth_of(nid),
            "e": float(y.e) if y else 0.0,
            "i": float(y.i) if y else 0.0,
            "net": float(y.net_value) if y else 0.0,
            "child_count": len(tree.children_of(nid)),
            "leg_count": len(tree.legs_of(nid)),
        })
    root = vals.get(tree.root_id)
    legs = []
    for (tx_id, role), node_id in sorted(tree.legs.items()):
        y = ledger.leg_value(tx_id, role)
        legs.append({
            "tx_id": tx_id,
            "role": role,
            "node_id": node_id,
            "e": float(y.e),
            "i": float(y.i),
        })
    focus_ids = sorted(tree.descendants(focus_node_id)) if focus_node_id and focus_node_id in tree.nodes else []
    return {
        "book_id": ledger.book.book_id,
        "root_id": tree.root_id,
        "root_e": float(root.e) if root else 0.0,
        "root_i": float(root.i) if root else 0.0,
        "root_net": float(root.net_value) if root else 0.0,
        "nodes": nodes,
        "legs": legs,
        "focus_node_ids": focus_ids,
    }


def get_tree(book_id: str, focus_node_id: str | None = None) -> dict:
    conn = get_conn()
    try:
        ledger = load_ledger(conn, book_id)
    finally:
        conn.close()
    return tree_payload(ledger, focus_node_id)


def get_status(book_id: str) -> dict:
    """客观状态：多少笔已归位、多少笔待归、不变量是否通过。

    **不判断「够不够细」** —— 那是人要回答的（代数 T3）。
    """
    conn = get_conn()
    try:
        ledger = load_ledger(conn, book_id)
        current_seq, redo_stack = loader.load_state(conn, book_id)
    finally:
        conn.close()
    checks = check_all(ledger)
    placed = sum(1 for tx_id in ledger.txs if (tx_id, "primary") in ledger.tree.legs)
    vals = aggregate(ledger)
    root = vals.get(ledger.tree.root_id)
    return {
        "book_id": book_id,
        "total_tx": len(ledger.txs),
        "placed_tx": placed,
        "unplaced_tx": len(ledger.txs) - placed,
        "node_count": len(ledger.tree.nodes),
        "leg_count": len(ledger.tree.legs),
        "root_e": float(root.e) if root else 0.0,
        "root_i": float(root.i) if root else 0.0,
        "root_net": float(root.net_value) if root else 0.0,
        "invariants": [c.as_dict() for c in checks],
        "invariants_ok": all(c.ok for c in checks),
        "current_seq": current_seq,
        "can_undo": current_seq > 0,
        "can_redo": bool(redo_stack),
    }


# ──────────────────────────────────────────────────────────────
#  写
# ──────────────────────────────────────────────────────────────

def create_book(
    name: str,
    date_from: str,
    date_to: str,
    *,
    created_by: str = "ui",
    note: str | None = None,
) -> dict:
    """建一本账：按区间冻结成员集，生成 M₀。

    **区间在建账时冻结，之后不可改** —— 自动进账意味着账的值会随时间自己变；
    代数里 X 是**给定的**输入，冻结的 X 才谈得上重跑。
    """
    if not (name or "").strip():
        raise LedgerError(
            "账本得有个名字。",
            code=Code.EMPTY_NODE_NAME,
            http_status=400,
        )
    if not date_from or not date_to or date_from > date_to:
        raise LedgerError(
            "账本的起止日期不合法。",
            code=Code.INVALID_DATE_RANGE,
            http_status=400,
            detail={"date_from": date_from, "date_to": date_to},
        )
    conn = get_conn()
    try:
        with transaction(conn):
            book_id = f"bk_{store.next_seq(conn, 'book'):04d}"
            txs = loader.txs_in_range(conn, date_from, date_to)
            root_id = f"{book_id}:root"
            book = Book(
                book_id=book_id,
                name=name.strip(),
                date_from=date_from,
                date_to=date_to,
                root_node_id=root_id,
            )
            store.create_book(conn, book, txs, created_by=created_by)
            if note:
                store.update_book_meta(conn, book_id, note=note)
    finally:
        conn.close()

    conn = get_conn()
    try:
        ledger = load_ledger(conn, book_id)
        payload = tree_payload(ledger)
        payload["book"] = {
            "book_id": book.book_id,
            "name": book.name,
            "date_from": book.date_from,
            "date_to": book.date_to,
            "member_count": len(ledger.txs),
        }
        return payload
    finally:
        conn.close()


def apply_op(book_id: str, op: dict, *, created_by: str = "ui") -> dict:
    """**唯一写路径**。

    op = {"op": "<类型>", ...参数}，八个类型见 ALL_OPS。
    """
    op_type = (op or {}).get("op")
    if op_type not in ALL_OPS:
        raise LedgerError(
            f"不认识的账本操作「{op_type}」。",
            code=Code.INVALID_OP,
            http_status=400,
            detail={"allowed": list(ALL_OPS)},
        )
    conn = get_conn()
    try:
        with transaction(conn):
            ledger = load_ledger(conn, book_id)
            new_ledger, inverse = _dispatch(conn, ledger, op_type, op, created_by)
            # 不变量：不过就抛 → 事务自动 ROLLBACK
            assert_all(new_ledger)
            # 顺序要紧：成员集先落表（attach 的腿要靠 ledger_book_tx 才插得进去），
            # 再写树。detach 时删 book_tx 会级联删掉那笔的腿 —— 正好。
            _sync_membership(conn, book_id, ledger, new_ledger)
            store.save_tree(conn, book_id, new_ledger.tree)
            current_seq, redo_stack = loader.load_state(conn, book_id)
            seq = store.next_seq(conn, f"op:{book_id}")
            store.append_op(
                conn, book_id, seq, op_type, op,
                inverse["inverse_type"], inverse["inverse_payload"],
                summary=inverse.get("summary"), created_by=created_by,
            )
            store.set_state(conn, book_id, seq, [])
            payload = tree_payload(new_ledger)
            payload["seq"] = seq
            payload["warnings"] = inverse.get("warnings", [])
    finally:
        conn.close()
    return payload


def _sync_membership(conn, book_id, old: Ledger, new: Ledger) -> None:
    """成员集变了就同步写表（attach / detach）。"""
    for tx_id in new.txs:
        if tx_id not in old.txs:
            store.add_book_tx(conn, book_id, tx_id)
    for tx_id in old.txs:
        if tx_id not in new.txs:
            store.remove_book_tx(conn, book_id, tx_id)


def _dispatch(conn, ledger: Ledger, op_type: str, op: dict, created_by: str):
    """执行 op，并算出它的逆操作（undo 靠日志，不靠算术）。"""
    if op_type == OP_SET_OP:
        node_id = op["node_id"]
        old_op = ledger.tree.nodes[node_id].op
        res = ops.set_op(ledger, node_id, _node_op(op))
        inv = {
            "inverse_type": OP_SET_OP,
            "inverse_payload": {"node_id": node_id, "op": old_op},
        }
    elif op_type == OP_SPLIT:
        res = ops.split(ledger, int(op["tx_id"]))
        inv = {
            "inverse_type": OP_UNSPLIT,
            "inverse_payload": {"tx_id": int(op["tx_id"])},
        }
    elif op_type == OP_UNSPLIT:
        res = ops.unsplit(ledger, int(op["tx_id"]))
        inv = {
            "inverse_type": OP_SPLIT,
            "inverse_payload": {"tx_id": int(op["tx_id"])},
        }
    elif op_type == OP_CREATE_NODE:
        res = ops.create_node(
            ledger, op["parent_id"], op["name"],
            op=_node_op(op, default="ADD"), tx_ids=list(op.get("tx_ids") or []),
            node_id=op.get("node_id"),
        )
        created_id = _newest_node(res.tree, op["parent_id"])
        # 把新建的 node_id 写回 op，日志里存的就是它 ⇒ redo 能拿到同一个 id
        op["node_id"] = created_id
        inv = {
            "inverse_type": OP_DISSOLVE_NODE,
            "inverse_payload": {"node_id": created_id},
        }
    elif op_type == OP_DISSOLVE_NODE:
        node_id = op["node_id"]
        node = ledger.tree.nodes[node_id]
        members = _subtree_members(ledger.tree, node_id)
        res = ops.dissolve_node(ledger, node_id)
        inv = {
            "inverse_type": OP_CREATE_NODE,
            "inverse_payload": {
                "parent_id": node.parent_id,
                "name": node.name,
                "op": node.op,
                "tx_ids": members,
                "node_id": node_id,
            },
        }
    elif op_type == OP_REASSIGN:
        tx_id = int(op["tx_id"])
        old_node = ledger.tree.tx_node(tx_id)
        res = ops.reassign(ledger, tx_id, op["to_node_id"])
        inv = {
            "inverse_type": OP_REASSIGN,
            "inverse_payload": {"tx_id": tx_id, "to_node_id": old_node},
        }
    elif op_type == OP_ATTACH_TX:
        tx_id = int(op["tx_id"])
        row = loader.load_txs_by_ids(conn, [tx_id]).get(tx_id)
        if row is None:
            raise LedgerError(
                f"没有这笔交易（{tx_id}）。",
                code=Code.TX_NOT_FOUND,
                http_status=404,
                detail={"tx_id": tx_id},
            )
        res = ops.attach_tx(ledger, tx_id, row, op.get("node_id"))
        inv = {
            "inverse_type": OP_DETACH_TX,
            "inverse_payload": {"tx_id": tx_id},
        }
    elif op_type == OP_DETACH_TX:
        tx_id = int(op["tx_id"])
        res = ops.detach_tx(ledger, tx_id)
        inv = {
            "inverse_type": OP_ATTACH_TX,
            "inverse_payload": {"tx_id": tx_id},
        }
    else:  # pragma: no cover - 上游已校验
        raise LedgerError(f"不认识的账本操作「{op_type}」。", code=Code.INVALID_OP)

    inv["summary"] = {
        "root_before": float(res.root_before.net_value),
        "root_after": float(res.root_after.net_value),
    }
    inv["warnings"] = res.warnings
    return res.ledger, inv


def _node_op(op: dict, default: str = "ADD") -> str:
    """取「节点的聚合方式」。

    判别联合里 `op` 已经被操作类型占了，所以节点算子叫 `op_value`。
    逆操作载荷里则直接叫 `op` —— 两种都认。
    """
    for key in ("op_value", "node_op", "op"):
        val = op.get(key)
        if val in ("ADD", "NET"):
            return val
    return default


def _newest_node(tree: Tree, parent_id: str) -> str:
    """create_node 之后，新建的节点 = parent 的孩子里 id 最大的那个。"""
    kids = tree.children_of(parent_id)
    if not kids:
        raise LedgerError("建节点之后却找不到新节点。", code=Code.INVARIANT_BROKEN)
    return max(kids)


def _subtree_members(tree: Tree, node_id: str) -> list[int]:
    """子树里全部交易的主腿 id（解散的逆操作要拿它重建）。"""
    out: list[int] = []
    for nid in tree.descendants(node_id):
        for tx_id, role in tree.legs_of(nid):
            if role == "primary":
                out.append(tx_id)
    return sorted(out)


# ──────────────────────────────────────────────────────────────
#  撤销 / 重做 / 回到 M₀
# ──────────────────────────────────────────────────────────────

def undo(book_id: str, *, created_by: str = "ui") -> dict:
    """撤销一步。

    **靠操作日志里的逆操作**，不是靠算术 ——
    分裂不守恒、切 NET 有损，算术还原不出来。
    """
    conn = get_conn()
    try:
        with transaction(conn):
            current_seq, redo_stack = loader.load_state(conn, book_id)
            if current_seq <= 0:
                raise LedgerError(
                    "没有可撤销的操作了。",
                    code=Code.NOTHING_TO_UNDO,
                    http_status=400,
                    detail={"book_id": book_id},
                )
            op_log = loader.load_ops(conn, book_id)
            target = None
            for entry in op_log:
                if entry["seq"] == current_seq:
                    target = entry
                    break
            if target is None:
                raise LedgerError(
                    "撤销栈指向了一个不存在的操作，账本状态可能已损坏。",
                    code=Code.INVARIANT_BROKEN,
                    http_status=500,
                    detail={"seq": current_seq},
                )
            ledger = load_ledger(conn, book_id)
            new_ledger, _ = _dispatch(
                conn, ledger, target["inverse_type"], target["inverse_payload"], created_by
            )
            assert_all(new_ledger)
            # 顺序要紧：成员集先落表（attach 的腿要靠 ledger_book_tx 才插得进去），
            # 再写树。detach 时删 book_tx 会级联删掉那笔的腿 —— 正好。
            _sync_membership(conn, book_id, ledger, new_ledger)
            store.save_tree(conn, book_id, new_ledger.tree)
            store.set_state(conn, book_id, current_seq - 1, redo_stack + [current_seq])
            payload = tree_payload(new_ledger)
            payload["seq"] = current_seq - 1
            payload["warnings"] = []
    finally:
        conn.close()
    return payload


def redo(book_id: str, *, created_by: str = "ui") -> dict:
    """重做一步。"""
    conn = get_conn()
    try:
        with transaction(conn):
            current_seq, redo_stack = loader.load_state(conn, book_id)
            if not redo_stack:
                raise LedgerError(
                    "没有可重做的操作了。",
                    code=Code.NOTHING_TO_REDO,
                    http_status=400,
                    detail={"book_id": book_id},
                )
            # 重做必须**按原顺序**：先重做最早被撤销的那一步
            next_seq = min(redo_stack)
            op_log = loader.load_ops(conn, book_id)
            target = next((e for e in op_log if e["seq"] == next_seq), None)
            if target is None:
                raise LedgerError(
                    "重做栈指向了一个不存在的操作，账本状态可能已损坏。",
                    code=Code.INVARIANT_BROKEN,
                    http_status=500,
                    detail={"seq": next_seq},
                )
            ledger = load_ledger(conn, book_id)
            new_ledger, _ = _dispatch(
                conn, ledger, target["op_type"], target["payload"], created_by
            )
            assert_all(new_ledger)
            # 顺序要紧：成员集先落表（attach 的腿要靠 ledger_book_tx 才插得进去），
            # 再写树。detach 时删 book_tx 会级联删掉那笔的腿 —— 正好。
            _sync_membership(conn, book_id, ledger, new_ledger)
            store.save_tree(conn, book_id, new_ledger.tree)
            store.set_state(conn, book_id, next_seq, [s for s in redo_stack if s != next_seq])
            payload = tree_payload(new_ledger)
            payload["seq"] = next_seq
            payload["warnings"] = []
    finally:
        conn.close()
    return payload


def reset_to_m0(book_id: str, *, created_by: str = "ui") -> dict:
    """回到 M₀ —— 树全 collapse 成「根 + 全部腿」。

    数值**可能变**（原树里有 NET 节点、有分裂）。
    这是预期行为：M₀ 是「平台口径」那一眼，不是「正确账」。
    """
    conn = get_conn()
    try:
        with transaction(conn):
            ledger = load_ledger(conn, book_id)
            book = ledger.book
            txs = list(ledger.txs.values())
            fresh = build_m0(book, txs)
            assert_all(fresh)
            store.save_tree(conn, book_id, fresh.tree)
            _sync_membership(conn, book_id, ledger, fresh)
            current_seq, redo_stack = loader.load_state(conn, book_id)
            store.set_state(conn, book_id, current_seq, redo_stack)
            payload = tree_payload(fresh)
            payload["seq"] = current_seq
            payload["warnings"] = [
                "已经回到 M₀ —— 所有交易都回到了「平台口径」的一层平铺状态。"
            ]
    finally:
        conn.close()
    return payload
