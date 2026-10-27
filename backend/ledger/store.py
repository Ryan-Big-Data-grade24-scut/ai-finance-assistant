"""
backend/ledger/store.py —— **唯一**写 `ledger_*` 表的模块

**纪律**：全仓库对 `ledger_*` 的 INSERT/UPDATE/DELETE 只允许出现在这里。
验证：`tests/test_ledger_guardrails.py` 用 grep 卡住。

本模块**不导出连接**。它只接受一个 `sqlite3.Connection` 参数，
由调用方（service）提供 —— 这样谁拿到了 store，也拿不到"随手写点 SQL"的能力。

**为什么整树覆盖写（save_tree）而不是增量 patch**：
    树小（节点几十个，腿几百条），一次覆盖写比增量写简单得多，
    而且**不可能出现"树和腿对不上"的中间态** ——
    增量写要维护两份结构的一致性，覆盖写不用。
    撤销/重做也简单：日志里存快照，重放即可。
"""
from __future__ import annotations

import json
import sqlite3

from .atoms import TxRow
from .errors import Code, LedgerError
from .tree import Book, Tree

# ---- 逆操作类型 ----
INV_CREATE_NODE = "create_node"
INV_DISSOLVE_NODE = "dissolve_node"
INV_SET_OP = "set_op"
INV_REASSIGN = "reassign"
INV_SPLIT = "split"
INV_UNSPLIT = "unsplit"
INV_ATTACH_TX = "attach_tx"
INV_DETACH_TX = "detach_tx"


# ──────────────────────────────────────────────────────────────
#  序号
# ──────────────────────────────────────────────────────────────

def next_seq(conn: sqlite3.Connection, scope: str) -> int:
    """取号并占用。scope 例：'node:bk_0001' / 'op:bk_0001'。"""
    row = conn.execute(
        "SELECT next_val FROM ledger_seq WHERE scope = ?", (scope,)
    ).fetchone()
    if row is None:
        val = 1
        conn.execute(
            "INSERT INTO ledger_seq (scope, next_val) VALUES (?, ?)", (scope, 2)
        )
        return val
    val = int(row["next_val"])
    conn.execute(
        "UPDATE ledger_seq SET next_val = ? WHERE scope = ?", (val + 1, scope)
    )
    return val


# ──────────────────────────────────────────────────────────────
#  建账
# ──────────────────────────────────────────────────────────────

def create_book(
    conn: sqlite3.Connection,
    book: Book,
    txs: list[TxRow],
    *,
    created_by: str = "ui",
) -> None:
    """建一本账：写账本、冻结成员集、建根节点（M₀）、初始化状态。

    M₀ = 根节点（op=ADD）直接持有全部腿。
    """
    conn.execute(
        "INSERT INTO ledger_books "
        "(book_id, name, date_from, date_to, root_node_id, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (book.book_id, book.name, book.date_from, book.date_to,
         book.root_node_id, created_by),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO ledger_book_tx (book_id, tx_id, added_by) "
        "VALUES (?, ?, ?)",
        [(book.book_id, tx.tx_id, created_by) for tx in txs],
    )
    conn.execute(
        "INSERT INTO ledger_nodes "
        "(book_id, node_id, parent_id, name, op, kind, sort_key) "
        "VALUES (?, ?, NULL, ?, 'ADD', 'root', 0)",
        (book.book_id, book.root_node_id, book.name or "总账"),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO ledger_legs (book_id, tx_id, node_id, role) "
        "VALUES (?, ?, ?, 'primary')",
        [(book.book_id, tx.tx_id, book.root_node_id) for tx in txs],
    )
    conn.execute(
        "INSERT INTO ledger_book_state (book_id, current_seq, redo_stack) "
        "VALUES (?, 0, '[]')",
        (book.book_id,),
    )


def delete_book(conn: sqlite3.Connection, book_id: str) -> None:
    """删一本账（连带树、腿、日志、快照）。原始交易不受影响。"""
    conn.execute("DELETE FROM ledger_books WHERE book_id = ?", (book_id,))


# ──────────────────────────────────────────────────────────────
#  成员集
# ──────────────────────────────────────────────────────────────

def add_book_tx(conn: sqlite3.Connection, book_id: str, tx_id: int, added_by: str = "ui") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ledger_book_tx (book_id, tx_id, added_by) VALUES (?, ?, ?)",
        (book_id, tx_id, added_by),
    )


def remove_book_tx(conn: sqlite3.Connection, book_id: str, tx_id: int) -> None:
    conn.execute(
        "DELETE FROM ledger_book_tx WHERE book_id = ? AND tx_id = ?", (book_id, tx_id)
    )


# ──────────────────────────────────────────────────────────────
#  树（整树覆盖写）
# ──────────────────────────────────────────────────────────────

def save_tree(conn: sqlite3.Connection, book_id: str, tree: Tree) -> None:
    """把整棵树写回去。

    顺序很重要：
      删：先删腿，再**从深到浅**删**库里已有的全部节点**
          （不只是新树里的 —— 被解散的旧节点也必须清掉；
          自引用 FK 是 RESTRICT，父节点还有子节点时删不掉）。
      写：**从浅到深**写节点（子节点的 parent 必须先存在）。
    """
    conn.execute("DELETE FROM ledger_legs WHERE book_id = ?", (book_id,))

    for node_id, depth in _db_depths(conn, book_id):
        conn.execute(
            "DELETE FROM ledger_nodes WHERE book_id = ? AND node_id = ?",
            (book_id, node_id),
        )
        del depth

    depth = {tree.root_id: 0}
    frontier = [tree.root_id]
    while frontier:
        nxt = []
        for nid in frontier:
            for kid in tree.children_of(nid):
                if kid not in depth:
                    depth[kid] = depth[nid] + 1
                    nxt.append(kid)
        frontier = nxt

    for node in sorted(tree.nodes.values(), key=lambda n: depth.get(n.node_id, 0)):
        conn.execute(
            "INSERT INTO ledger_nodes "
            "(book_id, node_id, parent_id, name, op, kind, sort_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (book_id, node.node_id, node.parent_id, node.name,
             node.op, node.kind, node.sort_key),
        )
    conn.executemany(
        "INSERT OR REPLACE INTO ledger_legs (book_id, tx_id, node_id, role) "
        "VALUES (?, ?, ?, ?)",
        [(book_id, tx_id, node_id, role)
         for (tx_id, role), node_id in tree.legs.items()],
    )


def _db_depths(conn: sqlite3.Connection, book_id: str) -> list[tuple[str, int]]:
    """库里现有节点的 (node_id, depth)，按 depth 降序 —— 用于自底向上删除。"""
    rows = conn.execute(
        "WITH RECURSIVE d(node_id, depth) AS ("
        "  SELECT node_id, 0 FROM ledger_nodes "
        "  WHERE book_id = ? AND parent_id IS NULL"
        "  UNION ALL"
        "  SELECT n.node_id, d.depth + 1 FROM ledger_nodes n "
        "  JOIN d ON n.parent_id = d.node_id WHERE n.book_id = ?"
        ") SELECT node_id, depth FROM d ORDER BY depth DESC",
        (book_id, book_id),
    ).fetchall()
    return [(r["node_id"], int(r["depth"])) for r in rows]


# ──────────────────────────────────────────────────────────────
#  操作日志与状态
# ──────────────────────────────────────────────────────────────

def append_op(
    conn: sqlite3.Connection,
    book_id: str,
    seq: int,
    op_type: str,
    payload: dict,
    inverse_type: str,
    inverse_payload: dict,
    summary: dict | None = None,
    created_by: str = "ui",
) -> None:
    conn.execute(
        "INSERT INTO ledger_ops "
        "(book_id, seq, op_type, payload, inverse_type, inverse_payload, summary, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (book_id, seq, op_type,
         json.dumps(payload, ensure_ascii=False),
         inverse_type,
         json.dumps(inverse_payload, ensure_ascii=False),
         json.dumps(summary or {}, ensure_ascii=False),
         created_by),
    )


def set_state(conn: sqlite3.Connection, book_id: str, current_seq: int,
              redo_stack: list[int]) -> None:
    conn.execute(
        "INSERT INTO ledger_book_state (book_id, current_seq, redo_stack) VALUES (?, ?, ?) "
        "ON CONFLICT(book_id) DO UPDATE SET current_seq = excluded.current_seq, "
        "redo_stack = excluded.redo_stack",
        (book_id, int(current_seq), json.dumps(list(redo_stack))),
    )


def save_snapshot(
    conn: sqlite3.Connection,
    book_id: str,
    seq: int,
    label: str,
    tree_json: dict,
    snapshot_id: str | None = None,
) -> str:
    sid = snapshot_id or f"{book_id}:s{seq:06d}"
    conn.execute(
        "INSERT OR REPLACE INTO ledger_snapshots "
        "(snapshot_id, book_id, seq, label, tree_json) VALUES (?, ?, ?, ?, ?)",
        (sid, book_id, seq, label, json.dumps(tree_json, ensure_ascii=False)),
    )
    return sid


def update_book_meta(conn: sqlite3.Connection, book_id: str, name: str | None = None,
                     note: str | None = None) -> None:
    sets, args = [], []
    if name is not None:
        sets.append("name = ?")
        args.append(name)
    if note is not None:
        sets.append("note = ?")
        args.append(note)
    if not sets:
        return
    args.append(book_id)
    conn.execute(
        f"UPDATE ledger_books SET {', '.join(sets)}, "
        "updated_at = datetime('now') WHERE book_id = ?",
        args,
    )


def assert_book_exists(conn: sqlite3.Connection, book_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM ledger_books WHERE book_id = ?", (book_id,)
    ).fetchone()
    if row is None:
        raise LedgerError(
            f"没有这本账（{book_id}）。",
            code=Code.BOOK_NOT_FOUND,
            http_status=404,
            detail={"book_id": book_id},
        )
