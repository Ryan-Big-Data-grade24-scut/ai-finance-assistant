"""HTTP/未来 MCP 共用的唯一账本服务。独立于第一轮报告口径。"""
from __future__ import annotations
import json
from uuid import uuid4
from .. import db
from . import loader, ops, store
from .amount import money, cents
from .atoms import analyze_special_transactions
from .errors import Code, LedgerError

LIVE_BOOK_ID = 'star-live'
from .invariants import validate


def _book_summary(conn, book_id: str) -> dict:
    b = loader.read_book(conn, book_id)
    b['tx_count'] = conn.execute('SELECT count(*) FROM ledger_book_tx WHERE book_id=?', (book_id,)).fetchone()[0]
    return b


def _assert_persisted_leg_structure(conn, book_id: str, generated_legs: list[dict]) -> None:
    """Ensure the persisted structural projection matches deterministic generation."""
    expected = sorted(
        (
            int(leg['tx_id']),
            str(leg['node_id']),
            str(leg['role']),
        )
        for leg in generated_legs
    )
    actual = sorted(
        (
            int(leg['tx_id']),
            str(leg['node_id']),
            str(leg['role']),
        )
        for leg in loader.read_legs(conn, book_id)
    )
    if actual != expected:
        raise LedgerError(
            Code.LEG_PERSISTENCE_MISMATCH,
            '已保存的记账腿结构与账本状态不一致，请先运行 ledger_legs 数据迁移。',
            500,
        )


def list_books() -> dict:
    conn = db.get_conn()
    try:
        books = [_book_summary(conn, row['id']) for row in conn.execute('SELECT id FROM ledger_books ORDER BY created_at DESC,id DESC')]
        return {'books': books}
    finally:
        conn.close()


def create_book(name: str, date_from: str, date_to: str) -> dict:
    if not name.strip() or date_from > date_to:
        raise LedgerError(Code.INVALID_BOOK, '请填写账本名以及正确的起止日期。')
    book_id = uuid4().hex[:12]
    root_id = f'{book_id}:root'
    state = {'members': [], 'placements': {}, 'splits': {}, 'nodes': [
        {'id': root_id, 'parent_id': None, 'name': '全部账目', 'op': 'ADD', 'kind': 'root'}
    ]}
    conn = db.get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        rows = loader.book_source(conn, date_from, date_to)
        categories: dict[tuple[str, str], str] = {}
        for row in rows:
            major = (row['category'] or '其他').strip() or '其他'
            minor = (row['subcat'] or '未细分').strip() or '未细分'
            for part in ((major, ''), (major, minor)):
                if part not in categories:
                    parent = root_id if not part[1] else categories[(major, '')]
                    node_id = f'{book_id}:n{len(categories) + 1:04d}'
                    categories[part] = node_id
                    state['nodes'].append({'id': node_id, 'name': part[1] or part[0],
                                           'op': 'ADD', 'kind': 'group', 'parent_id': parent})
            state['members'].append(int(row['id']))
            state['placements'][str(row['id'])] = categories[(major, minor)]
        proof = validate(state, loader.raw_transactions(conn, state['members']), book_id)
        store.create_book(
            conn,
            {'id': book_id, 'name': name.strip(), 'date_from': date_from, 'date_to': date_to},
            state,
            proof['legs'],
        )
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
    return get_tree(book_id)


def _source_rows(conn) -> list[dict]:
    """Live visualization includes *all visible imported rows*, even exempt rows.

    Deletion tombstones remain authoritative (v_tx removes those).  Report calculations
    are deliberately unaffected.  The live book has a different membership policy
    from the legacy date-frozen books.
    """
    return [dict(r) for r in conn.execute(
        'SELECT id, category, subcat FROM v_tx ORDER BY id'
    )]


def _categorized_state(book_id: str, rows: list[dict]) -> dict:
    root = f'{book_id}:root'
    state = {'members': [], 'placements': {}, 'splits': {}, 'nodes': [
        {'id': root, 'parent_id': None, 'name': '全部账目', 'op': 'ADD', 'kind': 'root'}
    ]}
    _assign_new_rows(state, rows, book_id)
    return state


def _assign_new_rows(state: dict, rows: list[dict], book_id: str) -> None:
    """Attach new records, reusing category paths and preserving all old placements."""
    nodes = {n['id']: n for n in state['nodes']}
    paths = {(n['parent_id'], n['name']): n['id'] for n in state['nodes'] if n['kind'] == 'group'}
    root = f'{book_id}:root'
    used = set(nodes)
    next_id = 1
    def new_node(parent: str, name: str) -> str:
        nonlocal next_id
        while f'{book_id}:auto{next_id:06d}' in used:
            next_id += 1
        node_id = f'{book_id}:auto{next_id:06d}'
        used.add(node_id)
        node = {'id': node_id, 'parent_id': parent, 'name': name, 'op': 'ADD', 'kind': 'group'}
        state['nodes'].append(node)
        nodes[node_id] = node
        paths[(parent, name)] = node_id
        next_id += 1
        return node_id
    def find_or_create(parent: str, name: str) -> str:
        return paths.get((parent, name)) or new_node(parent, name)
    for row in rows:
        tx_id = int(row['id'])
        if str(tx_id) in state['placements']:
            continue
        major = (row['category'] or '其他').strip() or '其他'
        minor = (row['subcat'] or '未细分').strip() or '未细分'
        major_id = find_or_create(root, major)
        minor_id = find_or_create(major_id, minor)
        state['members'].append(tx_id)
        state['placements'][str(tx_id)] = minor_id


def sync_live_book() -> dict:
    """Idempotently initialize/synchronize the sole automatic star map book.

    Uses the same store and invariant checks as manual operations. Existing moved
    records and user-defined groups are preserved. Never mutates transactions_raw,
    tx_overrides, or the legacy report layer.
    """
    conn = db.get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        rows = _source_rows(conn)
        ids = {int(r['id']) for r in rows}
        present = conn.execute('SELECT 1 FROM ledger_books WHERE id=?', (LIVE_BOOK_ID,)).fetchone()
        if not present:
            state = _categorized_state(LIVE_BOOK_ID, rows)
            proof = validate(state, loader.raw_transactions(conn, state['members']), LIVE_BOOK_ID)
            store.create_book(
                conn,
                {'id': LIVE_BOOK_ID, 'name': '自动同步 · 全部流水',
                 'date_from': '0001-01-01', 'date_to': '9999-12-31'},
                state,
                proof['legs'],
            )
        else:
            current = loader.read_book(conn, LIVE_BOOK_ID)
            state = loader.read_state(conn, LIVE_BOOK_ID)
            current_facts = loader.raw_transactions(conn, state['members'])
            current_proof = validate(state, current_facts, LIVE_BOOK_ID)
            _assert_persisted_leg_structure(conn, LIVE_BOOK_ID, current_proof['legs'])
            if set(state['members']) != ids:
                # New imports are a change of INPUT X, not a user change of M.
                # Merge input updates into *every* historical version so Undo/Redo
                # continue to revert only the user's structural edits.
                revised: dict[int, dict] = {}
                current_legs: list[dict] | None = None
                history = list(conn.execute(
                    'SELECT seq,state_json FROM ledger_history WHERE book_id=? ORDER BY seq',
                    (LIVE_BOOK_ID,)))
                for h in history:
                    snap = json.loads(h['state_json'])
                    snap.setdefault('splits', {})
                    removed = set(snap['members']) - ids
                    if removed:
                        snap['members'] = [tx for tx in snap['members'] if tx not in removed]
                        for tx in removed:
                            snap['placements'].pop(str(tx), None)
                            snap['splits'].pop(str(tx), None)
                    _assign_new_rows(snap,
                        [r for r in rows if int(r['id']) not in snap['members']], LIVE_BOOK_ID)
                    snapshot_facts = loader.raw_transactions(conn, snap['members'])
                    # 新导入的全额退款/内部转账/退款证据冲突会使旧分裂失效；
                    # 合法部分退款保留分裂，但镜像金额按退款后的剩余金额重算。
                    snap = ops.reconcile_special_splits(snap, snapshot_facts)
                    snapshot_proof = validate(snap, snapshot_facts, LIVE_BOOK_ID)
                    revised[h['seq']] = snap
                    if int(h['seq']) == int(current['current_seq']):
                        current_legs = snapshot_proof['legs']
                if current_legs is None:
                    raise ValueError('Live book current revision was not rebuilt')
                store.sync_live_membership(
                    conn, LIVE_BOOK_ID, current['current_seq'], revised, current_legs
                )
        conn.commit()
        return {'imported_visible': len(ids), 'db_path': str(db.DB_PATH)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _filter_live_state(state: dict, facts: list[dict], date_from: str | None,
                       date_to: str | None, category_node_id: str | None, book_id: str,
                       special: dict | None = None) -> dict:
    from datetime import date as _date
    if date_from:
        try: _date.fromisoformat(date_from)
        except ValueError: raise LedgerError(Code.INVALID_DATE, '开始日期格式不正确。')
    if date_to:
        try: _date.fromisoformat(date_to)
        except ValueError: raise LedgerError(Code.INVALID_DATE, '结束日期格式不正确。')
    if date_from and date_to and date_from > date_to:
        raise LedgerError(Code.INVALID_DATE, '开始日期不能晚于结束日期。')
    children: dict[str, list[str]] = {}
    for n in state['nodes']:
        if n['parent_id']:
            children.setdefault(n['parent_id'], []).append(n['id'])
    allowed_nodes: set[str] | None = None
    if category_node_id:
        if category_node_id not in {n['id'] for n in state['nodes']}:
            raise LedgerError(Code.NODE_NOT_FOUND, '所选分类不存在，请重新选择。', 404)
        allowed_nodes = set()
        def collect(nid: str):
            allowed_nodes.add(nid)
            for child in children.get(nid, []): collect(child)
        collect(category_node_id)
    selected = {int(tx['id']) for tx in facts
        if (not date_from or tx['date'] >= date_from)
        and (not date_to or tx['date'] <= date_to)
        and (allowed_nodes is None or state['placements'][str(tx['id'])] in allowed_nodes)}
    selected_splits = {k: v for k, v in (state.get('splits') or {}).items() if int(k) in selected}
    nodes = list(state['nodes'])
    if selected_splits:
        # Keep the external-income node only when this view contains a split primary leg.
        pass
    else:
        nodes = [n for n in nodes if n.get('kind') != 'external_income']
    result = {'nodes': nodes, 'members': [i for i in state['members'] if i in selected],
              'placements': {str(i): state['placements'][str(i)] for i in selected},
              'splits': selected_splits}
    if special:
        result['_refund_zero_ids'] = list(special.get('refund_zero_ids', set()))
        result['_transfer_zero_ids'] = list(special.get('transfer_zero_ids', set()))
        result['_partial_refund_ids'] = list(special.get('partial_refund_ids', set()))
        result['_refund_conflict_ids'] = list(special.get('refund_conflict_ids', set()))
        result['_refund_amounts_by_original'] = {
            str(key): int(value)
            for key, value in special.get('refund_amounts_by_original', {}).items()
            if int(key) in selected
        }
        result['_refund_pairs'] = list(special.get('refund_pairs', []))
        result['_transfer_pairs'] = list(special.get('transfer_pairs', []))
        result['_special_warnings'] = list(special.get('warnings', []))
    return result


def get_live_tree(date_from: str | None = None, date_to: str | None = None,
                  category_node_id: str | None = None) -> dict:
    diagnostics = sync_live_book()
    conn = db.get_conn()
    try:
        full = loader.read_state(conn, LIVE_BOOK_ID)
        full_facts = loader.raw_transactions(conn, full['members'])
        full_proof = validate(full, full_facts, LIVE_BOOK_ID)
        _assert_persisted_leg_structure(conn, LIVE_BOOK_ID, full_proof['legs'])
        full_special = analyze_special_transactions(full_facts)
        selected = _filter_live_state(full, full_facts, date_from, date_to, category_node_id,
                                      LIVE_BOOK_ID, full_special)
        result = _build(conn, LIVE_BOOK_ID, override_state=selected)
        result.update({'source_count': diagnostics['imported_visible'],
                       'database_path': diagnostics['db_path'],
                       'total_count': len(full['members']),
                       'filters': {'date_from': date_from, 'date_to': date_to,
                                   'category_node_id': category_node_id}})
        return result
    finally:
        conn.close()


def _build(conn, book_id: str, override_state: dict | None = None) -> dict:
    book = _book_summary(conn, book_id)
    state = override_state if override_state is not None else loader.read_state(conn, book_id)
    facts = loader.raw_transactions(conn, state['members'])
    proof = validate(state, facts, book_id)
    if override_state is None:
        _assert_persisted_leg_structure(conn, book_id, proof['legs'])
    # validate() 已按当前完整账本或筛选视图的权威特殊交易元数据生成腿，
    # 展示状态直接复用同一份结果，避免 UI 与金额计算口径分叉。
    special = {
        'refund_zero_ids': proof['refund_zero_ids'],
        'transfer_zero_ids': proof['transfer_zero_ids'],
        'partial_refund_ids': proof['partial_refund_ids'],
        'refund_conflict_ids': proof['refund_conflict_ids'],
        'refund_amounts_by_original': proof['refund_amounts_by_original'],
        'refund_pairs': proof['refund_pairs'],
        'transfer_pairs': proof['transfer_pairs'],
        'warnings': proof['warnings'],
    }
    txs = []
    for tx in facts:
        tx_id = int(tx['id'])
        income = tx['direction'] == '收入' or (tx['direction'] == '中性' and '收益发放' in (tx['item'] or ''))
        original_amount_cents = cents(tx['amount'])
        refunded_cents = int(special['refund_amounts_by_original'].get(tx_id, 0))
        is_refund_zeroed = tx_id in special['refund_zero_ids']
        is_transfer_zeroed = tx_id in special['transfer_zero_ids']
        effective_amount_cents = (
            0 if is_refund_zeroed or is_transfer_zeroed
            else max(0, original_amount_cents - refunded_cents)
        )
        txs.append({'id': tx['id'], 'date': tx['date'], 'tx_time': tx['tx_time'],
                    'direction': 'income' if income else 'expense', 'source': tx['source'],
                    'amount': money(original_amount_cents),
                    'refunded_amount': money(refunded_cents),
                    'effective_amount': money(effective_amount_cents),
                    'counterparty': tx['counterparty'] or '', 'item': tx['item'] or '',
                    'pay_method': tx['pay_method'] or '', 'funding': tx.get('funding') or '自有',
                    'tx_no': tx.get('tx_no') or '', 'merchant_no': tx.get('merchant_no') or '',
                    'status': tx.get('status') or '', 'tx_type': tx.get('tx_type') or '',
                    'node_id': state['placements'][str(tx['id'])],
                    'refund_zeroed': is_refund_zeroed,
                    'partial_refund': tx_id in special['partial_refund_ids'],
                    'refund_conflict': tx_id in special['refund_conflict_ids'],
                    'internal_transfer_zeroed': is_transfer_zeroed,
                    'split': str(tx['id']) in (state.get('splits') or {})})
    nodes = [{**n, **{'e': money(proof['totals'][n['id']]['e']),
                    'i': money(proof['totals'][n['id']]['i']),
                    'net': money(proof['totals'][n['id']]['net']),
                    'count': proof['totals'][n['id']]['count']}} for n in state['nodes']]
    legs = [{**leg, 'e': money(leg['e']), 'i': money(leg['i'])} for leg in proof['legs']]
    next_seq = conn.execute('SELECT MAX(seq) FROM ledger_history WHERE book_id=?', (book_id,)).fetchone()[0] or 0
    return {'book': book, 'root_id': f'{book_id}:root', 'nodes': nodes, 'transactions': txs,
            'legs': legs, 'root_e': money(proof['root_e']), 'root_i': money(proof['root_i']),
            'root_net': money(proof['root_net']), 'count': proof['count'],
            'invariants_ok': True, 'invariants': proof['invariants'], 'audit_ok': proof['audit_ok'], 'warnings': proof['warnings'],
            'audit': {key: money(value) for key, value in proof['audit'].items()},
            'refund_pairs': proof['refund_pairs'],
            'refund_amounts_by_original': {
                str(key): money(value)
                for key, value in proof['refund_amounts_by_original'].items()
            },
            'refund_conflict_ids': sorted(proof['refund_conflict_ids']),
            'internal_transfer_pairs': proof['transfer_pairs'],
            'split_count': len(state.get('splits') or {}), 'seq': book['current_seq'],
            'can_undo': book['current_seq'] > 0, 'can_redo': book['current_seq'] < next_seq}


def migrate_ledger_legs() -> dict:
    """迁移当前腿归属权威来源，并清理旧 ledger_placements 投影。

    可重复执行：优先验证现有 ledger_legs；缺失时才从已校验的历史快照/
    旧归属表回填。若发现非空腿结构与快照冲突则停止，不自动覆盖。
    不修改原始流水和历史快照。
    """
    conn = db.get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        book_ids = [
            str(row['id'])
            for row in conn.execute('SELECT id FROM ledger_books ORDER BY id')
        ]
        total_legs = 0
        for book_id in book_ids:
            # Explicit migration may read the legacy placement table or the
            # revision snapshot to backfill ledger_legs. Normal application reads
            # never use ledger_placements.
            state = loader.read_state(
                conn, book_id, allow_legacy_placements=True
            )
            facts = loader.raw_transactions(conn, state['members'])
            proof = validate(state, facts, book_id)
            expected = sorted(
                (int(leg['tx_id']), str(leg['node_id']), str(leg['role']))
                for leg in proof['legs']
            )
            actual = sorted(
                (int(leg['tx_id']), str(leg['node_id']), str(leg['role']))
                for leg in loader.read_legs(conn, book_id)
            )
            if actual and actual != expected:
                raise LedgerError(
                    Code.LEG_PERSISTENCE_MISMATCH,
                    f'账本 {book_id} 的现有记账腿与历史快照冲突，迁移已停止。',
                    500,
                )
            if not actual and expected:
                store.replace_leg_structure(conn, book_id, proof['legs'])
            store.clear_legacy_placements(conn, book_id)

            # Re-read through the normal path after migration. This proves that
            # the new authority works without consulting ledger_placements.
            restored_state = loader.read_state(conn, book_id)
            restored_facts = loader.raw_transactions(conn, restored_state['members'])
            restored_proof = validate(restored_state, restored_facts, book_id)
            _assert_persisted_leg_structure(conn, book_id, restored_proof['legs'])
            total_legs += len(restored_proof['legs'])
        conn.commit()
        return {
            'books_migrated': len(book_ids),
            'legs_written': total_legs,
            'idempotent': True,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_tree(book_id: str, focus_node_id: str | None = None) -> dict:
    conn = db.get_conn()
    try:
        result = _build(conn, book_id)
        if focus_node_id and focus_node_id not in {n['id'] for n in result['nodes']}:
            raise LedgerError(Code.NODE_NOT_FOUND, '未找到要聚焦的节点。', 404)
        return result
    finally:
        conn.close()


def get_status(book_id: str) -> dict:
    tree = get_tree(book_id)
    return {'book_id': book_id, 'total_tx': tree['count'], 'placed_tx': tree['count'],
            'invariants_ok': tree['invariants_ok'], 'invariants': tree['invariants'],
            'can_undo': tree['can_undo'], 'can_redo': tree['can_redo'], 'current_seq': tree['seq'],
            'leg_count': len(tree.get('legs', [])), 'warnings': tree.get('warnings', []), 'audit_ok': tree.get('audit_ok', False),
            'split_count': tree.get('split_count', 0), 'audit': tree.get('audit', {})}


def _apply_ops_in_transaction(conn, book_id: str, operations: list[dict],
                              expected_seq: int | None, label: str) -> int:
    """在调用方事务中把多次操作保存为一个修订。

    分类工作流用它实现“一批建议要么全成功、要么全回滚”。调用方负责
    BEGIN/COMMIT/ROLLBACK；返回保存后的 seq。它不是 HTTP/MCP 入口。
    """
    if not operations:
        return int(loader.read_book(conn, book_id)['current_seq'])
    book = loader.read_book(conn, book_id)
    current_seq = int(book['current_seq'])
    if expected_seq is not None and current_seq != expected_seq:
        raise LedgerError(Code.STALE_REVISION, '账本已在另一窗口更新，请刷新后重试。', 409)
    state = loader.read_state(conn, book_id)
    facts = loader.raw_transactions(conn, state['members'])
    current_proof = validate(state, facts, book_id)
    _assert_persisted_leg_structure(conn, book_id, current_proof['legs'])
    for operation in operations:
        tx_id = operation.get('tx_id')
        exists = bool(conn.execute(
            'SELECT 1 FROM transactions_raw WHERE id=?', (tx_id,)
        ).fetchone()) if tx_id else False
        state = ops.apply(state, operation, book_id, exists, facts)
        if operation['op'] in {'attach_tx', 'detach_tx'}:
            facts = loader.raw_transactions(conn, state['members'])
    facts = loader.raw_transactions(conn, state['members'])
    state = ops.reconcile_special_splits(state, facts)
    proof = validate(state, facts, book_id)
    store.save_revision(conn, book_id, current_seq, label, state, proof['legs'])
    return current_seq + 1


def apply_ops_batch(book_id: str, operations: list[dict], expected_seq: int | None = None,
                    label: str = '批量账本操作') -> dict:
    """公开的原子批量入口；未来内置 AI API 与 MCP 分类服务可共同复用。"""
    conn = db.get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        _apply_ops_in_transaction(conn, book_id, operations, expected_seq, label)
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
    return get_tree(book_id)


def apply_op(book_id: str, op: dict, expected_seq: int | None = None) -> dict:
    return apply_ops_batch(book_id, [op], expected_seq, op['op'])


def _move_cursor(book_id: str, delta: int, expected_seq: int | None = None) -> dict:
    conn = db.get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        b = loader.read_book(conn, book_id)
        if expected_seq is not None and b['current_seq'] != expected_seq:
            raise LedgerError(Code.STALE_REVISION, '账本已在另一窗口更新，请刷新后重试。', 409)
        current_state = loader.read_state(conn, book_id)
        current_facts = loader.raw_transactions(conn, current_state['members'])
        current_proof = validate(current_state, current_facts, book_id)
        _assert_persisted_leg_structure(conn, book_id, current_proof['legs'])
        target = b['current_seq'] + delta
        row = conn.execute('SELECT state_json FROM ledger_history WHERE book_id=? AND seq=?', (book_id, target)).fetchone()
        if not row:
            raise LedgerError(Code.HISTORY_LIMIT, '没有更多可以撤销或重做的操作。')
        state = json.loads(row['state_json'])
        proof = validate(state, loader.raw_transactions(conn, state['members']), book_id)
        store.restore_revision(conn, book_id, target, state, proof['legs'])
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
    return get_tree(book_id)


def undo(book_id: str, expected_seq: int | None = None) -> dict:
    return _move_cursor(book_id, -1, expected_seq)


def redo(book_id: str, expected_seq: int | None = None) -> dict:
    return _move_cursor(book_id, 1, expected_seq)


def reset_to_m0(book_id: str, expected_seq: int | None = None) -> dict:
    conn = db.get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        b = loader.read_book(conn, book_id)
        if expected_seq is not None and b['current_seq'] != expected_seq:
            raise LedgerError(Code.STALE_REVISION, '账本已在另一窗口更新，请刷新后重试。', 409)
        old = loader.read_state(conn, book_id)
        old_facts = loader.raw_transactions(conn, old['members'])
        old_proof = validate(old, old_facts, book_id)
        _assert_persisted_leg_structure(conn, book_id, old_proof['legs'])
        root = f'{book_id}:root'
        state = {'members': old['members'], 'placements': {str(n): root for n in old['members']}, 'splits': {},
                 'nodes': [{'id': root, 'parent_id': None, 'name': '全部账目', 'op': 'ADD', 'kind': 'root'}]}
        proof = validate(state, loader.raw_transactions(conn, state['members']), book_id)
        store.save_revision(conn, book_id, b['current_seq'], '回到 M₀', state, proof['legs'])
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
    return get_tree(book_id)
