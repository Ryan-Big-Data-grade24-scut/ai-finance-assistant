"""
backend/ledger/invariants.py —— 引擎要守的东西（纯函数，零 IO）

代数 §5 的六条不变量 + 引擎自己的完备性 + **审计链**。

    (I1) 腿的唯一归属      每条腿恰好属于一个节点
    (I2) 腿的完备性        ⋃_n legs(n) = ⋃_τ legs(τ)
    (I3) 分裂对称          ℓ₁ +_1 ℓ₂ = (a, a)
    (I4) 分裂去向唯一      ℓ₂ 计入外部收入节点（无其他可能）
    (I5) 节点可加性        Y_n = op_n( ⨆ children ∪ legs )
    (I6) NET 幂等          net(net(X₁)+net(X₂)) = net(X₁+X₂)

    (C1) = I1   (C2) = I2        —— 引擎 §5.1 的两条

    (T1) 完备    legs 覆盖所有 τ，无孤儿   —— 代数 §6
    (T3) 目的达成 —— **机器判不了**，是人回答的。不在这里查。

**审计链必须闭合**（代数 §5）：

    ①  Σ s(τ)·a(τ)          平台原始有符号和
    ②  + 中性定向产生的变化   (engine_sign − platform_sign)·a(τ)
    ③  + 分裂新增的镜像和     Σ_{ℓ₂} (i − e)
    ④  + NET 重排的影响       ≡ 0
    ⑤  = 根的净值            i(root) − e(root)

    不闭合 = **实现缺陷**，不是口径分歧。
    口径分歧永远是链路上的一行；缺陷是不在链路上的东西。

⚠️ 所有失败消息都写成人话，并带上**具体的数字**。前端要把这句话
   原样弹给用户，所以「assertion failed」这种写法一律禁止。
"""
from __future__ import annotations

from dataclasses import dataclass

from .atoms import (
    DIRECTION_EXPENSE,
    DIRECTION_INCOME,
    legs_for,
    platform_sign,
    resolve_direction,
)
from .amount import Y, ZERO
from .errors import Code, LedgerError
from .tree import (
    KIND_EXTERNAL_INCOME,
    OP_ADD,
    OP_NET,
    ROLE_MIRROR,
    ROLE_PRIMARY,
    Ledger,
    aggregate,
)


@dataclass(frozen=True)
class CheckResult:
    """一条检查的结果。ok=False 时 message 必须能直接给人看。"""

    name: str
    ok: bool
    message: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "message": self.message}


# ──────────────────────────────────────────────────────────────
#  I1 / C1 —— 腿的唯一归属
# ──────────────────────────────────────────────────────────────

def check_i1(ledger: Ledger) -> CheckResult:
    """每条腿恰好属于一个节点。

    结构上 `tree.legs` 是 dict，天然唯一；真正要查的是
    「腿指向的节点还在不在」「同一笔交易的角色有没有重复」。
    """
    tree = ledger.tree
    for (tx_id, role), node_id in tree.legs.items():
        if node_id not in tree.nodes:
            return CheckResult(
                "I1", False,
                f"交易 {tx_id} 的{_role_cn(role)}腿指向了一个不存在的节点（{node_id}）。",
            )
        if tx_id not in ledger.txs:
            return CheckResult(
                "I1", False,
                f"账本里有一笔不属于本账的交易（{tx_id}）占了腿位。",
            )
    return CheckResult("I1", True)


# ──────────────────────────────────────────────────────────────
#  I2 / C2 / T1 —— 腿的完备性
# ──────────────────────────────────────────────────────────────

def check_i2(ledger: Ledger) -> CheckResult:
    """⋃_n legs(n) = ⋃_τ legs(τ)：不漏、不多。

    对每一笔交易：引擎认为它该有几条腿（primary 必有，mirror 视分裂而定），
    树里就得有几条。数量和角色都要对上。
    """
    tree = ledger.tree
    for tx_id, tx in ledger.txs.items():
        expected = {role for role, _ in legs_for(tx, split=ledger.is_split(tx_id))}
        actual = {role for (tid, role) in tree.legs if tid == tx_id}
        missing = expected - actual
        extra = actual - expected
        if missing:
            return CheckResult(
                "I2", False,
                f"交易 {tx_id} 少了{_roles_cn(missing)}，它没被完全记进账里。",
            )
        if extra:
            return CheckResult(
                "I2", False,
                f"交易 {tx_id} 多了{_roles_cn(extra)}，账上有不该有的腿。",
            )
    return CheckResult("I2", True)


def check_t1(ledger: Ledger) -> CheckResult:
    """完备：每笔 τ 都有腿（无孤儿），每个持腿的节点都在树里。"""
    for tx_id in ledger.txs:
        if (tx_id, ROLE_PRIMARY) not in ledger.tree.legs:
            return CheckResult(
                "T1", False,
                f"交易 {tx_id} 在账上没有腿 —— 它被漏掉了。",
            )
    return CheckResult("T1", True)


# ──────────────────────────────────────────────────────────────
#  I3 —— 分裂对称
# ──────────────────────────────────────────────────────────────

def check_i3(ledger: Ledger) -> CheckResult:
    """ℓ₁ +_1 ℓ₂ = (a, a)：镜像腿的金额相等、方向相反。"""
    for tx_id in ledger.txs:
        if not ledger.is_split(tx_id):
            continue
        p = ledger.leg_value(tx_id, ROLE_PRIMARY)
        m = ledger.leg_value(tx_id, ROLE_MIRROR)
        if p.e != m.i or p.i != m.e:
            return CheckResult(
                "I3", False,
                f"交易 {tx_id} 的两条腿不对称：主腿 ({p.e}, {p.i})，镜像腿 ({m.e}, {m.i})。",
            )
    return CheckResult("I3", True)


# ──────────────────────────────────────────────────────────────
#  I4 —— 分裂去向唯一
# ──────────────────────────────────────────────────────────────

def check_i4(ledger: Ledger) -> CheckResult:
    """ℓ₂ 计入外部收入节点，没有其他可能。"""
    tree = ledger.tree
    ext = tree.external_income_node()
    for tx_id in ledger.txs:
        if not ledger.is_split(tx_id):
            continue
        owner = tree.leg_owner(tx_id, ROLE_MIRROR)
        if ext is None:
            return CheckResult(
                "I4", False,
                f"交易 {tx_id} 有镜像腿，但这本账里没有「外部收入节点」。",
            )
        if owner != ext:
            return CheckResult(
                "I4", False,
                f"交易 {tx_id} 的镜像腿跑到「{owner}」去了，"
                f"它只能待在「外部收入节点」（{ext}）。",
            )
    return CheckResult("I4", True)


# ──────────────────────────────────────────────────────────────
#  I5 —— 节点可加性
# ──────────────────────────────────────────────────────────────

def check_i5(ledger: Ledger) -> CheckResult:
    """Y_n = op_n( ⨆ children ∪ legs )。

    aggregate() 本身就是这个定义，所以这里做的是**独立重算**：
    一条路走 aggregate()，另一条路手工沿树展开，比对。
    两条路不一致 = aggregate 里写错了。
    """
    tree = ledger.tree
    vals = aggregate(ledger)
    for nid, node in tree.nodes.items():
        total = Y()
        for kid in tree.children_of(nid):
            total = total + vals.get(kid, Y())
        for tx_id, role in tree.legs_of(nid):
            total = total + ledger.leg_value(tx_id, role)
        expect = total.net() if node.op == OP_NET else total
        got = vals.get(nid, Y())
        if expect.e != got.e or expect.i != got.i:
            return CheckResult(
                "I5", False,
                f"节点「{node.name}」（{nid}）的值对不上："
                f"算出来是 ({expect.e}, {expect.i})，但账上是 ({got.e}, {got.i})。",
            )
    return CheckResult("I5", True)


# ──────────────────────────────────────────────────────────────
#  I6 —— NET 幂等
# ──────────────────────────────────────────────────────────────

def check_i6(ledger: Ledger) -> CheckResult:
    """net(net(X₁) +_1 net(X₂)) = net(X₁ +_1 X₂)。

    逐个 NET 节点检查：对它的两个子集分别净额化再相加，
    必须等于整体一起净额化。
    """
    tree = ledger.tree
    vals = aggregate(ledger)
    for nid, node in tree.nodes.items():
        if node.op != OP_NET:
            continue
        kids = tree.children_of(nid)
        parts = [vals.get(k, Y()) for k in kids]
        for tx_id, role in tree.legs_of(nid):
            parts.append(ledger.leg_value(tx_id, role))
        if len(parts) < 2:
            continue
        x1, x2 = parts[0], parts[1]
        lhs = (x1.net() + x2.net()).net()
        rhs = (x1 + x2).net()
        if lhs.e != rhs.e or lhs.i != rhs.i:
            return CheckResult(
                "I6", False,
                f"节点「{node.name}」（{nid}）的净额化不幂等："
                f"分开算是 ({lhs.e}, {lhs.i})，一起算是 ({rhs.e}, {rhs.i})。",
            )
    return CheckResult("I6", True)


# ──────────────────────────────────────────────────────────────
#  审计链
# ──────────────────────────────────────────────────────────────

def audit_chain(ledger: Ledger) -> dict:
    """把账从原始原子一路算到根，返回链上的每一项。

    返回值同时给机器（对数）和人（看哪一项不对）。
    """
    # ① 平台原始有符号和
    s1 = Y()
    # ② 中性定向 / 退款归零 / 搬运归零 产生的变化
    s2 = Y()
    for tx in ledger.txs.values():
        direction, _ = resolve_direction(tx)
        eng = -1 if direction == DIRECTION_EXPENSE else (
            1 if direction == DIRECTION_INCOME else 0
        )
        plat = platform_sign(tx)
        amt = tx.amount
        # ① 平台原始有符号和
        if plat < 0:
            s1 = s1 + Y(amt, ZERO)
        elif plat > 0:
            s1 = s1 + Y(ZERO, amt)
        # ② 中性定向 / 退款归零 / 搬运归零 产生的变化
        delta = eng - plat
        if delta > 0:
            s2 = s2 + Y(ZERO, amt * delta)
        elif delta < 0:
            s2 = s2 + Y(amt * (-delta), ZERO)
    # ③ 分裂新增的镜像和
    s3 = Y()
    for tx_id in ledger.txs:
        if not ledger.is_split(tx_id):
            continue
        m = ledger.leg_value(tx_id, ROLE_MIRROR)
        s3 = s3 + m
    # ④ NET 重排的影响 ≡ 0（净额化不改变净值）
    s4 = Y()
    # ⑤ 根的净值
    root = aggregate(ledger).get(ledger.tree.root_id, Y())
    s5 = Y(root.e, root.i)

    total = s1 + s2 + s3 + s4
    return {
        "s1_platform": s1,
        "s2_reorientation": s2,
        "s3_mirror": s3,
        "s4_net_reorder": s4,
        "s5_root": s5,
        "total": total,
        "root_net_value": root.net_value,
        "closed": total.net_value == root.net_value,
    }


def check_audit(ledger: Ledger) -> CheckResult:
    """审计链必须闭合。

    闭合的判据用**净值**（i−e）比，而不是逐分量比：
    链上每一项都是 Y，但 NET 重排只影响分量的归属、不影响净值，
    所以净值相等才是「没有交易凭空出现或消失」的准确表述。
    """
    chain = audit_chain(ledger)
    if not chain["closed"]:
        lhs = chain["total"].net_value
        rhs = chain["root_net_value"]
        return CheckResult(
            "AUDIT", False,
            f"账没对上：链上算出来是 {lhs}，但根上的净值是 {rhs}，"
            f"差了 {rhs - lhs}。",
        )
    return CheckResult("AUDIT", True)


# ──────────────────────────────────────────────────────────────
#  汇总
# ──────────────────────────────────────────────────────────────

ALL_CHECKS = (
    check_i1,
    check_i2,
    check_t1,
    check_i3,
    check_i4,
    check_i5,
    check_i6,
    check_audit,
)


def check_all(ledger: Ledger) -> list[CheckResult]:
    """跑全部检查。返回逐条结果（不抛异常）。"""
    return [fn(ledger) for fn in ALL_CHECKS]


def assert_all(ledger: Ledger) -> None:
    """跑全部检查；任何一条不过就抛 LedgerError。

    **「不通过就拒绝」是免费获得的** —— service 层在事务里调它，
    异常一抛，`db.transaction` 自动 ROLLBACK。
    """
    for res in check_all(ledger):
        if not res.ok:
            raise LedgerError(
                res.message,
                code=Code.INVARIANT_BROKEN,
                http_status=409,
                detail={"check": res.name},
            )


def _role_cn(role: str) -> str:
    return "镜像" if role == ROLE_MIRROR else "主"


def _roles_cn(roles: set[str]) -> str:
    return "、".join(f"{_role_cn(r)}腿" for r in sorted(roles))
