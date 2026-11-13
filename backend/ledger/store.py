"""账本侧唯一写 ledger_* 的模块。所有写入在同一 SQLite 事务内。"""
from __future__ import annotations
import json


def create_book(conn, book: dict, state: dict, legs: list[dict]) -> None:
    conn.execute('INSERT INTO ledger_books(id,name,date_from,date_to) VALUES(?,?,?,?)',
                 (book['id'], book['name'], book['date_from'], book['date_to']))
    _replace_state(conn, book['id'], state, legs)
    conn.execute('INSERT INTO ledger_history(book_id,seq,label,state_json) VALUES(?,?,?,?)',
                 (book['id'], 0, '初始分类', json.dumps(state, ensure_ascii=False)))


def _insert_leg_structure(conn, book_id: str, legs: list[dict]) -> None:
    """只持久化腿的结构；e/i/amount 始终由 atoms + invariants 实时计算。"""
    rows = [
        (book_id, int(leg['tx_id']), str(leg['node_id']), str(leg['role']))
        for leg in legs
    ]
    conn.executemany(
        'INSERT INTO ledger_legs(book_id,tx_id,node_id,role) VALUES(?,?,?,?)',
        rows,
    )


def replace_leg_structure(conn, book_id: str, legs: list[dict]) -> None:
    """重建一本账当前版本的腿结构；供迁移和唯一写路径使用。"""
    conn.execute('DELETE FROM ledger_legs WHERE book_id=?', (book_id,))
    _insert_leg_structure(conn, book_id, legs)


def clear_legacy_placements(conn, book_id: str) -> None:
    """Clear the retired one-row-per-transaction placement projection."""
    conn.execute('DELETE FROM ledger_placements WHERE book_id=?', (book_id,))


def _replace_state(conn, book_id: str, state: dict, legs: list[dict]) -> None:
    # ledger_legs is the only current placement source. Clear the retired
    # ledger_placements shadow before replacing nodes/members to satisfy old FKs.
    conn.execute('DELETE FROM ledger_legs WHERE book_id=?', (book_id,))
    clear_legacy_placements(conn, book_id)
    conn.execute('DELETE FROM ledger_nodes WHERE book_id=?', (book_id,))
    conn.execute('DELETE FROM ledger_book_tx WHERE book_id=?', (book_id,))
    conn.executemany('INSERT INTO ledger_book_tx(book_id,tx_id) VALUES(?,?)',
                     ((book_id, tx_id) for tx_id in state['members']))
    # 插入时 parent FK 会检验，按父先于子的拓扑排序。
    pending = list(state['nodes']); written: set[str] = set()
    while pending:
        next_round = []
        for node in pending:
            if node['parent_id'] is None or node['parent_id'] in written:
                conn.execute('INSERT INTO ledger_nodes(id,book_id,parent_id,name,op,kind) VALUES(?,?,?,?,?,?)',
                             (node['id'], book_id, node['parent_id'], node['name'], node['op'], node['kind']))
                written.add(node['id'])
            else:
                next_round.append(node)
        if len(next_round) == len(pending):
            raise ValueError('Invalid ledger parent order')
        pending = next_round
    _insert_leg_structure(conn, book_id, legs)


def save_revision(conn, book_id: str, current_seq: int, label: str,
                  state: dict, legs: list[dict]) -> None:
    _replace_state(conn, book_id, state, legs)
    conn.execute('DELETE FROM ledger_history WHERE book_id=? AND seq>?', (book_id, current_seq))
    conn.execute('INSERT INTO ledger_history(book_id,seq,label,state_json) VALUES(?,?,?,?)',
                 (book_id, current_seq + 1, label, json.dumps(state, ensure_ascii=False)))
    conn.execute('UPDATE ledger_books SET current_seq=? WHERE id=?', (current_seq + 1, book_id))


def restore_revision(conn, book_id: str, seq: int, state: dict, legs: list[dict]) -> None:
    _replace_state(conn, book_id, state, legs)
    conn.execute('UPDATE ledger_books SET current_seq=? WHERE id=?', (seq, book_id))


def sync_live_membership(conn, book_id: str, current_seq: int,
                         snapshots: dict[int, dict], legs: list[dict]) -> None:
    """Refresh input membership, without treating imports as Undo-able user edits.

    The live book treats import changes as a new set X applied consistently across
    all saved M versions. Each snapshot is validated by service before entering.
    """
    if current_seq not in snapshots:
        raise ValueError('Missing current revision during live sync')
    for seq, state in snapshots.items():
        conn.execute('UPDATE ledger_history SET state_json=? WHERE book_id=? AND seq=?',
                     (json.dumps(state, ensure_ascii=False), book_id, seq))
    _replace_state(conn, book_id, snapshots[current_seq], legs)
