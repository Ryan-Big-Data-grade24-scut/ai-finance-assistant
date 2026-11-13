"""账本侧唯一只读 SQL 入口：读取冻结成员的不可变事实。"""
from __future__ import annotations
import json
from .errors import Code, LedgerError


def raw_transactions(conn, tx_ids: list[int]) -> list[dict]:
    if not tx_ids:
        return []
    # 使用主表，而非 v_tx_counted：一本账的成员 X 冻结后，不被旧报表口径隐式改变。
    found = []
    for index in range(0, len(tx_ids), 400):
        part = tx_ids[index:index + 400]
        q = ','.join('?' for _ in part)
        found.extend(dict(r) for r in conn.execute(
            'SELECT t.id, t.tx_time, t.date, t.direction, t.amount, t.source, t.counterparty, t.item, '
            't.pay_method, t.tx_type, t.status, t.tx_no, t.merchant_no, t.remark, t.raw_json, '
            "COALESCE(o.funding, '自有') AS funding "
            f'FROM transactions_raw t LEFT JOIN tx_overrides o ON o.tx_id=t.id WHERE t.id IN ({q})', part
        ).fetchall())
    return sorted(found, key=lambda row: (row['tx_time'], row['id']))


def read_book(conn, book_id: str) -> dict:
    row = conn.execute('SELECT * FROM ledger_books WHERE id=?', (book_id,)).fetchone()
    if not row:
        raise LedgerError(Code.BOOK_NOT_FOUND, '没有找到这本账，请刷新页面。', 404)
    return dict(row)


def read_state(conn, book_id: str, *, allow_legacy_placements: bool = False) -> dict:
    """Read the current book state.

    Current leg destinations are read from ledger_legs (primary rows). The current
    history snapshot is retained as the revision/audit witness and supplies split
    metadata. The legacy ledger_placements table is deliberately ignored except when
    an explicit migration caller opts into allow_legacy_placements.
    """
    book = read_book(conn, book_id)
    nodes = [dict(row) for row in conn.execute(
        'SELECT id, parent_id, name, op, kind FROM ledger_nodes WHERE book_id=?', (book_id,)
    )]
    members = [int(row['tx_id']) for row in conn.execute(
        'SELECT tx_id FROM ledger_book_tx WHERE book_id=? ORDER BY tx_id', (book_id,)
    )]

    history = conn.execute(
        'SELECT state_json FROM ledger_history WHERE book_id=? AND seq=?',
        (book_id, book['current_seq'])
    ).fetchone()
    if not history:
        raise LedgerError(
            Code.LEG_PERSISTENCE_MISMATCH,
            '当前账本版本缺少历史快照，无法安全读取记账腿。',
            500,
        )
    try:
        snapshot = json.loads(history['state_json'])
    except (TypeError, json.JSONDecodeError) as exc:
        raise LedgerError(
            Code.LEG_PERSISTENCE_MISMATCH,
            '当前账本历史快照无法解析，请先检查数据库。',
            500,
        ) from exc

    if not isinstance(snapshot, dict):
        raise LedgerError(
            Code.LEG_PERSISTENCE_MISMATCH,
            '当前账本历史快照格式无效，请先检查数据库。',
            500,
        )

    snapshot_members = snapshot.get('members')
    snapshot_placements = snapshot.get('placements')
    snapshot_nodes = snapshot.get('nodes')
    if not isinstance(snapshot_members, list) or not isinstance(snapshot_placements, dict) or not isinstance(snapshot_nodes, list):
        raise LedgerError(
            Code.LEG_PERSISTENCE_MISMATCH,
            '当前账本历史快照缺少成员、归属或节点信息。',
            500,
        )

    try:
        normalized_snapshot_members = sorted(int(tx_id) for tx_id in snapshot_members)
        normalized_snapshot_placements = {
            str(int(tx_id)): str(node_id)
            for tx_id, node_id in snapshot_placements.items()
        }
        normalized_snapshot_nodes = sorted(
            (
                str(node['id']),
                None if node.get('parent_id') is None else str(node['parent_id']),
                str(node['name']),
                str(node['op']),
                str(node['kind']),
            )
            for node in snapshot_nodes
        )
        normalized_current_nodes = sorted(
            (
                str(node['id']),
                None if node.get('parent_id') is None else str(node['parent_id']),
                str(node['name']),
                str(node['op']),
                str(node['kind']),
            )
            for node in nodes
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerError(
            Code.LEG_PERSISTENCE_MISMATCH,
            '当前账本历史快照包含无效的节点或成员。',
            500,
        ) from exc

    if normalized_snapshot_members != members:
        raise LedgerError(
            Code.LEG_PERSISTENCE_MISMATCH,
            '当前账本成员表与历史快照不一致。',
            500,
        )
    if normalized_snapshot_nodes != normalized_current_nodes:
        raise LedgerError(
            Code.LEG_PERSISTENCE_MISMATCH,
            '当前账本节点表与历史快照不一致。',
            500,
        )

    # The primary leg is the only current placement source. The legacy table is
    # available solely to the explicit migration path, never to normal reads/writes.
    primary_rows = [
        row for row in read_legs(conn, book_id)
        if row['role'] == 'primary'
    ]
    primary_placements = {
        str(int(row['tx_id'])): str(row['node_id'])
        for row in primary_rows
    }
    if len(primary_placements) != len(primary_rows):
        raise LedgerError(
            Code.LEG_PERSISTENCE_MISMATCH,
            'ledger_legs 中存在重复的主腿归属。',
            500,
        )

    if allow_legacy_placements:
        legacy_rows = conn.execute(
            'SELECT tx_id, node_id FROM ledger_placements WHERE book_id=?',
            (book_id,),
        ).fetchall()
        legacy_placements = {
            str(int(row['tx_id'])): str(row['node_id'])
            for row in legacy_rows
        }
        if legacy_placements:
            if legacy_placements != normalized_snapshot_placements:
                raise LedgerError(
                    Code.LEG_PERSISTENCE_MISMATCH,
                    '旧归属表与当前历史快照不一致，迁移已停止。',
                    500,
                )
            placements = legacy_placements
        elif primary_rows:
            if primary_placements != normalized_snapshot_placements:
                raise LedgerError(
                    Code.LEG_PERSISTENCE_MISMATCH,
                    '当前主腿归属与账本历史快照不一致，请检查 ledger_legs。',
                    500,
                )
            placements = primary_placements
        else:
            # Explicit migration only: if both projections are empty, the validated
            # revision snapshot is the safe source from which to backfill all legs.
            placements = normalized_snapshot_placements
    else:
        placements = primary_placements
        if placements != normalized_snapshot_placements:
            raise LedgerError(
                Code.LEG_PERSISTENCE_MISMATCH,
                '当前主腿归属与账本历史快照不一致，请检查 ledger_legs。',
                500,
            )

    splits = snapshot.get('splits') or {}
    if not isinstance(splits, dict):
        raise LedgerError(
            Code.LEG_PERSISTENCE_MISMATCH,
            '当前账本分裂记录格式无效。',
            500,
        )
    return {
        'nodes': nodes,
        'members': members,
        'placements': placements,
        'splits': splits,
    }


def book_source(conn, date_from: str, date_to: str) -> list[dict]:
    # 仅在建账时使用第一轮的可见/有效成员；此后成员被冻结。
    return [dict(r) for r in conn.execute(
        'SELECT id, category, subcat FROM v_tx WHERE date BETWEEN ? AND ? '
        'AND is_exempt=0 ORDER BY id', (date_from, date_to)
    )]

def read_legs(conn, book_id: str) -> list[dict]:
    """读取当前版本已物化的腿结构，不读取/保存金额。"""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT book_id, tx_id, node_id, role FROM ledger_legs "
            "WHERE book_id=? ORDER BY tx_id, CASE role WHEN 'primary' THEN 0 ELSE 1 END",
            (book_id,),
        )
    ]
