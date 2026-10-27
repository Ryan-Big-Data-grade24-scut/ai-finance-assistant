"""
backend/ledger/loader.py —— 账本侧**唯一**的 SQL 入口（只读）

**纪律**：账本层的 SQL 全部集中在这里，其它模块不写 SQL。
`store.py` 是唯一写 `ledger_*` 表的模块；本文件只 SELECT。

**纪律二**：只读 `v_tx`（已含 funding / category / is_exempt），
**绝不查 `v_tx_counted`** —— 那是第一轮的口径，账本不继承它。
账本层也不 import `backend.reports` / `backend.overrides`。
"""
from __future__ import annotations

import sqlite3

from .amount import d
from .atoms import TxRow
from .errors import Code, LedgerError
from .tree import Book, Node, Tree

# ---- 从 v_tx 读一笔交易的全部字段 ----
# v_tx 不暴露 merchant_no（退款配对要用），所以 JOIN 回原始层补一列。
# 只读，不改 v_tx —— 第一轮的 7 个报告模块都 FROM 它，改它会连带出事。
_TX_COLUMNS = (
    "v.id, v.date, v.tx_time, v.direction, v.amount, v.item, v.tx_type, "
    "v.counterparty, v.pay_method, r.merchant_no, v.tx_no, v.funding, "
    "v.category, v.is_exempt"
)
_TX_FROM = "FROM v_tx v JOIN transactions_raw r ON r.id = v.id"


def _row_to_tx(row: sqlite3.Row) -> TxRow:
    return TxRow(
        tx_id=int(row["id"]),
        date=row["date"] or "",
        tx_time=row["tx_time"] or "",
        raw_direction=row["direction"] or "",
        amount=d(row["amount"] or 0),
        item=row["item"] or "",
        tx_type=row["tx_type"] or "",
        counterparty=row["counterparty"] or "",
        pay_method=row["pay_method"] or "",
        merchant_no=row["merchant_no"] or "",
        tx_no=row["tx_no"] or "",
        funding=row["funding"] or "自有",
        category=row["category"] or "",
        is_exempt=bool(row["is_exempt"]),
    )


# ──────────────────────────────────────────────────────────────
#  账本
# ──────────────────────────────────────────────────────────────

def list_books(conn: sqlite3.Connection) -> list[Book]:
    """所有账本，按创建时间倒序。"""
    rows = conn.execute(
        "SELECT book_id, name, date_from, date_to, root_node_id, created_by, created_at "
        "FROM ledger_books ORDER BY created_at DESC, book_id DESC"
    ).fetchall()
    return [
        Book(
            book_id=r["book_id"],
            name=r["name"],
            date_from=r["date_from"],
            date_to=r["date_to"],
            root_node_id=r["root_node_id"],
            created_by=r["created_by"] or "ui",
            created_at=r["created_at"] or "",
        )
        for r in rows
    ]


def get_book(conn: sqlite3.Connection, book_id: str) -> Book | None:
    row = conn.execute(
        "SELECT book_id, name, date_from, date_to, root_node_id, created_by, created_at "
        "FROM ledger_books WHERE book_id = ?",
        (book_id,),
    ).fetchone()
    if row is None:
        return None
    return Book(
        book_id=row["book_id"],
        name=row["name"],
        date_from=row["date_from"],
        date_to=row["date_to"],
        root_node_id=row["root_node_id"],
        created_by=row["created_by"] or "ui",
        created_at=row["created_at"] or "",
    )


def require_book(conn: sqlite3.Connection, book_id: str) -> Book:
    book = get_book(conn, book_id)
    if book is None:
        raise LedgerError(
            f"没有这本账（{book_id}）。",
            code=Code.BOOK_NOT_FOUND,
            http_status=404,
            detail={"book_id": book_id},
        )
    return book


# ──────────────────────────────────────────────────────────────
#  成员交易
# ──────────────────────────────────────────────────────────────

def load_book_txs(conn: sqlite3.Connection, book_id: str) -> list[TxRow]:
    """本账的成员交易（按时间排序）。

    只读 `v_tx` —— 那里已经缝好了修正层的 funding / category / is_exempt。
    """
    rows = conn.execute(
        f"SELECT {_TX_COLUMNS} {_TX_FROM} "
        "WHERE v.id IN (SELECT tx_id FROM ledger_book_tx WHERE book_id = ?) "
        "ORDER BY v.tx_time, v.id",
        (book_id,),
    ).fetchall()
    return [_row_to_tx(r) for r in rows]


def load_txs_by_ids(conn: sqlite3.Connection, tx_ids: list[int]) -> dict[int, TxRow]:
    """按 id 取一批交易（attach 时用）。"""
    if not tx_ids:
        return {}
    out: dict[int, TxRow] = {}
    for i in range(0, len(tx_ids), 400):
        chunk = tx_ids[i:i + 400]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT {_TX_COLUMNS} {_TX_FROM} WHERE v.id IN ({marks})",
            chunk,
        ).fetchall()
        for r in rows:
            out[int(r["id"])] = _row_to_tx(r)
    return out


def txs_in_range(conn: sqlite3.Connection, date_from: str, date_to: str) -> list[TxRow]:
    """区间内的全部交易 —— 建账时冻结成员集用。"""
    rows = conn.execute(
        f"SELECT {_TX_COLUMNS} {_TX_FROM} "
        "WHERE v.date BETWEEN ? AND ? ORDER BY v.tx_time, v.id",
        (date_from, date_to),
    ).fetchall()
    return [_row_to_tx(r) for r in rows]


# ──────────────────────────────────────────────────────────────
#  树
# ──────────────────────────────────────────────────────────────

def load_tree(conn: sqlite3.Connection, book: Book) -> Tree:
    """读回一棵树。"""
    tree = Tree(book_id=book.book_id, root_id=book.root_node_id)
    for r in conn.execute(
        "SELECT node_id, parent_id, name, op, kind, sort_key "
        "FROM ledger_nodes WHERE book_id = ?",
        (book.book_id,),
    ).fetchall():
        tree.nodes[r["node_id"]] = Node(
            node_id=r["node_id"],
            parent_id=r["parent_id"],
            name=r["name"] or "",
            op=r["op"] or "ADD",
            kind=r["kind"] or "group",
            sort_key=int(r["sort_key"] or 0),
        )
    for r in conn.execute(
        "SELECT tx_id, node_id, role FROM ledger_legs WHERE book_id = ?",
        (book.book_id,),
    ).fetchall():
        tree.legs[(int(r["tx_id"]), r["role"])] = r["node_id"]
    return tree


def load_state(conn: sqlite3.Connection, book_id: str) -> tuple[int, list[int]]:
    """读撤销/重做状态：(current_seq, redo_stack)。"""
    row = conn.execute(
        "SELECT current_seq, redo_stack FROM ledger_book_state WHERE book_id = ?",
        (book_id,),
    ).fetchone()
    if row is None:
        return 0, []
    import json
    try:
        redo = json.loads(row["redo_stack"] or "[]")
    except json.JSONDecodeError:
        redo = []
    return int(row["current_seq"] or 0), [int(x) for x in redo]


def load_ops(conn: sqlite3.Connection, book_id: str) -> list[dict]:
    """读操作日志，按 seq 升序。"""
    import json
    rows = conn.execute(
        "SELECT seq, op_type, payload, inverse_type, inverse_payload, summary, "
        "created_by, created_at FROM ledger_ops WHERE book_id = ? ORDER BY seq",
        (book_id,),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "seq": int(r["seq"]),
            "op_type": r["op_type"],
            "payload": json.loads(r["payload"] or "{}"),
            "inverse_type": r["inverse_type"],
            "inverse_payload": json.loads(r["inverse_payload"] or "{}"),
            "summary": json.loads(r["summary"] or "{}") if r["summary"] else {},
            "created_by": r["created_by"] or "ui",
            "created_at": r["created_at"] or "",
        })
    return out


def load_snapshot(conn: sqlite3.Connection, book_id: str, seq: int) -> dict | None:
    import json
    row = conn.execute(
        "SELECT snapshot_id, seq, label, tree_json FROM ledger_snapshots "
        "WHERE book_id = ? AND seq = ?",
        (book_id, seq),
    ).fetchone()
    if row is None:
        return None
    return {
        "snapshot_id": row["snapshot_id"],
        "seq": int(row["seq"]),
        "label": row["label"] or "",
        "tree": json.loads(row["tree_json"]),
    }
