"""
backend/ledger/atoms.py —— 原子层（纯函数，零 IO）

代数 §2：**所有判断都在这一层做完**（推论 2）。
节点层不重新解释任何一笔交易，它只做聚合。

腿的生成（代数 §2.6）三分支铁律，顺序不可交换：

    IF τ 是退款            →  1 条零腿
    ELIF τ 涉及外部额度    →  2 条（一去路 + 一来源，金额相等方向相反）
    ELSE                   →  1 条主腿

⛔ 顺序说明：代数 §2.5 说「先合并退款，再决定分裂」，所以退款笔
永远不会产生镜像腿 —— (0,0) 分裂仍是 (0,0)，没有意义。

──────────────────────────── 中性交易的口径 ────────────────────────────

真实数据里 direction='中性' 的有 900+ 笔，占 32%（余额宝 / 薪金宝 /
花呗等账户搬运）。这是本引擎**唯一**的方向判定点：

    支出  → 支出
    收入  → 收入
    中性  → 支出          ← 默认值（用户已定）
            除非 item 含「收益发放」→ 收入

出处：上一轮原型 Temp/md/20261015_ai-finance-impl/model/compute_v2.py
    sign = {"支出": -1, "收入": 1, "中性": -1}
    # 中性但明确是收入的（余额宝派息）—— 显式声明，不靠猜
    if a["bill_direction"] == "中性" and "收益发放" in (a["item"] or ""):
        a["sign"] = 1

⚠️ 这套规则和 reports/_common.py 的 type_of() 是**两回事**，不要合并：
    reports 那套是 API 枚举映射（库里「中性」→ 'neutral'）；
    引擎这套是记账方向（库里「中性」→ 支出）。
    混在一起，第一轮一改就会带崩引擎。

──────────────────────── 两种「归零」的语义 ─────────────────────────

1. 退款（代数 §2.5）：退款意味着资源回到了原处 —— 既没消费，也没获得。
   所以退款笔判零腿；它的配对扣款笔**也**判零腿（两者相抵为 0）。
   真实数据里出现过「退款笔找不到配对扣款」的情况（青桔骑行的扣款因
   CHECK(amount > 0) 未入库）—— 此时退款笔**单独**判零腿：没有原扣款
   更说明没有消费发生。

2. 账户搬运（代数 §2.4）：自己的账户之间划转**不分裂**。
   上一轮原型对搬运也做了 split 再靠同组 NET 归零，那违反了代数。
   本引擎按代数实现：funding='账户搬运' 的 τ 定向为零腿，
   不分裂、不计收支，但树里看得见。数值与原型等价，腿更少。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .amount import Y, ZERO, q2

# ---- 引擎判定的方向 ----
DIRECTION_EXPENSE = "expense"
DIRECTION_INCOME = "income"
DIRECTION_ZERO = "zero"

# ---- 平台原始方向（来自 transactions_raw.direction）----
PLATFORM_EXPENSE = "支出"
PLATFORM_INCOME = "收入"
PLATFORM_NEUTRAL = "中性"

#: 中性里明确是收入的标记。余额宝每日派息这类。
INCOME_MARKERS = ("收益发放",)

#: 退款候选的标记。与原型 compute_v2.py 的判定一致：
#:     a["tx_type"] == "退款" or "退款" in (a["item"] or "")
REFUND_MARKERS = ("退款",)

#: 内部搬运：自己的账户之间搬进搬出。定向为零腿，不分裂。
INTERNAL_FUNDINGS = frozenset({"账户搬运"})

#: 外部额度：额度归属方在自己名下之外。这是**唯一**允许分裂的来源。
EXTERNAL_FUNDINGS = frozenset({"亲情卡", "他人代付"})


@dataclass(frozen=True, slots=True)
class TxRow:
    """账本引擎眼里的一个原子。字段全部来自只读查询，不含任何可写状态。"""

    tx_id: int
    date: str
    tx_time: str
    raw_direction: str          # 平台原始方向：支出 / 收入 / 中性
    amount: Decimal
    item: str
    tx_type: str                # 平台原始分类（第一轮修正后的 category）
    counterparty: str
    pay_method: str
    merchant_no: str            # v_tx 不暴露，退款配对要用
    tx_no: str
    funding: str                # 自有 / 亲情卡 / 他人代付 / 账户搬运
    category: str
    is_exempt: bool


def is_refund_candidate(tx: TxRow) -> bool:
    """退款候选：平台分类是「退款」，或商品说明里含「退款」。"""
    return tx.tx_type == "退款" or any(m in (tx.item or "") for m in REFUND_MARKERS)


def split_applicable(tx: TxRow) -> bool:
    """分裂适用域（代数 §2.4）：**仅当** τ 涉及外部额度。

    依据 tx_overrides.funding —— 显式配置，不靠金额或商户名猜。
    退款笔不适用：代数 §2.5 的退款合并优先级高于分裂。
    """
    if tx.funding not in EXTERNAL_FUNDINGS:
        return False
    return not is_refund_candidate(tx)


def resolve_direction(tx: TxRow) -> tuple[str, str]:
    """平台方向 → 引擎方向。返回 (方向, 规则 id)。

    规则 id 会进审计链和前端 tooltip，让人看得见这笔为什么这么算。
    这是本模块**唯一**的方向判定点。
    """
    if tx.funding in INTERNAL_FUNDINGS:
        return DIRECTION_ZERO, "账户搬运·定向为零"
    if is_refund_candidate(tx):
        return DIRECTION_ZERO, "退款·先合并归零"
    if tx.raw_direction == PLATFORM_EXPENSE:
        return DIRECTION_EXPENSE, "支出"
    if tx.raw_direction == PLATFORM_INCOME:
        return DIRECTION_INCOME, "收入"
    # 中性
    if any(m in (tx.item or "") for m in INCOME_MARKERS):
        return DIRECTION_INCOME, "中性·收益发放"
    return DIRECTION_EXPENSE, "中性·默认支出"


def platform_sign(tx: TxRow) -> int:
    """平台原始有符号方向：支出 -1 / 收入 +1 / 中性 0。审计链第①项用。"""
    if tx.raw_direction == PLATFORM_EXPENSE:
        return -1
    if tx.raw_direction == PLATFORM_INCOME:
        return 1
    return 0


def engine_sign(tx: TxRow) -> int:
    """引擎定向后的有符号方向。审计链第②项用它和平台方向作差。"""
    direction, _ = resolve_direction(tx)
    if direction == DIRECTION_EXPENSE:
        return -1
    if direction == DIRECTION_INCOME:
        return 1
    return 0


def leg_value(tx: TxRow, direction: str) -> Y:
    """一条腿的值。金额恒为正，方向决定落在 e 还是 i。"""
    amount = q2(tx.amount)
    if direction == DIRECTION_ZERO:
        return Y(ZERO, ZERO)
    if direction == DIRECTION_EXPENSE:
        return Y(amount, ZERO)
    return Y(ZERO, amount)


def legs_for(tx: TxRow, *, split: bool) -> list[tuple[str, Y]]:
    """原子 τ → 腿集合。代数 §2.6。

    返回 [(role, value)]，role ∈ {primary, mirror}。
    mirror = primary 的镜像（金额相等、方向相反），代数 §2.4。
    """
    direction, _ = resolve_direction(tx)
    primary = leg_value(tx, direction)
    out = [("primary", primary)]
    if split:
        out.append(("mirror", Y(primary.i, primary.e)))
    return out


def leg_ids(tx_id: int, role: str) -> str:
    """腿 id：{tx_id}:{role}。账本内唯一由 (book_id, tx_id, role) 保证。"""
    return f"{tx_id}:{role}"
