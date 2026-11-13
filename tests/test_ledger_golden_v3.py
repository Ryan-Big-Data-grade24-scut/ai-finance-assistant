"""
tests/test_ledger_golden_v3.py —— V3 账本引擎黄金基线（合成数据，进 CI）

**这个文件是「项目不会跑偏」的终极标尺（V3 重写版）。**

规矩：
    fixture 里 expected 的每一个数字，都必须能在同一处的 notes 里被逐项加出来。
    谁改了引擎，改了引擎却没改 fixture（或改了 fixture 却没手算），
    这里立刻红。

与 A 树（main 分支的 tests/test_ledger_golden.py）的差别：
    * fixture 的剧本是「可执行的操作脚本」，每一步都带 op 与 expected；
    * notes 里的每一处加法都会被本文件**逐条重算**（不是手写看着像）；
    * 每个节点的 ADD/NET 被 node_ops 钉住 —— 纯支出组 ADD 与 NET 数值完全一样，
      只看 e/i 会漏掉「op 没切上」；
    * 审计链（raw → 中性定向 → 退款 → 转账 → 分裂 → 根净值）逐段手算并断言闭合；
    * 撤销/重做是整份状态快照回放（ledger_history.state_json），
      这里做**全量**比对：每一步的 root_e/root_i/root_net/node_count/leg_count
      都要和当时的 expected 逐字对上。

⚠️ fixture 是**虚构数据** —— 不含任何真实账单信息。
   真实数据的差分验证在 scripts/ledger_golden_real.py（不进 CI）。
"""
from __future__ import annotations

import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db as db_mod                          # noqa: E402
from backend.ledger import invariants, loader, service    # noqa: E402
from backend.ledger.atoms import (                        # noqa: E402
    analyze_special_transactions,
    effective_atom_value,
)
from backend.ledger.amount import cents                    # noqa: E402
from backend.ledger.errors import LedgerError             # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ledger_golden_v3.json"

ROOT_NODE_NAME = "全部账目"

PLATFORM_ID = "demo-v3"
PLATFORM_NAME = "模拟演示平台"
BOOK_NAME = "黄金基线V3"
BOOK_DATE_FROM = "2026-10-05"
BOOK_DATE_TO = "2026-10-05"


# ──────────────────────────────────────────────────────────────────────────
#  fixture 读入与临时库
# ──────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, golden: dict) -> Path:
    """临时库 + fixture 里的合成交易。

    ⚠️ 绝不碰 db/bills.db：DB_PATH 被 monkeypatch 到 tmp_path。
    """
    path = tmp_path / "golden_v3.db"
    monkeypatch.setattr(db_mod, "DB_PATH", path)
    db_mod.init_db(path)
    conn = db_mod.get_conn(path)
    try:
        conn.execute(
            "INSERT INTO platforms (id, name, kind, adapter) VALUES (?, ?, ?, ?)",
            (PLATFORM_ID, PLATFORM_NAME, "wallet", "generic_csv"),
        )
        for tx in golden["txs"]:
            tx_date = "2026-10-06" if tx["id"] == 14 else "2026-10-05"
            conn.execute(
                "INSERT INTO transactions_raw "
                "(id, source, platform_id, tx_time, direction, amount, pay_method, "
                " counterparty, item, tx_type, status, tx_no, merchant_no, remark, "
                " raw_json, date, month, year, weekday, hour, dedupe_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tx["id"], PLATFORM_NAME, PLATFORM_ID,
                    f"{tx_date} 12:00:{tx['id']:02d}",
                    tx["direction"], float(tx["amount"]), "模拟支付方式", "模拟对手方",
                    tx["item"], tx["tx_type"], tx["status"], tx["tx_no"],
                    tx["merchant_no"], "", json.dumps(tx["raw"], ensure_ascii=False),
                    tx_date, tx_date[:7], tx_date[:4], 0, 12,
                    f"golden-v3-{tx['id']:04d}",
                ),
            )
            conn.execute(
                "INSERT INTO tx_overrides "
                "(tx_id, manual_category, manual_subcat, funding, updated_by) "
                "VALUES (?, ?, ?, ?, 'test')",
                (tx["id"], "模拟账目", "模拟明细", tx["funding"]),
            )
        conn.commit()
    finally:
        conn.close()
    return path


# ──────────────────────────────────────────────────────────────────────────
#  剧本执行器
# ──────────────────────────────────────────────────────────────────────────

def _resolve(tree: dict, ref: str) -> str:
    """把 '@root' / '@模拟日常组' 解析成真实 node_id。"""
    if ref == "@root":
        return tree["root_id"]
    name = ref[1:]
    for node in tree["nodes"]:
        if node["name"] == name:
            return node["id"]
    raise AssertionError(f"fixture 里引用了不存在的节点 {ref}")


def _payload(op: dict, tree: dict) -> dict:
    payload = dict(op)
    for key in ("parent_id", "to_node_id", "node_id"):
        if key in payload and isinstance(payload[key], str):
            payload[key] = _resolve(tree, payload[key])
    return payload


def _run_script(golden: dict) -> tuple[str, list[tuple[dict, dict]]]:
    """按剧本走一遍，返回 (book_id, [(step, tree), ...])。

    被拒绝的 op 不产生修订，但树会被重新读一遍用于断言「原样不动」。
    """
    book_id = ""
    tree: dict = {}
    snapshots: list[tuple[dict, dict]] = []
    for step in golden["steps"]:
        op = step.get("op")
        if op is not None:
            kind = op["op"]
            if kind == "create_book":
                tree = service.create_book(
                    op.get("name", BOOK_NAME),
                    op.get("date_from", BOOK_DATE_FROM),
                    op.get("date_to", BOOK_DATE_TO),
                )
                book_id = tree["book"]["id"]
            elif kind == "reset_to_m0":
                tree = service.reset_to_m0(book_id)
            elif step["expect"].get("rejected"):
                with pytest.raises(LedgerError) as exc:
                    service.apply_op(book_id, _payload(op, tree), tree["seq"])
                assert exc.value.code == step["expect"]["error_code"], (
                    f"步骤「{step['name']}」的错误码不对："
                    f"期望 {step['expect']['error_code']}，实际 {exc.value.code}"
                )
                tree = service.get_tree(book_id)
            else:
                tree = service.apply_op(book_id, _payload(op, tree), tree["seq"])
        snapshots.append((step, tree))
    return book_id, snapshots


# ──────────────────────────────────────────────────────────────────────────
#  1. fixture 自身：notes 与自洽性
# ──────────────────────────────────────────────────────────────────────────

_NUM = r"-?\d+(?:\.\d{1,2})?"
_ANNOTATION = re.compile(r"\([^()]*[^\d\+\-\.\s][^()]*\)")   # (τ1) / (根直属) / (模拟日常组)
_EQUATION = re.compile(rf"(?P<expr>{_NUM}(?:\s*[+-]\s*{_NUM})*)\s*=\s*(?P<val>{_NUM})")


def _equations(line: str) -> list[tuple[Decimal, Decimal]]:
    """从一行 notes 里抠出所有「表达式 = 数值」，并把表达式算出来。"""
    text = _ANNOTATION.sub(" ", line)
    found = []
    for match in _EQUATION.finditer(text):
        expr = match.group("expr")
        # 表达式只允许数字与加减；出现别的字符说明这行不是可验算的加法。
        assert re.fullmatch(rf"{_NUM}(?:\s*[+-]\s*{_NUM})*", expr), \
            f"notes 里出现了无法验算的表达式：{expr!r}（出自 {line!r}）"
        terms = re.findall(r"[+-]?\s*\d+(?:\.\d{1,2})?", expr)
        value = sum(Decimal(term.replace(" ", "")) for term in terms)
        found.append((value, Decimal(match.group("val"))))
    return found


def _check_notes_arithmetic(notes: list[str], expect: dict, where: str) -> int:
    """逐条重算 notes 里的加法，并保证每个 expected 键都被 notes 覆盖。"""
    checked = 0
    for line in notes:
        for computed, stated in _equations(line):
            assert computed == stated, (
                f"{where}：notes 里的加法自己就对不上 —— "
                f"算出来 {computed}，写的是 {stated}（{line!r}）"
            )
            checked += 1
    for key in ("root_e", "root_i", "root_net", "node_count", "leg_count"):
        lines = [ln for ln in notes if ln.strip().startswith(key)]
        assert lines, f"{where}：notes 里没有 {key} 的加法过程"
        for line in lines:
            equations = _equations(line)
            assert equations, f"{where}：{key} 的那行 notes 没有可验算的加法：{line!r}"
            values = [stated for _computed, stated in equations]
            assert Decimal(str(expect[key])) in values, (
                f"{where}：{key} 的 notes 加不出期望值 {expect[key]}（{line!r} → {values}）"
            )
    return checked


def test_fixture_notes_are_machine_verifiable(golden: dict):
    """notes 里的每一处加法都必须真的算得通，且能加出 expected。"""
    total = 0
    for tx in golden["txs"]:
        assert tx.get("notes"), f"τ{tx['id']} 没有 notes"
    for step in golden["steps"]:
        assert step.get("name"), "步骤没有名字"
        assert step.get("notes"), f"步骤「{step['name']}」没有 notes"
        total += _check_notes_arithmetic(step["notes"], step["expect"], step["name"])
    chain = golden["audit_chain"]
    assert chain.get("notes"), "审计链没有 notes"
    for line in chain["notes"]:
        for computed, stated in _equations(line):
            assert computed == stated, (
                f"审计链 notes 的加法自己就对不上 —— 算出来 {computed}，写的是 {stated}（{line!r}）"
            )
            total += 1
    assert total > 0


def test_fixture_m0_is_self_consistent(golden: dict):
    """M₀ 的期望值必须能被各笔期望加起来 —— 防止改了 tx 忘了改 M₀。"""
    m0 = next(s for s in golden["steps"] if s["name"].startswith("S1"))
    members = [tx for tx in golden["txs"] if tx.get("book_member", True)]
    e = sum(Decimal(tx["expect_e"]) for tx in members)
    i = sum(Decimal(tx["expect_i"]) for tx in members)
    expect = m0["expect"]
    assert Decimal(expect["root_e"]) == e, f"M₀ 支出 {expect['root_e']} ≠ 各笔之和 {e}"
    assert Decimal(expect["root_i"]) == i, f"M₀ 收入 {expect['root_i']} ≠ 各笔之和 {i}"
    assert Decimal(expect["root_net"]) == i - e
    assert expect["leg_count"] == len(members)
    assert expect["node_count"] == 1, "M₀ 是深度为 1 的退化树"


def test_fixture_contains_no_real_looking_personal_data(golden: dict):
    """fixture 必须完全虚构：没有真实姓名、卡号、手机号、真实商户。"""
    blob = json.dumps(golden, ensure_ascii=False)
    for pattern in (r"\d{16,}", r"1[3-9]\d{9}", r"[\w.+-]+@[\w-]+\.[\w.]+"):
        assert not re.search(pattern, blob), f"fixture 里出现了疑似真实个人信息：{pattern}"
    for tx in golden["txs"]:
        assert tx["item"].startswith("模拟") or "本人账户" in tx["item"], \
            f"τ{tx['id']} 的商品说明不像虚构数据"
        assert tx["tx_no"].startswith("DEMO-"), f"τ{tx['id']} 的交易号不像虚构数据"
        assert tx["merchant_no"].startswith("DEMO-"), f"τ{tx['id']} 的订单号不像虚构数据"


# ──────────────────────────────────────────────────────────────────────────
#  2. 原子层：方向、退款、转账的识别结果
# ──────────────────────────────────────────────────────────────────────────

def test_each_tx_effective_direction_matches_fixture(env: Path, golden: dict):
    """方向判定表 —— fixture 写死的期望必须和引擎一致。

    期望值是「这条交易最终进账的金额」：
        * 全额退款 / 孤立退款 / 退款流水 / 账户搬运 → 0.00 / 0.00
        * 部分退款的原交易 → 原金额 − 累计退款金额
        * 其余 → 中性方向规则（默认支出，商品说明含「收益发放」→收入）后的原子值
    """
    conn = db_mod.get_conn()
    try:
        facts = loader.raw_transactions(conn, [tx["id"] for tx in golden["txs"]])
    finally:
        conn.close()
    special = analyze_special_transactions(facts)
    zeroed = set(special["refund_zero_ids"]) | set(special["transfer_zero_ids"])
    for tx in golden["txs"]:
        fact = next(f for f in facts if int(f["id"]) == tx["id"])
        if tx["id"] in zeroed:
            e = i = Decimal(0)
        else:
            refunded = Decimal(special["refund_amounts_by_original"].get(tx["id"], 0)) / 100
            trimmed = dict(fact)
            trimmed["amount"] = float(Decimal(str(fact["amount"])) - refunded)
            raw_e, raw_i = effective_atom_value(trimmed)
            e = Decimal(raw_e) / 100
            i = Decimal(raw_i) / 100
        assert Decimal(tx["expect_e"]) == e, \
            f"τ{tx['id']} 的有效支出应为 {tx['expect_e']}，实际 {e}"
        assert Decimal(tx["expect_i"]) == i, \
            f"τ{tx['id']} 的有效收入应为 {tx['expect_i']}，实际 {i}"


def test_special_transactions_are_recognised_only_with_evidence(env: Path, golden: dict):
    """退款 / 内部转账的识别结果必须与 fixture 一致。"""
    conn = db_mod.get_conn()
    try:
        facts = loader.raw_transactions(conn, [tx["id"] for tx in golden["txs"]])
    finally:
        conn.close()
    result = analyze_special_transactions(facts)
    assert result["refund_zero_ids"] == {5, 6, 8, 12}, "全额退款/孤立退款都应归零"
    assert result["partial_refund_ids"] == {7, 8}, "τ7 是部分退款，τ8 是它的退款流水"
    assert result["refund_amounts_by_original"] == {7: 8000, 5: 8800}, \
        "累计退款金额：τ7 退 80.00、τ5 全额退 88.00"
    assert result["refund_pairs"] == [(5, 6), (7, 8)], "退款配对必须唯一"
    assert result["transfer_zero_ids"] == {9, 10}, "本人账户互转应归零"
    assert result["transfer_pairs"] == [(9, 10)]
    assert result["refund_conflict_ids"] == set(), "本 fixture 不应出现退款证据冲突"
    assert any("未找到" in w for w in result["warnings"]), "孤立退款必须给出警告"


def _fake_tx(tx_id: int, direction: str, amount: float, **overrides) -> dict:
    """造一条「只在内存里」的交易记录，用来钉住识别规则的边界。

    这些记录不进数据库，也不进 fixture —— 它们的作用是把「不应该成立」的
    情形显式写出来：金额相同但没有任何明确证据时，绝不能配对。
    """
    base = {
        "id": tx_id,
        "direction": direction,
        "amount": amount,
        "source": "模拟演示平台",
        "item": "模拟演练交易",
        "tx_type": "模拟消费",
        "status": "交易成功",
        "tx_no": "",
        "merchant_no": "",
        "raw_json": {},
    }
    base.update(overrides)
    return base


def test_equal_amount_alone_is_never_evidence():
    """金额相同 ≠ 退款关联；没有明确证据就绝不能配对。

    这是 V3 退款识别的核心纪律：金额相同只是巧合，不能当成证据。
    配对只认三种：平台累计成功退款金额 / 唯一明确的原交易引用 / 唯一的订单号交集。
    """
    # ① 金额完全相同，但订单号、交易号、引用全都没有 → 不能配对。
    facts = [
        _fake_tx(101, "支出", 88.00),
        _fake_tx(102, "收入", 88.00, tx_type="模拟退款", item="模拟退款流水"),
    ]
    result = analyze_special_transactions(facts)
    assert result["refund_pairs"] == [], "金额相同不构成退款关联证据"
    assert 101 not in result["refund_zero_ids"], "原交易不应被归零"
    assert 101 not in result["refund_amounts_by_original"], "不应扣减原交易金额"
    assert 102 in result["refund_zero_ids"], "无法关联的退款流水自身归零"
    assert any("未找到" in w for w in result["warnings"]), "孤立退款必须给出警告"

    # ② 金额相同 + 唯一订单号交集 → 允许配对（这才是合法证据）。
    facts = [
        _fake_tx(201, "支出", 88.00, merchant_no="DEMO-PROOF-0001"),
        _fake_tx(202, "收入", 88.00, tx_type="模拟退款", item="模拟退款流水",
                 merchant_no="DEMO-PROOF-0001"),
    ]
    result = analyze_special_transactions(facts)
    assert result["refund_pairs"] == [(201, 202)], "唯一订单号交集是合法证据"

    # ③ 显式原交易引用 → 允许配对。
    facts = [
        _fake_tx(301, "支出", 30.00, merchant_no="DEMO-PROOF-0002"),
        _fake_tx(302, "收入", 30.00, tx_type="模拟退款", item="模拟退款流水",
                 raw_json={"原交易ID": "301"}),
    ]
    result = analyze_special_transactions(facts)
    assert result["refund_pairs"] == [(301, 302)], "显式原交易引用是合法证据"

    # ④ 平台累计成功退款金额 → 允许配对（哪怕退款流水根本没导入）。
    facts = [
        _fake_tx(401, "支出", 45.00, raw_json={"成功退款（元）": "45.00"}),
        _fake_tx(402, "收入", 45.00, tx_type="模拟退款", item="模拟退款流水"),
    ]
    result = analyze_special_transactions(facts)
    assert result["refund_zero_ids"] == {401, 402}, "平台累计退款金额是权威证据"
    assert result["refund_amounts_by_original"] == {401: 4500}

    # ⑤ 内部转账：缺「本人账户」标记 → 不能配对（本系统与外部账户互转是真实业务）。
    facts = [
        _fake_tx(501, "支出", 150.00, tx_type="转账", merchant_no="DEMO-MOVE-9001",
                 item="模拟账户转出"),
        _fake_tx(502, "收入", 150.00, tx_type="转账", merchant_no="DEMO-MOVE-9001",
                 item="模拟账户转入"),
    ]
    result = analyze_special_transactions(facts)
    assert result["transfer_pairs"] == [], "缺本人账户标记的转账不能配对"
    assert result["transfer_zero_ids"] == set(), "不能配对的转账不得归零"

    # ⑥ 补上「本人账户互转」标记 → 可以配对并归零。
    facts = [
        _fake_tx(601, "支出", 150.00, tx_type="转账", merchant_no="DEMO-MOVE-9002",
                 item="模拟本人账户互转转出"),
        _fake_tx(602, "收入", 150.00, tx_type="转账", merchant_no="DEMO-MOVE-9002",
                 item="模拟本人账户互转转入"),
    ]
    result = analyze_special_transactions(facts)
    assert result["transfer_pairs"] == [(601, 602)]
    assert result["transfer_zero_ids"] == {601, 602}


# ──────────────────────────────────────────────────────────────────────────
#  3. 逐步执行黄金剧本
# ──────────────────────────────────────────────────────────────────────────

def test_golden_steps(env: Path, golden: dict):
    """按 fixture 的步骤走一遍，每一步的根值都必须对得上。"""
    _book_id, snapshots = _run_script(golden)
    assert snapshots, "剧本是空的"
    for step, tree in snapshots:
        expect = step["expect"]
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
        assert tree["invariants_ok"] is True and tree["audit_ok"] is True, \
            f"步骤「{step['name']}」后引擎自检没过"
        assert len(tree["invariants"]) == 6, "六条不变量都要在"

        # 节点自身的 op（ADD / NET）也要钉住。
        # 为什么单列：节点值只靠 e/i 判定的话，「切了 op 但数字碰巧没变」会漏过去
        # —— 纯支出的组 ADD 和 NET 数值完全一样（见 fixture 第 S5 步的 notes）。
        for ref, want_op in (expect.get("node_ops") or {}).items():
            node = next((n for n in tree["nodes"] if n["id"] == _resolve(tree, ref)), None)
            assert node is not None, f"步骤「{step['name']}」里找不到节点 {ref}"
            assert node["op"] == want_op, \
                f"步骤「{step['name']}」节点 {ref} 的 op 不对：期望 {want_op}，实际 {node['op']}"

        # 每一步的审计链都要闭合，且与 fixture 的手算一致。
        audit = tree["audit"]
        assert audit["root_net"] == pytest.approx(audit["expected_net"]), \
            f"步骤「{step['name']}」审计链不闭合：{audit}"
        assert audit["root_net"] == pytest.approx(float(expect["root_net"])), \
            f"步骤「{step['name']}」审计链的根净值与 expected 不一致：{audit}"
        for key, want in (expect.get("audit") or {}).items():
            assert audit[key] == pytest.approx(float(want)), \
                f"步骤「{step['name']}」审计链 {key} 不对：期望 {want}，实际 {audit[key]}"


def test_golden_invariants_stay_green(env: Path, golden: dict):
    """走完全部步骤后，不变量（I1–I6）必须仍然全绿。"""
    book_id, snapshots = _run_script(golden)
    conn = db_mod.get_conn()
    try:
        for step, _tree in snapshots:
            state = loader.read_state(conn, book_id)
            facts = loader.raw_transactions(conn, state["members"])
            proof = invariants.validate(state, facts, book_id)
            assert proof["audit_ok"] is True, f"步骤「{step['name']}」后审计没过"
            assert proof["root_e"] == proof["totals"][state["nodes"][0]["id"]]["e"]
            assert len(proof["invariants"]) == 6
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────
#  4. 审计链（手算）
# ──────────────────────────────────────────────────────────────────────────

def test_audit_chain_matches_hand_computed(env: Path, golden: dict):
    """审计链的每一项都要和 fixture 里手算的数一致，并且闭合。"""
    spec = golden["audit_chain"]["m0"]
    book_id, _snapshots = _run_script(golden)
    conn = db_mod.get_conn()
    try:
        state = loader.read_state(conn, book_id)
        facts = loader.raw_transactions(conn, state["members"])
        proof = invariants.validate(state, facts, book_id)
    finally:
        conn.close()
    audit = proof["audit"]
    for key, want in spec.items():
        if key == "closed":
            continue
        assert audit[key] == cents(want), \
            f"审计链 {key} 不对：期望 {want}，实际 {audit[key] / 100}"
    # 闭合：原始有符号和 + 中性定向 + 退款 + 转账 + 分裂 = 根的净值
    total = (
        audit["raw_net"] + audit["neutral_direction_delta"]
        + audit["refund_delta"] + audit["transfer_delta"]
        + audit["split_delta"]
    )
    assert total == audit["root_net"], f"审计链不闭合：{total} ≠ {audit['root_net']}"
    assert spec["closed"] is True


# ──────────────────────────────────────────────────────────────────────────
#  5. 撤销 / 重做：整份状态快照回放（V3 的关键性质）
# ──────────────────────────────────────────────────────────────────────────

def _shape(tree: dict) -> dict:
    """把一棵树压成可比对的形状：根值 + 节点数 + 腿数 + 每个节点的 op。"""
    return {
        "root_e": round(tree["root_e"], 2),
        "root_i": round(tree["root_i"], 2),
        "root_net": round(tree["root_net"], 2),
        "node_count": len(tree["nodes"]),
        "leg_count": len(tree["legs"]),
        "node_ops": {n["name"]: n["op"] for n in tree["nodes"]},
    }


def _expect_shape(expect: dict) -> dict:
    """fixture 的 node_ops 用 '@root' 指根节点，引擎里根节点叫「全部账目」。"""
    return {
        "root_e": round(float(expect["root_e"]), 2),
        "root_i": round(float(expect["root_i"]), 2),
        "root_net": round(float(expect["root_net"]), 2),
        "node_count": expect["node_count"],
        "leg_count": expect["leg_count"],
        "node_ops": {
            (ROOT_NODE_NAME if ref == "@root" else ref[1:]): op
            for ref, op in (expect.get("node_ops") or {}).items()
        },
    }


def test_undo_redo_roundtrip_is_lossless(env: Path, golden: dict):
    """逐步操作 → 撤销回 M₀ → 重做回来，每个快照都要和当时的 expected 完全相同。

    V3 的撤销不是「反向操作」，而是把 ledger_history.state_json 整份状态回放。
    所以这里比对的不是几个字段，而是**全部**可比对的字段：
    root_e / root_i / root_net / node_count / leg_count / 每个节点的 op。
    """
    book_id, snapshots = _run_script(golden)

    # 记录每个「真正产生了修订」的步骤的期望形状。
    effective: list[tuple[str, dict]] = []
    for step, tree in snapshots:
        if step.get("op") is not None and not step["expect"].get("rejected"):
            effective.append((step["name"], _expect_shape(step["expect"])))
    assert len(effective) >= 15, "剧本里的有效步骤太少，测不出回放的性质"

    m0_index = next(
        (i for i, (name, _shape_) in enumerate(effective) if name.startswith("S1")), None
    )
    assert m0_index is not None, "剧本里找不到 M₀ 步骤"

    # 一路撤销回 M₀。
    for index in range(len(effective) - 1, m0_index, -1):
        tree = service.undo(book_id)
        target = effective[index - 1][1]
        assert _shape(tree) == target, (
            f"撤销到「{effective[index - 1][0]}」后与当时的 expected 不一致："
            f"{_shape(tree)} ≠ {target}"
        )
    assert _shape(service.get_tree(book_id)) == effective[m0_index][1], \
        "撤销回 M₀ 后应与 M₀ 逐字相同"

    # 一路重做回来。
    for index in range(m0_index + 1, len(effective)):
        tree = service.redo(book_id)
        target = effective[index][1]
        assert _shape(tree) == target, (
            f"重做到「{effective[index][0]}」后与当时的 expected 不一致："
            f"{_shape(tree)} ≠ {target}"
        )
    final = service.get_tree(book_id)
    assert _shape(final) == effective[-1][1], "重做到底应与最后一步的 expected 相同"
    assert final["invariants_ok"] is True and final["audit_ok"] is True
