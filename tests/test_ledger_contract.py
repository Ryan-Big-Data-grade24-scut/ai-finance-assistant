"""
tests/test_ledger_contract.py —— 账本引擎的契约测试

**这些测试是「确保项目不会跑偏」的机器兑现。**
每一条都对应代数里的一条不变量或一条产品决策。

跑法（仓库根，无 pytest.ini）：
    .venv/Scripts/python.exe -m pytest -q

⚠️ 全部使用**临时库 + 合成数据**，不碰真实账单库，也不碰真实数据。
"""
from __future__ import annotations

import random
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db as db_mod                                    # noqa: E402
from backend.ledger import invariants, loader, ops, service, store  # noqa: E402
from backend.ledger.atoms import (                                 # noqa: E402
    DIRECTION_EXPENSE,
    DIRECTION_INCOME,
    DIRECTION_ZERO,
    PLATFORM_EXPENSE,
    PLATFORM_INCOME,
    PLATFORM_NEUTRAL,
    TxRow,
    resolve_direction,
    split_applicable,
)
from backend.ledger.amount import Y                               # noqa: E402
from backend.ledger.errors import Code, LedgerError               # noqa: E402
from backend.ledger.tree import Book, build_m0                    # noqa: E402

# ──────────────────────────────────────────────────────────────
#  夹具：临时库 + 合成交易
# ──────────────────────────────────────────────────────────────

#: 合成交易。字段全部虚构，不含任何真实账单信息。
#: (id, direction, amount, funding, item, tx_type)
SYNTH = [
    (1, PLATFORM_EXPENSE, "100.00", "自有", "超市买菜", "餐饮"),
    (2, PLATFORM_INCOME, "500.00", "自有", "工资", "工资"),
    (3, PLATFORM_NEUTRAL, "50.00", "自有", "余额宝收益发放", "理财"),
    (4, PLATFORM_NEUTRAL, "30.00", "自有", "账户互转", "转账"),
    (5, PLATFORM_EXPENSE, "40.00", "亲情卡", "给长辈买的菜", "餐饮"),
    (6, PLATFORM_EXPENSE, "25.00", "自有", "地铁", "交通"),
    (7, PLATFORM_INCOME, "9.50", "自有", "退款", "退款"),
    (8, PLATFORM_EXPENSE, "9.50", "自有", "青桔骑行", "交通"),
    (9, PLATFORM_EXPENSE, "12.00", "他人代付", "代付商品", "购物"),
    (10, PLATFORM_NEUTRAL, "200.00", "账户搬运", "转入余额宝", "转账"),
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """一个装好合成数据的临时库。"""
    path = tmp_path / "ledger_test.db"
    db_mod.init_db(path)
    monkeypatch.setattr(db_mod, "DB_PATH", path)
    conn = db_mod.get_conn(path)
    try:
        conn.execute(
            "INSERT INTO platforms (id, name, kind, adapter) "
            "VALUES ('test', '测试平台', 'wallet', 'generic_csv')"
        )
        for tid, direction, amount, funding, item, tx_type in SYNTH:
            conn.execute(
                "INSERT INTO transactions_raw "
                "(source, platform_id, tx_time, direction, amount, pay_method, "
                " counterparty, item, tx_type, status, tx_no, merchant_no, raw_json, "
                " date, month, year, weekday, hour, dedupe_key) "
                "VALUES ('测试平台', 'test', ?, ?, ?, '支付宝', '测试对手方', ?, ?, "
                "        '交易成功', ?, ?, '{}', '2026-10-05', '2026-10', '2026', 0, 12, ?)",
                (f"2026-10-05 12:00:{tid:02d}", direction, float(amount),
                 item, tx_type, f"no{tid}", f"m{tid}", f"key-{tid}"),
            )
            conn.execute(
                "INSERT INTO tx_overrides (tx_id, funding) VALUES (?, ?)",
                (tid, funding),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def make_book(name="测试账", date_from="2026-10-01", date_to="2026-10-31"):
    return service.create_book(name, date_from, date_to)


# ──────────────────────────────────────────────────────────────
#  1. 方向映射（全枚举表）
# ──────────────────────────────────────────────────────────────

def _tx(tid, direction, amount="10.00", funding="自有", item="", tx_type="") -> TxRow:
    return TxRow(
        tx_id=tid, date="2026-10-05", tx_time="2026-10-05 12:00:00",
        raw_direction=direction, amount=Decimal(amount), item=item,
        tx_type=tx_type, counterparty="", pay_method="支付宝",
        merchant_no="", tx_no="", funding=funding, category="", is_exempt=False,
    )


DIRECTION_TABLE = [
    # (raw_direction, funding, item, tx_type, 期望方向)
    (PLATFORM_EXPENSE, "自有", "", "", DIRECTION_EXPENSE),
    (PLATFORM_INCOME, "自有", "", "", DIRECTION_INCOME),
    (PLATFORM_NEUTRAL, "自有", "", "", DIRECTION_EXPENSE),        # 中性默认记支出
    (PLATFORM_NEUTRAL, "自有", "余额宝收益发放", "", DIRECTION_INCOME),  # 唯一例外
    (PLATFORM_EXPENSE, "账户搬运", "", "", DIRECTION_ZERO),       # 搬运归零
    (PLATFORM_INCOME, "账户搬运", "", "", DIRECTION_ZERO),
    (PLATFORM_INCOME, "自有", "", "退款", DIRECTION_ZERO),        # 退款归零
    (PLATFORM_EXPENSE, "自有", "订单退款", "", DIRECTION_ZERO),
]


@pytest.mark.parametrize("raw,funding,item,tx_type,expect", DIRECTION_TABLE)
def test_direction_table(raw, funding, item, tx_type, expect):
    """方向映射表 —— 全枚举，不靠抽样。"""
    tx = _tx(1, raw, funding=funding, item=item, tx_type=tx_type)
    direction, rule = resolve_direction(tx)
    assert direction == expect, f"{raw}/{funding}/{item}/{tx_type} 判成了 {direction}（{rule}）"
    assert rule, "每条判定都要有规则 id —— 人要看得见这笔为什么这么算"


def test_refund_beats_split():
    """退款优先于分裂（代数 §2.5 的顺序铁律）。"""
    tx = _tx(1, PLATFORM_EXPENSE, funding="亲情卡", item="退款")
    assert resolve_direction(tx)[0] == DIRECTION_ZERO
    assert not split_applicable(tx)


def test_split_applicable_only_external():
    """分裂只对外部额度（亲情卡/他人代付）成立。"""
    assert split_applicable(_tx(1, PLATFORM_EXPENSE, funding="亲情卡"))
    assert split_applicable(_tx(1, PLATFORM_EXPENSE, funding="他人代付"))
    assert not split_applicable(_tx(1, PLATFORM_EXPENSE, funding="自有"))
    assert not split_applicable(_tx(1, PLATFORM_EXPENSE, funding="账户搬运"))


# ──────────────────────────────────────────────────────────────
#  2. 分裂对称（I3）
# ──────────────────────────────────────────────────────────────

def test_split_symmetry(env):
    """ℓ₁ +_1 ℓ₂ = (a, a)。"""
    book = make_book()
    bid = book["book_id"]
    before = book["root_e"]
    out = service.apply_op(bid, {"op": "split", "tx_id": 5})
    ext = [n for n in out["nodes"] if n["kind"] == "external_income"]
    assert len(ext) == 1, "外部收入节点必须唯一（I4）"
    # 主腿还在原处，镜像腿进了外部收入节点
    legs = {(l["tx_id"], l["role"]): l["node_id"] for l in out["legs"]}
    assert legs[(5, "mirror")] == ext[0]["node_id"]
    assert legs[(5, "primary")] != ext[0]["node_id"]
    # 镜像腿把 40 记成了收入
    assert out["root_i"] == pytest.approx(book["root_i"] + 40.0)
    assert out["root_e"] == pytest.approx(before)


def test_split_not_applicable_is_rejected(env):
    """不涉及外部额度的交易不许分裂，且理由是人话。"""
    book = make_book()
    bid = book["book_id"]
    with pytest.raises(LedgerError) as exc:
        service.apply_op(bid, {"op": "split", "tx_id": 1})
    assert exc.value.code == Code.SPLIT_NOT_APPLICABLE
    assert "外部额度" in exc.value.message


def test_double_split_is_rejected(env):
    book = make_book()
    bid = book["book_id"]
    service.apply_op(bid, {"op": "split", "tx_id": 5})
    with pytest.raises(LedgerError) as exc:
        service.apply_op(bid, {"op": "split", "tx_id": 5})
    assert exc.value.code == Code.TX_ALREADY_SPLIT


# ──────────────────────────────────────────────────────────────
#  3. 不变量：随机操作序列，每步都全绿
# ──────────────────────────────────────────────────────────────

def test_random_op_sequence_keeps_invariants(env):
    """固定种子的随机操作序列 —— 每一步之后不变量必须全绿。

    这是「前端乱造不会算出错账」的机器兑现。
    """
    book = make_book()
    bid = book["book_id"]
    rng = random.Random(20261026)
    conn = db_mod.get_conn()
    try:
        ledger = service.load_ledger(conn, bid)
        for step in range(60):
            node_ids = list(ledger.tree.nodes)
            tx_ids = list(ledger.txs)
            choice = rng.choice(["create", "setop", "reassign", "split", "unsplit", "dissolve"])
            try:
                if choice == "create" and len(node_ids) < 12:
                    parent = rng.choice(node_ids)
                    res = ops.create_node(
                        ledger, parent, f"组{step}",
                        op=rng.choice(["ADD", "NET"]),
                        tx_ids=rng.sample(tx_ids, min(2, len(tx_ids))),
                    )
                elif choice == "setop":
                    res = ops.set_op(ledger, rng.choice(node_ids), rng.choice(["ADD", "NET"]))
                elif choice == "reassign":
                    res = ops.reassign(ledger, rng.choice(tx_ids), rng.choice(node_ids))
                elif choice == "split":
                    res = ops.split(ledger, rng.choice(tx_ids))
                elif choice == "unsplit":
                    res = ops.unsplit(ledger, rng.choice(tx_ids))
                else:
                    cand = [n for n in node_ids
                            if n != ledger.tree.root_id
                            and ledger.tree.nodes[n].kind != "external_income"]
                    if not cand:
                        continue
                    res = ops.dissolve_node(ledger, rng.choice(cand))
            except LedgerError:
                continue          # 非法组合被拒 —— 这本身就是正确行为
            for check in invariants.check_all(res.ledger):
                assert check.ok, f"第 {step} 步 {choice} 之后 {check.name} 没过：{check.message}"
            ledger = res.ledger
    finally:
        conn.close()


def test_corrupt_tree_is_detected(env):
    """人为造坏树 → 必须被抓到（护栏第 4 道锁）。"""
    book = make_book()
    bid = book["book_id"]
    conn = db_mod.get_conn()
    try:
        ledger = service.load_ledger(conn, bid)
        # 把一笔交易的主腿指向一个不存在的节点
        ledger.tree.legs[(1, "primary")] = "bk_9999:nope"
        bad = [c for c in invariants.check_all(ledger) if not c.ok]
        assert bad, "坏树必须被抓到"
        assert bad[0].name in ("I1", "I2")
        with pytest.raises(LedgerError) as exc:
            invariants.assert_all(ledger)
        assert exc.value.code == Code.INVARIANT_BROKEN
    finally:
        conn.close()


def test_mirror_leg_cannot_wander(env):
    """I4：镜像腿只能待在外部收入节点。"""
    book = make_book()
    bid = book["book_id"]
    service.apply_op(bid, {"op": "split", "tx_id": 5})
    conn = db_mod.get_conn()
    try:
        ledger = service.load_ledger(conn, bid)
        ext = ledger.tree.external_income_node()
        other = [n for n in ledger.tree.nodes if n != ext][0]
        ledger.tree.legs[(5, "mirror")] = other
        bad = [c for c in invariants.check_all(ledger) if not c.ok]
        assert any(c.name == "I4" for c in bad), "I4 必须抓到镜像腿乱跑"
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────
#  4. 作用域
# ──────────────────────────────────────────────────────────────

def test_cross_book_is_rejected(env):
    """跨账操作拒绝（账本正交，不许互相污染）。"""
    b1 = make_book("账一")
    b2 = make_book("账二", "2026-10-01", "2026-10-02")
    node2 = [n["node_id"] for n in b2["nodes"]][0]
    with pytest.raises(LedgerError) as exc:
        service.apply_op(b1["book_id"], {"op": "reassign", "tx_id": 1, "to_node_id": node2})
    assert exc.value.code in (Code.CROSS_BOOK, Code.SAME_PARENT_REQUIRED)


def test_two_books_are_independent(env):
    """两本账互不影响。"""
    b1 = make_book("账一")
    b2 = make_book("账二")
    before2 = b2["root_e"]
    service.apply_op(b1["book_id"], {
        "op": "create_node", "parent_id": b1["root_id"],
        "name": "新组", "op_value": "ADD", "tx_ids": [1, 2],
    })
    after2 = service.get_tree(b2["book_id"])["root_e"]
    assert after2 == before2


def test_cross_parent_move_is_rejected(env):
    """交易不在目标父节点下 ⇒ 拒绝（引擎不替前端猜）。"""
    book = make_book()
    bid = book["book_id"]
    out = service.apply_op(bid, {
        "op": "create_node", "parent_id": book["root_id"],
        "name": "A", "op_value": "ADD", "tx_ids": [1],
    })
    node_a = [n["node_id"] for n in out["nodes"] if n["name"] == "A"][0]
    with pytest.raises(LedgerError) as exc:
        service.apply_op(bid, {
            "op": "create_node", "parent_id": node_a,
            "name": "B", "op_value": "ADD", "tx_ids": [2],   # 2 不在 A 下
        })
    assert exc.value.code == Code.SAME_PARENT_REQUIRED


# ──────────────────────────────────────────────────────────────
#  5. 撤销 / 重做 / 回到 M₀
# ──────────────────────────────────────────────────────────────

def _shape(payload):
    return (
        sorted((n["node_id"], n["parent_id"], n["name"], n["op"]) for n in payload["nodes"]),
        sorted((l["tx_id"], l["role"], l["node_id"]) for l in payload["legs"]),
        payload["root_e"], payload["root_i"],
    )


def test_undo_redo_roundtrip(env):
    """撤销再重做，逐字段还原。"""
    book = make_book()
    bid = book["book_id"]
    out = service.apply_op(bid, {
        "op": "create_node", "parent_id": book["root_id"],
        "name": "餐饮", "op_value": "ADD", "tx_ids": [1, 6],
    })
    after = _shape(out)
    service.undo(bid)
    back = _shape(service.get_tree(bid))
    assert back != after
    service.redo(bid)
    assert _shape(service.get_tree(bid)) == after


def test_undo_restores_root_value_after_split(env):
    """分裂后撤销，根值必须还原（数值不守恒 ⇒ 靠日志还原）。"""
    book = make_book()
    bid = book["book_id"]
    before = (book["root_e"], book["root_i"])
    service.apply_op(bid, {"op": "split", "tx_id": 5})
    assert service.get_tree(bid)["root_i"] != before[1]
    service.undo(bid)
    after = service.get_tree(bid)
    assert (after["root_e"], after["root_i"]) == before


def test_undo_to_m0_and_reset(env):
    """一路撤销回到 M₀，再 reset 也回到 M₀。"""
    book = make_book()
    bid = book["book_id"]
    m0 = _shape(book)
    service.apply_op(bid, {
        "op": "create_node", "parent_id": book["root_id"],
        "name": "餐饮", "op_value": "ADD", "tx_ids": [1],
    })
    service.apply_op(bid, {"op": "set_op", "node_id": book["root_id"], "op_value": "NET"})
    service.undo(bid)
    service.undo(bid)
    assert _shape(service.get_tree(bid)) == m0
    service.apply_op(bid, {
        "op": "create_node", "parent_id": book["root_id"],
        "name": "别的", "op_value": "ADD", "tx_ids": [2],
    })
    reset = service.reset_to_m0(bid)
    assert _shape(reset) == m0


def test_undo_nothing_to_undo(env):
    book = make_book()
    with pytest.raises(LedgerError) as exc:
        service.undo(book["book_id"])
    assert exc.value.code == Code.NOTHING_TO_UNDO


# ──────────────────────────────────────────────────────────────
#  6. 数值：Decimal 不漂移 + 审计链闭合
# ──────────────────────────────────────────────────────────────

def test_no_float_drift(env):
    """0.1 + 0.2 必须是 0.30 —— 引擎内部用 Decimal。"""
    conn = db_mod.get_conn()
    try:
        ledger = service.load_ledger(conn, make_book()["book_id"])
        total = sum((ledger.leg_value(t, "primary") for t in ledger.txs), Y())
        assert total.e == total.e.quantize(Decimal("0.01"))
        assert total.i == total.i.quantize(Decimal("0.01"))
    finally:
        conn.close()


def test_audit_chain_closes(env):
    """审计链必须闭合：不闭合 = 实现缺陷，不是口径分歧。"""
    book = make_book()
    bid = book["book_id"]
    service.apply_op(bid, {"op": "split", "tx_id": 5})
    service.apply_op(bid, {"op": "split", "tx_id": 9})
    conn = db_mod.get_conn()
    try:
        ledger = service.load_ledger(conn, bid)
        chain = invariants.audit_chain(ledger)
        assert chain["closed"], "审计链没闭合"
        for check in invariants.check_all(ledger):
            assert check.ok, f"{check.name}: {check.message}"
    finally:
        conn.close()


def test_net_node_preserves_net_value(env):
    """NET 重排不改变净值（代数 ④ ≡ 0 的依据）。"""
    book = make_book()
    bid = book["book_id"]
    before = book["root_net"]
    out = service.apply_op(bid, {"op": "set_op", "node_id": book["root_id"], "op_value": "NET"})
    assert out["root_net"] == pytest.approx(before)


# ──────────────────────────────────────────────────────────────
#  7. M₀ 与幂等
# ──────────────────────────────────────────────────────────────

def test_m0_is_flat_platform_view(env):
    """M₀ = 根 + 全部腿的一层平铺树。"""
    book = make_book()
    assert len(book["nodes"]) == 1
    assert book["root_id"] == book["nodes"][0]["node_id"]
    assert book["nodes"][0]["op"] == "ADD"
    assert len(book["legs"]) == len(SYNTH)


def test_idempotent_create_and_dissolve(env):
    """建了又解散 ⇒ 回到 M₀ 的形状（幂等）。"""
    book = make_book()
    bid = book["book_id"]
    out = service.apply_op(bid, {
        "op": "create_node", "parent_id": book["root_id"],
        "name": "临时", "op_value": "ADD", "tx_ids": [1, 2],
    })
    nid = [n["node_id"] for n in out["nodes"] if n["name"] == "临时"][0]
    out2 = service.apply_op(bid, {"op": "dissolve_node", "node_id": nid})
    assert _shape(out2) == _shape(book)


def test_dissolve_root_is_rejected(env):
    book = make_book()
    with pytest.raises(LedgerError) as exc:
        service.apply_op(book["book_id"], {"op": "dissolve_node", "node_id": book["root_id"]})
    assert exc.value.code == Code.INVALID_OP


# ──────────────────────────────────────────────────────────────
#  8. 账本模型
# ──────────────────────────────────────────────────────────────

def test_book_members_are_frozen(env):
    """建账时按区间冻结成员；之后只能显式 attach/detach。"""
    book = make_book("区间账", "2026-10-01", "2026-10-31")
    bid = book["book_id"]
    assert book["book"]["member_count"] == len(SYNTH)   # 合成数据都在同一天
    # 区间外的交易不进账
    conn = db_mod.get_conn()
    try:
        conn.execute(
            "INSERT INTO transactions_raw "
            "(source, platform_id, tx_time, direction, amount, item, tx_type, status, "
            " tx_no, merchant_no, raw_json, date, month, year, weekday, hour, dedupe_key) "
            "VALUES ('测试平台', 'test', '2026-11-01 10:00:00', '支出', 5, 'x', 'x', "
            "        '交易成功', 'noX', 'mX', '{}', '2026-11-01', '2026-11', '2026', 0, 10, 'key-x')"
        )
        conn.commit()
    finally:
        conn.close()
    assert service.get_status(bid)["total_tx"] == len(SYNTH)
    # 显式 attach 后才进来
    conn = db_mod.get_conn()
    try:
        new_id = conn.execute(
            "SELECT id FROM transactions_raw WHERE dedupe_key = 'key-x'").fetchone()["id"]
        conn.commit()
    finally:
        conn.close()
    out = service.apply_op(bid, {"op": "attach_tx", "tx_id": new_id})
    assert len(out["legs"]) == len(SYNTH) + 1
    out2 = service.apply_op(bid, {"op": "detach_tx", "tx_id": new_id})
    assert len(out2["legs"]) == len(SYNTH)


def test_invalid_date_range_is_rejected(env):
    with pytest.raises(LedgerError) as exc:
        service.create_book("坏区间", "2026-10-31", "2026-10-01")
    assert exc.value.code == Code.INVALID_DATE_RANGE
