"""事实适配器：真实流水 → V2 的 Tx。V2 唯一读业务表的地方。

方向口径复用 V1 的 `backend.ledger.atoms.effective_atom_value`（中性默认按支出）。
**这里不做退款配对 / 账户搬运归并**：那属于账本语义，由 +2 净额组表达（ν 抵消），
事实层不按另一笔交易改写本笔金额 —— 尤其不能因为一笔**还没初始化**的交易把可见交易归零。

分类来源：`tx_overrides.manual_category` → `transactions_raw.tx_type` → '其他'。
**不能用 `v_tx.category`** —— 它优先取 V1 账本树的叶子名，V2 若依赖它，V1 动一下 V2 的树就跟着变。

初始化：账本只读「已初始化」的交易（`ledger2_tx` 有 role='primary' 且 initialized=1 的行）。
未初始化的交易在所有读出里直接被过滤，也不参与任何计算。
"""
from __future__ import annotations

import json

from backend.ledger import atoms
from backend.ledger.amount import cents

from .errors import LedgerError
from .model import Scope, Tx

CAT_PREFIX = 'cat:'
DEFAULT_CATEGORY = '其他'

_CATEGORY_EXPR = "COALESCE(NULLIF(TRIM(o.manual_category), ''), NULLIF(TRIM(t.tx_type), ''), '其他')"
_LIVE = 'NOT EXISTS (SELECT 1 FROM tx_deletions d WHERE d.tx_id = t.id)'
_FROM = 'FROM transactions_raw t LEFT JOIN tx_overrides o ON o.tx_id = t.id'
_INIT = ("EXISTS (SELECT 1 FROM ledger2_tx m WHERE m.tx_id = t.id "
         "AND m.role = 'primary' AND m.initialized = 1)")


def category_node_id(category: str) -> str:
    return CAT_PREFIX + category


def is_category_node(node_id: str) -> bool:
    return node_id.startswith(CAT_PREFIX)


def category_name(node_id: str) -> str:
    return node_id[len(CAT_PREFIX):]


def category_of(conn, tx_id: int) -> str:
    """一笔存活交易的分类；不存在或已被删除 → TX_NOT_FOUND。"""
    row = conn.execute(
        f'SELECT {_CATEGORY_EXPR} AS c {_FROM} WHERE t.id = ? AND {_LIVE}', (int(tx_id),)
    ).fetchone()
    if row is None:
        raise LedgerError('TX_NOT_FOUND', f'交易 {tx_id} 不存在，或已被删除。')
    return row['c']


def category_of_any(conn, tx_id: int) -> str:
    """分类，**不看是否已删除**（只在搬运已有归属行时用：死交易的行要跟着组/节点走）；根本不存在 → TX_NOT_FOUND。"""
    row = conn.execute(f'SELECT {_CATEGORY_EXPR} AS c {_FROM} WHERE t.id = ?', (int(tx_id),)).fetchone()
    if row is None:
        raise LedgerError('TX_NOT_FOUND', f'交易 {tx_id} 不存在。')
    return row['c']


def category_exists(conn, category: str) -> bool:
    row = conn.execute(
        f'SELECT 1 {_FROM} WHERE {_CATEGORY_EXPR} = ? AND {_LIVE} LIMIT 1', (category,)
    ).fetchone()
    return row is not None


def live_ids(conn) -> set[int]:
    return {int(r['id']) for r in conn.execute(f'SELECT t.id {_FROM} WHERE {_LIVE}')}


def _fact_rows(conn) -> list[dict]:
    rows = conn.execute(
        'SELECT t.id, t.tx_time, t.date, t.direction, t.amount, t.source, t.counterparty, t.item, '
        't.pay_method, t.tx_type, t.status, t.tx_no, t.merchant_no, t.remark, t.raw_json, '
        f"COALESCE(o.funding, '自有') AS funding, {_CATEGORY_EXPR} AS category, "
        'm.title AS m_title, m.note AS m_note, m.category_path AS m_path, '
        '(m.tx_id IS NOT NULL AND m.initialized = 1) AS m_init '
        f"{_FROM} LEFT JOIN ledger2_tx m ON m.tx_id = t.id AND m.role = 'primary' "
        f'WHERE {_LIVE} ORDER BY t.tx_time, t.id'
    ).fetchall()
    return [dict(r) for r in rows]


def load_txs(conn) -> dict[int, Tx]:
    """已初始化的存活交易（primary 角色）→ {id: Tx}。金额是整数分，方向是 ±1。

    每一笔交易都按自己的事实换算，**不配对、不归并**：退款想做净额就建成 +2 组，
    由 ν（成员 ∩ scope）在引擎里抵消。事实层不认识「退款」「搬运」这些语义。
    """
    out: dict[int, Tx] = {}
    for f in _fact_rows(conn):
        if not f['m_init'] or not _positive(f):
            continue
        e, i = atoms.effective_atom_value(f)
        direction, amount = (1, i) if i > 0 else (-1, e)
        tx_id = int(f['id'])
        out[tx_id] = Tx(
            id=tx_id, role='primary', amount=amount, direction=direction,
            tx_time=f['tx_time'], date=f['date'], category=f['category'],
            counterparty=f['counterparty'], item=f['item'], source=f['source'], note=None,
            title=f['m_title'], memo=f['m_note'], category_path=json.loads(f['m_path'] or '[]'),
        )
    return out


def _positive(row: dict) -> bool:
    try:
        return cents(row['amount']) > 0
    except Exception:
        return False


def date_bounds(conn) -> dict:
    """存活交易的日期边界 {first, last}（没有交易时都是 None）。读出口径用，不落表。"""
    row = conn.execute(f'SELECT MIN(t.date) AS a, MAX(t.date) AS b {_FROM} WHERE {_LIVE}').fetchone()
    return {'first': row['a'], 'last': row['b']}


def scope_by_date(conn, date_from: str, date_to: str) -> Scope:
    """时间范围 → 存活交易 id 集合。范围是读时参数，不落表。"""
    rows = conn.execute(
        f'SELECT t.id {_FROM} WHERE t.date BETWEEN ? AND ? AND {_LIVE} AND {_INIT}', (date_from, date_to)
    ).fetchall()
    return Scope(frozenset(int(r['id']) for r in rows))


def is_initialized(conn, tx_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM ledger2_tx WHERE tx_id = ? AND role = 'primary' AND initialized = 1",
        (int(tx_id),)
    ).fetchone() is not None


def uninitialized_ids(conn) -> list[int]:
    """存活但还没初始化的交易 id（按时间排）。"""
    rows = conn.execute(
        f'SELECT t.id {_FROM} WHERE {_LIVE} AND NOT {_INIT} ORDER BY t.tx_time, t.id'
    ).fetchall()
    return [int(r['id']) for r in rows]


def immutable_fields(conn, tx_ids) -> dict[int, dict]:
    """平台预设要抄的交易不可变字段：{id: {category, title, note}}。

    category = 平台分类（人工覆盖 → tx_type → 其他）；title = 商品，没有则交易对方；note = 平台备注。
    存活的交易才返回；缺的由调用方报 TX_NOT_FOUND。
    """
    out: dict[int, dict] = {}
    for tx_id in tx_ids:
        row = conn.execute(
            f'SELECT {_CATEGORY_EXPR} AS c, t.item, t.counterparty, t.remark {_FROM} '
            f'WHERE t.id = ? AND {_LIVE}', (int(tx_id),)
        ).fetchone()
        if row is not None:
            out[int(tx_id)] = {
                'category': row['c'],
                'title': (row['item'] or row['counterparty'] or '').strip(),
                'note': (row['remark'] or '').strip(),
            }
    return out


def scope_of_ids(tx_ids) -> Scope:
    return Scope(frozenset(int(t) for t in tx_ids))
