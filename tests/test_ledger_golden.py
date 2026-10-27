"""
tests/test_ledger_golden.py —— 黄金基线（合成数据，进 CI）

**这个文件是「项目不会跑偏」的终极标尺。**

规矩：
    fixture 里 expected 的每一个数字，都必须能在同一处的 notes 里被逐项加出来。
    谁改了引擎，改了引擎却没改 fixture（或改了 fixture 却没手算），
    这里立刻红。

⚠️ fixture 是**虚构数据** —— 不含任何真实账单信息。
   真实数据的差分验证在 scripts/ledger_golden_real.py（不进 CI）。
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import db as db_mod                # noqa: E402
from backend.ledger import invariants, service  # noqa: E402
from backend.ledger.amount import Y            # noqa: E402
from backend.ledger.atoms import resolve_direction  # noqa: E402
from backend.ledger.errors import Code, LedgerError  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ledger_golden.json"


@pytest.fixture(scope="module")
def golden():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture()
def env(tmp_path, monkeypatch, golden):
    """临时库 + fixture 里的合成交易。"""
    path = tmp_path / "golden.db"
    db_mod.init_db(path)
    monkeypatch.setattr(db_mod, "DB_PATH", path)
    conn = db_mod.get_conn(path)
    try:
        conn.execute(
            "INSERT INTO platforms (id, name, kind, adapter) "
            "VALUES ('test', '测试平台', 'wallet', 'generic_csv')"
        )
        for tx in golden["txs"]:
            conn.execute(
                "INSERT INTO transactions_raw "
                "(source, platform_id, tx_time, direction, amount, pay_method, "
                " counterparty, item, tx_type, status, tx_no, merchant_no, raw_json, "
                " date, month, year, weekday, hour, dedupe_key) "
                "VALUES ('测试平台', 'test', ?, ?, ?, '支付宝', '测试对手方', ?, ?, "
                "        '交易成功', ?, ?, '{}', '2026-10-05', '2026-10', '2026', 0, 12, ?)",
                (f"2026-10-05 12:00:{tx['id']:02d}", tx["direction"],
                 float(tx["amount"]), tx["item"], tx["tx_type"],
                 f"no{tx['id']}", f"m{tx['id']}", f"golden-{tx['id']}"),
            )
            conn.execute(
                "INSERT INTO tx_overrides (tx_id, funding) VALUES (?, ?)",
                (tx["id"], tx["funding"]),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _resolve(tree, ref: str) -> str:
    """把 '@root' / '@生活' 解析成真实 node_id。"""
    if ref == "@root":
        return tree["root_id"]
    name = ref[1:]
    for node in tree["nodes"]:
        if node["name"] == name:
            return node["node_id"]
    raise AssertionError(f"fixture 里引用了不存在的节点 {ref}")


# ──────────────────────────────────────────────────────────────
#  1. 每笔交易的方向判定
# ──────────────────────────────────────────────────────────────

def test_each_tx_direction_matches_fixture(env, golden):
    """方向判定表 —— fixture 写死的期望必须和引擎一致。"""
    from backend.ledger.atoms import TxRow
    for tx in golden["txs"]:
        row = TxRow(
            tx_id=tx["id"], date="2026-10-05", tx_time="2026-10-05 12:00:00",
            raw_direction=tx["direction"], amount=Decimal(tx["amount"]),
            item=tx["item"], tx_type=tx["tx_type"], counterparty="",
            pay_method="支付宝", merchant_no="", tx_no="", funding=tx["funding"],
            category="", is_exempt=False,
        )
        direction, rule = resolve_direction(row)
        assert rule == tx["expect_rule"], \
            f"τ{tx['id']} 规则应是 {tx['expect_rule']}，实际 {rule}"
        e = Decimal(tx["expect_e"])
        i = Decimal(tx["expect_i"])
        if direction == "expense":
            assert e == Decimal(tx["amount"]) and i == 0
        elif direction == "income":
            assert i == Decimal(tx["amount"]) and e == 0
        else:
            assert e == 0 and i == 0


# ──────────────────────────────────────────────────────────────
#  2. 逐步执行黄金步骤
# ──────────────────────────────────────────────────────────────

def test_golden_steps(env, golden):
    """按 fixture 的步骤走一遍，每一步的根值都必须对得上。"""
    book = service.create_book("黄金基线", "2026-10-05", "2026-10-05")
    bid = book["book_id"]
    tree = book

    for step in golden["steps"]:
        expect = step["expect"]
        op = step["op"]
        if op is not None:
            payload = dict(op)
            if "parent_id" in payload:
                payload["parent_id"] = _resolve(tree, payload["parent_id"])
            if "to_node_id" in payload:
                payload["to_node_id"] = _resolve(tree, payload["to_node_id"])
            if "node_id" in payload:
                payload["node_id"] = _resolve(tree, payload["node_id"])
            if expect.get("rejected"):
                with pytest.raises(LedgerError) as exc:
                    service.apply_op(bid, payload)
                assert exc.value.code == getattr(Code, expect["error_code"]), \
                    f"步骤「{step['name']}」的错误码不对：{exc.value.code}"
                tree = service.get_tree(bid)
            else:
                tree = service.apply_op(bid, payload)

        if expect.get("rejected"):
            continue

        assert tree["root_e"] == pytest.approx(float(expect["root_e"])), \
            f"步骤「{step['name']}」根支出不对：期望 {expect['root_e']}，实际 {tree['root_e']}"
        assert tree["root_i"] == pytest.approx(float(expect["root_i"])), \
            f"步骤「{step['name']}」根收入不对：期望 {expect['root_i']}，实际 {tree['root_i']}"
        assert tree["root_net"] == pytest.approx(float(expect["root_net"])), \
            f"步骤「{step['name']}」根净值不对：期望 {expect['root_net']}，实际 {tree['root_net']}"
        assert len(tree["nodes"]) == expect["node_count"], \
            f"步骤「{step['name']}」节点数不对：期望 {expect['node_count']}，实际 {len(tree['nodes'])}"
        assert len(tree["legs"]) == expect["leg_count"], \
            f"步骤「{step['name']}」腿数不对：期望 {expect['leg_count']}，实际 {len(tree['legs'])}"

        # 节点自身的 op（ADD / NET）也要钉住。
        # 为什么单列：节点值只靠 e/i 判定的话，「切了 op 但数字碰巧没变」会漏过去
        # —— 纯支出的组 ADD 和 NET 数值完全一样（见 fixture 第 3 步的 notes）。
        for ref, want_op in (expect.get("node_ops") or {}).items():
            nid = _resolve(tree, ref)
            node = next((n for n in tree["nodes"] if n["node_id"] == nid), None)
            assert node is not None, f"步骤「{step['name']}」里找不到节点 {ref}"
            assert node["op"] == want_op, \
                f"步骤「{step['name']}」节点 {ref} 的 op 不对：期望 {want_op}，实际 {node['op']}"


def test_golden_invariants_stay_green(env, golden):
    """走完全部步骤后，不变量必须仍然全绿。"""
    book = service.create_book("黄金基线2", "2026-10-05", "2026-10-05")
    bid = book["book_id"]
    tree = book
    for step in golden["steps"]:
        op = step["op"]
        if op is None:
            continue
        payload = dict(op)
        if "parent_id" in payload:
            payload["parent_id"] = _resolve(tree, payload["parent_id"])
        if "to_node_id" in payload:
            payload["to_node_id"] = _resolve(tree, payload["to_node_id"])
        if "node_id" in payload:
            payload["node_id"] = _resolve(tree, payload["node_id"])
        try:
            tree = service.apply_op(bid, payload)
        except LedgerError:
            continue
        for check in invariants.check_all(
            service.load_ledger(db_mod.get_conn(), bid)
        ):
            assert check.ok, f"步骤「{step['name']}」后 {check.name} 没过：{check.message}"


# ──────────────────────────────────────────────────────────────
#  3. 审计链（手算）
# ──────────────────────────────────────────────────────────────

def test_audit_chain_matches_hand_computed(env, golden):
    """审计链的每一项都要和 fixture 里手算的数一致。"""
    spec = golden["audit_chain_m0"]
    book = service.create_book("黄金审计", "2026-10-05", "2026-10-05")
    bid = book["book_id"]
    conn = db_mod.get_conn()
    try:
        ledger = service.load_ledger(conn, bid)
        chain = invariants.audit_chain(ledger)
    finally:
        conn.close()

    assert chain["s1_platform"].net_value == Decimal(spec["s1_platform"]["value"]), \
        f"① 平台有符号和不对：{chain['s1_platform']}"
    assert chain["s2_reorientation"].net_value == Decimal(spec["s2_reorientation"]["value"]), \
        f"② 中性定向变化不对：{chain['s2_reorientation']}"
    assert chain["s3_mirror"].net_value == Decimal(spec["s3_mirror"]["value"]), \
        f"③ 镜像腿合计不对：{chain['s3_mirror']}"
    assert chain["s4_net_reorder"].net_value == Decimal(spec["s4_net_reorder"]["value"]), \
        f"④ NET 重排不对：{chain['s4_net_reorder']}"
    assert chain["root_net_value"] == Decimal(spec["s5_root"]["value"]), \
        f"⑤ 根的净值不对：{chain['root_net_value']}"
    assert chain["closed"] is spec["closed"], "审计链闭合判定不对"


# ──────────────────────────────────────────────────────────────
#  4. fixture 自身的自洽性
# ──────────────────────────────────────────────────────────────

def test_fixture_notes_contain_the_arithmetic(env, golden):
    """fixture 的每一步都必须有 notes —— 没有 notes 的数字不许信。"""
    for step in golden["steps"]:
        assert step.get("notes"), f"步骤「{step['name']}」没有 notes"
        assert step.get("name")
    for tx in golden["txs"]:
        assert tx.get("notes"), f"τ{tx['id']} 没有 notes"
    assert golden["audit_chain_m0"].get("notes")


def test_fixture_root_expectation_is_self_consistent(golden):
    """fixture 里的 M₀ 期望必须能被 tx 的期望加起来 —— 防止改了 tx 忘了改 M₀。"""
    e = sum(Decimal(tx["expect_e"]) for tx in golden["txs"])
    i = sum(Decimal(tx["expect_i"]) for tx in golden["txs"])
    m0 = golden["steps"][0]["expect"]
    assert Decimal(m0["root_e"]) == e, f"M₀ 支出 {m0['root_e']} ≠ 各笔之和 {e}"
    assert Decimal(m0["root_i"]) == i, f"M₀ 收入 {m0['root_i']} ≠ 各笔之和 {i}"
    assert Decimal(m0["root_net"]) == i - e
    assert m0["leg_count"] == len(golden["txs"])
