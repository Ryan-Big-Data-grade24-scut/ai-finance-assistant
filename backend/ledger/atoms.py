"""原子交易到记账腿的纯函数转换。

本模块负责两层语义：
1. build_legs：严格遵循记账代数，平台方向为“中性”时生成零值腿。
2. generate_legs：应用产品契约，中性默认按支出，商品说明包含“收益发放”时按收入；
   再处理退款、本人账户互转以及显式外部额度分裂。

本模块不访问数据库、不修改原始交易。金额统一使用整数分（cents）。
"""
from __future__ import annotations

import json
from decimal import Decimal
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .amount import cents


OWN_FUNDING = "自有"
EXTERNAL_FUNDING = frozenset({"亲情卡", "他人代付"})
SUPPORTED_FUNDING = frozenset({OWN_FUNDING, *EXTERNAL_FUNDING})

DESTINATION_TRANSACTION = "transaction"
DESTINATION_EXTERNAL_INCOME = "external_income"

ROLE_PRIMARY = "primary"
ROLE_MIRROR = "mirror"
ROLE_REFUND_ZERO = "refund_zero"

_DIRECTION_REVERSE = {
    "支出": "收入",
    "收入": "支出",
}


@dataclass(frozen=True, slots=True)
class Leg:
    """一条不可变的记账腿。e/i 均为整数分。"""

    tx_id: int
    leg_index: int
    role: str
    e_cents: int
    i_cents: int
    destination: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _transaction_id(tx: Mapping[str, Any]) -> int:
    value = tx.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("交易 id 必须是正整数。")
    return value


def _amount_cents(tx: Mapping[str, Any]) -> int:
    try:
        value = cents(tx.get("amount"))
    except Exception as exc:
        raise ValueError("交易 amount 必须是有效的正数金额。") from exc
    if value <= 0:
        raise ValueError("交易 amount 必须大于 0。")
    return value


def _direction(tx: Mapping[str, Any]) -> str:
    value = tx.get("direction")
    if value not in {"支出", "收入", "中性"}:
        raise ValueError("direction 只能是：支出、收入、中性。")
    return value


def _primary_components(direction: str, amount_cents: int) -> tuple[int, int]:
    """纯代数 atom(τ)：中性方向严格生成 (0, 0)。"""
    if direction == "支出":
        return amount_cents, 0
    if direction == "收入":
        return 0, amount_cents
    return 0, 0


def _zero_leg(tx_id: int) -> Leg:
    return Leg(
        tx_id=tx_id,
        leg_index=1,
        role=ROLE_REFUND_ZERO,
        e_cents=0,
        i_cents=0,
        destination=DESTINATION_TRANSACTION,
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _source_compatible(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """两边来源都存在时必须相同；测试/历史数据缺来源时不凭空拒绝。"""
    a, b = _text(left.get("source")), _text(right.get("source"))
    return not (a and b and a != b)


def _raw_mapping(tx: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = tx.get("raw_json")
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _successful_refund_cents(tx: Mapping[str, Any]) -> int | None:
    """读取支付宝原始行中的成功退款金额；缺字段与无效字段均返回 None。"""
    raw = _raw_mapping(tx)
    wanted = {
        "成功退款（元）",
        "成功退款(元)",
        "成功退款",
        "退款金额（元）",
        "退款金额(元)",
        "退款金额",
    }
    for key, value in raw.items():
        if str(key).strip() not in wanted:
            continue
        if value is None or not str(value).strip():
            return None
        try:
            amount = cents(value)
        except Exception:
            return None
        return amount if amount > 0 else None
    return None


def _is_refund_transaction(tx: Mapping[str, Any]) -> bool:
    """判断“退款流水”，不能把状态标记为已退款的原始交易误判成退款流水。

    微信会把原交易状态写成“已全额退款”，同时另生成一笔“转账-退款”收入。
    原交易应由 _refund_analysis 根据 status 归零；只有交易类型/商品说明明确
    表示退款的那一行，才是退款流水。
    """
    if "退款" in _text(tx.get("tx_type")):
        return True
    return tx.get("direction") == "收入" and "退款" in _text(tx.get("item"))


def is_refund_transaction(tx: Mapping[str, Any]) -> bool:
    """公开给分类工作流使用的退款流水判定。"""
    return _is_refund_transaction(tx)


def _has_full_refund_status(tx: Mapping[str, Any]) -> bool:
    status = _text(tx.get("status"))
    return any(marker in status for marker in ("已全额退款", "全额退款成功", "全额退还"))


def _has_partial_refund_status(tx: Mapping[str, Any]) -> bool:
    return "部分退款" in _text(tx.get("status"))



_REFUND_ORIGINAL_ID_KEYS = {
    "原交易ID", "原交易id", "关联交易ID", "关联交易id",
    "refund_of_tx_id", "original_tx_id", "original_transaction_id",
}
_REFUND_ORIGINAL_REF_KEYS = {
    "原交易号", "原交易订单号", "原订单号", "原商家订单号",
    "关联交易号", "关联订单号", "退款关联交易号", "退款关联订单号",
    "退款对应交易号", "退款对应订单号", "original_tx_no",
    "original_order_no", "related_transaction_no",
}


def _refund_link_keys(tx: Mapping[str, Any]) -> set[str]:
    return {
        value
        for value in (_text(tx.get("merchant_no")), _text(tx.get("tx_no")))
        if value
    }


def _explicit_refund_reference(refund: Mapping[str, Any]) -> tuple[int | None, set[str]]:
    """读取退款行显式指向的原交易 ID / 交易号 / 商家订单号。"""
    direct = refund.get("refund_of_tx_id")
    if direct not in (None, ""):
        try:
            value = int(direct)
            if value > 0:
                return value, set()
        except (TypeError, ValueError):
            # 非数字 direct reference 作为交易号继续处理。
            return None, {_text(direct)} if _text(direct) else set()

    raw = _raw_mapping(refund)
    explicit_id: int | None = None
    references: set[str] = set()
    for key, value in raw.items():
        key_norm = str(key).strip()
        text = _text(value)
        if not text:
            continue
        if key_norm in _REFUND_ORIGINAL_ID_KEYS:
            try:
                parsed = int(text)
                if parsed > 0:
                    explicit_id = parsed
            except ValueError:
                references.add(text)
        elif key_norm in _REFUND_ORIGINAL_REF_KEYS:
            references.add(text)
    return explicit_id, references


def _effective_product_direction(transaction: Mapping[str, Any]) -> str:
    direction = _direction(transaction)
    if direction != "中性":
        return direction
    return "收入" if "收益发放" in _text(transaction.get("item")) else "支出"


def effective_atom_value(transaction: Mapping[str, Any]) -> tuple[int, int]:
    """返回应用产品中性方向规则后的完整原子值（整数分）。"""
    return _primary_components(
        _effective_product_direction(transaction),
        _amount_cents(transaction),
    )


def _refund_candidates(
    refund: Mapping[str, Any],
    originals: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """仅依靠明确引用或平台订单标识关联，不按金额相同猜测。"""
    explicit_id, explicit_refs = _explicit_refund_reference(refund)
    refund_keys = _refund_link_keys(refund)

    candidates: list[Mapping[str, Any]] = []
    for original in originals:
        if _is_refund_transaction(original):
            continue
        if not _source_compatible(original, refund):
            continue
        if _DIRECTION_REVERSE.get(_effective_product_direction(original)) != _effective_product_direction(refund):
            continue

        original_id = _transaction_id(original)
        original_keys = _refund_link_keys(original)
        if explicit_id is not None:
            matched = original_id == explicit_id
        elif explicit_refs:
            matched = bool(explicit_refs.intersection(original_keys))
        else:
            matched = bool(refund_keys.intersection(original_keys))
        if matched:
            candidates.append(original)
    return candidates


def _refund_analysis(facts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """识别全额/部分/多笔/孤立退款，并返回每笔原交易的累计退款金额。

    证据优先级：
    1. 原交易平台字段“成功退款金额”是累计退款金额的权威值；
    2. 缺少该字段时，使用所有唯一明确关联的退款流水之和；
    3. 交易号/商家订单号必须唯一指向一笔原交易；金额相同本身不是关联证据。

    对可确认的部分退款，原交易只保留 amount-refunded 的有效金额，退款流水为零值腿。
    """
    refund_rows = [tx for tx in facts if _is_refund_transaction(tx)]
    originals = [tx for tx in facts if not _is_refund_transaction(tx)]

    refund_zero_ids: set[int] = {_transaction_id(tx) for tx in refund_rows}
    partial_refund_ids: set[int] = set()
    refund_conflict_ids: set[int] = set()
    refund_amounts_by_original: dict[int, int] = {}
    refund_pairs: list[tuple[int, int]] = []
    warnings: list[str] = []

    candidate_map: dict[int, list[Mapping[str, Any]]] = {}
    unmatched: list[tuple[Mapping[str, Any], int]] = []
    for refund in refund_rows:
        candidates = _refund_candidates(refund, originals)
        if len(candidates) != 1:
            unmatched.append((refund, len(candidates)))
            continue
        original_id = _transaction_id(candidates[0])
        candidate_map.setdefault(original_id, []).append(refund)
        refund_pairs.append((original_id, _transaction_id(refund)))

    for original in originals:
        original_id = _transaction_id(original)
        linked_refunds = candidate_map.get(original_id, [])
        raw_refund = _successful_refund_cents(original)
        original_amount = _amount_cents(original)
        linked_total = sum(_amount_cents(refund) for refund in linked_refunds)
        status_full = _has_full_refund_status(original)
        status_partial = _has_partial_refund_status(original)

        # 微信原交易可能直接标记“已全额退款”，这属于原交易状态，
        # 不是一条新的退款流水。该明确状态优先于未完整导入的退款明细。
        if status_full:
            evidenced_total = original_amount
            if raw_refund is not None and raw_refund != original_amount:
                refund_conflict_ids.add(original_id)
                warnings.append(
                    f"交易 {original_id} 状态标记全额退款，但平台退款金额字段不一致；"
                    "按全额退款归零并要求人工核对。"
                )
            if linked_refunds and linked_total != original_amount:
                refund_conflict_ids.add(original_id)
                refund_conflict_ids.update(_transaction_id(row) for row in linked_refunds)
                warnings.append(
                    f"交易 {original_id} 状态标记全额退款，但已关联退款流水合计不一致；"
                    "按全额退款归零并要求人工核对。"
                )
        elif raw_refund is None and not linked_refunds:
            if status_partial:
                refund_conflict_ids.add(original_id)
                warnings.append(
                    f"交易 {original_id} 状态标记部分退款，但缺少可核验的退款金额或关联流水；"
                    "暂不抵扣并要求人工核对。"
                )
            continue
        else:
            # 平台原始“成功退款金额”是累计金额；退款明细可能只导入部分行。
            # 若二者不一致，仍按平台累计值计算，但标记冲突并禁止该笔交易分裂。
            evidenced_total = raw_refund if raw_refund is not None else linked_total
            if raw_refund is not None and linked_refunds and linked_total != raw_refund:
                refund_conflict_ids.add(original_id)
                refund_conflict_ids.update(_transaction_id(row) for row in linked_refunds)
                warnings.append(
                    f"交易 {original_id} 的平台累计成功退款金额与已关联退款流水合计不一致；"
                    "暂按平台累计金额计算，并禁止该交易分裂，请核对退款明细。"
                )

        if evidenced_total <= 0:
            continue

        if evidenced_total > original_amount:
            refund_conflict_ids.add(original_id)
            refund_conflict_ids.update(_transaction_id(row) for row in linked_refunds)
            warnings.append(
                f"交易 {original_id} 的累计退款金额超过原交易金额，未自动抵扣，请人工核对。"
            )
            continue

        refund_amounts_by_original[original_id] = evidenced_total
        if evidenced_total == original_amount:
            refund_zero_ids.add(original_id)
        else:
            partial_refund_ids.add(original_id)
            partial_refund_ids.update(_transaction_id(row) for row in linked_refunds)
            amount_text = f"{evidenced_total / 100:.2f}"
            remaining_text = f"{(original_amount - evidenced_total) / 100:.2f}"
            warnings.append(
                f"交易 {original_id} 已确认部分退款 ¥{amount_text}，"
                f"有效金额按原金额扣减为 ¥{remaining_text}；退款流水不重复计入收入。"
            )

    for refund, candidate_count in unmatched:
        refund_id = _transaction_id(refund)
        if candidate_count > 1:
            warnings.append(
                f"退款交易 {refund_id} 对应多笔候选原交易，无法唯一关联；"
                "该退款流水按零值腿处理，未抵扣任何原交易。"
            )
        else:
            warnings.append(
                f"退款交易 {refund_id} 未找到明确关联的原始交易；"
                "该退款流水按零值腿处理，未抵扣任何原交易。"
            )

    return {
        "refund_zero_ids": refund_zero_ids,
        "partial_refund_ids": partial_refund_ids,
        "refund_conflict_ids": refund_conflict_ids,
        "refund_amounts_by_original": refund_amounts_by_original,
        "refund_pairs": sorted(set(refund_pairs)),
        "warnings": list(dict.fromkeys(warnings)),
    }

def _has_own_account_evidence(tx: Mapping[str, Any]) -> bool:
    item = _text(tx.get("item"))
    counterparty = _text(tx.get("counterparty"))
    remark = _text(tx.get("remark"))
    own_markers = ("本人账户互转", "本人账户转账", "本人账户", "自有账户互转", "自己账户互转")
    return (
        any(marker in item for marker in own_markers)
        or any(marker in remark for marker in own_markers)
        or "本人" in counterparty
    )


def _transfer_analysis(
    facts: Sequence[Mapping[str, Any]],
    excluded_ids: set[int],
) -> dict[str, Any]:
    """只把有共同订单号、等额反向且双方都有本人账户证据的转账归零。"""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for tx in facts:
        tx_id = _transaction_id(tx)
        if tx_id in excluded_ids or _is_refund_transaction(tx):
            continue
        if "转账" not in _text(tx.get("tx_type")):
            continue
        merchant_no = _text(tx.get("merchant_no"))
        if not merchant_no:
            continue
        if not _has_own_account_evidence(tx):
            continue
        source = _text(tx.get("source"))
        groups.setdefault((source, merchant_no), []).append(tx)

    transfer_zero_ids: set[int] = set()
    transfer_pairs: list[tuple[int, int]] = []
    for _key, rows in groups.items():
        candidates: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                if _DIRECTION_REVERSE.get(_effective_product_direction(left)) != _effective_product_direction(right):
                    continue
                if _amount_cents(left) != _amount_cents(right):
                    continue
                if not _source_compatible(left, right):
                    continue
                candidates.append((left, right))

        # 仅接受互相唯一的配对；同一商家订单号出现多种可能时不猜。
        used: set[int] = set()
        for left, right in candidates:
            left_id, right_id = _transaction_id(left), _transaction_id(right)
            if left_id in used or right_id in used:
                continue
            left_options = [
                pair for pair in candidates
                if left in pair or right in pair
            ]
            if len(left_options) != 1:
                continue
            used.update((left_id, right_id))
            transfer_zero_ids.update((left_id, right_id))
            transfer_pairs.append(tuple(sorted((left_id, right_id))))

    return {
        "transfer_zero_ids": transfer_zero_ids,
        "transfer_pairs": sorted(set(transfer_pairs)),
    }



def analyze_special_transactions(facts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """统一识别退款、本人账户互转及需要人工核对的退款冲突。"""
    refund = _refund_analysis(facts)
    excluded_from_transfer = (
        refund["refund_zero_ids"]
        | refund["partial_refund_ids"]
        | refund["refund_conflict_ids"]
    )
    transfer = _transfer_analysis(facts, excluded_from_transfer)
    return {
        **refund,
        **transfer,
        "warnings": list(dict.fromkeys(refund["warnings"])),
    }


def atom_value(transaction: Mapping[str, Any]) -> tuple[int, int]:
    """返回平台原始方向对应的原子值 (支出分, 收入分)。

    注意：中性在纯代数层为 (0, 0)。产品层对中性交易的默认方向
    在 generate_legs 中单独应用，因此审计可以计算出中性定向带来的变化。
    """
    amount_cents = _amount_cents(transaction)
    direction = _direction(transaction)
    return _primary_components(direction, amount_cents)


def _engine_zero_leg(tx_id: int) -> Leg:
    """引擎内部的零值主腿，保持 ledger role 契约为 primary/mirror。"""
    return Leg(
        tx_id=tx_id,
        leg_index=1,
        role=ROLE_PRIMARY,
        e_cents=0,
        i_cents=0,
        destination=DESTINATION_TRANSACTION,
    )


def generate_legs(
    state: Mapping[str, Any],
    facts: Sequence[Mapping[str, Any]],
    book_id: str,
) -> dict[str, Any]:
    """按账本状态生成完整腿集合，并返回审计元数据。

    每笔成员交易恰好生成一条 primary；只有 state.splits 中显式登记的交易
    才额外生成 mirror。原始交易和数据库均不会被修改。
    """
    placements = state.get("placements") or {}
    members = {int(value) for value in (state.get("members") or [])}
    splits = state.get("splits") or {}
    facts_by_id = {_transaction_id(tx): tx for tx in facts}

    if set(facts_by_id) != members:
        raise ValueError("facts 必须与账本 members 一一对应。")

    special = analyze_special_transactions(facts)
    # 实时星图先在完整交易集上识别退款/内部转账，再把结果带入日期筛选视图。
    # 若筛选后只剩转账的一侧，仍必须保留它在完整账本中的零值判定。
    if "_refund_zero_ids" in state:
        selected_ids = members
        special["refund_zero_ids"] = set(state.get("_refund_zero_ids") or []) & selected_ids
        special["transfer_zero_ids"] = set(state.get("_transfer_zero_ids") or []) & selected_ids
        special["partial_refund_ids"] = set(state.get("_partial_refund_ids") or []) & selected_ids
        special["refund_conflict_ids"] = set(state.get("_refund_conflict_ids") or []) & selected_ids
        special["refund_amounts_by_original"] = {
            int(key): int(value)
            for key, value in (state.get("_refund_amounts_by_original") or {}).items()
            if int(key) in selected_ids
        }
        special["refund_pairs"] = [
            tuple(pair) for pair in (state.get("_refund_pairs") or [])
            if len(pair) == 2 and bool(set(map(int, pair)) & selected_ids)
        ]
        special["transfer_pairs"] = [
            tuple(pair) for pair in (state.get("_transfer_pairs") or [])
            if len(pair) == 2 and bool(set(map(int, pair)) & selected_ids)
        ]
        special["warnings"] = list(state.get("_special_warnings") or [])

    refund_zero_ids = set(special["refund_zero_ids"])
    transfer_zero_ids = set(special["transfer_zero_ids"])
    partial_refund_ids = set(special["partial_refund_ids"])
    refund_conflict_ids = set(special["refund_conflict_ids"])
    refund_amounts_by_original = {
        int(key): int(value)
        for key, value in special["refund_amounts_by_original"].items()
    }

    split_ids = {int(tx_id) for tx_id in splits}
    # 合法部分退款先扣减退款金额，再对剩余金额应用外部额度分裂。
    # 全额退款、内部转账和退款证据冲突才禁止分裂。
    blocked_split_ids = refund_zero_ids | transfer_zero_ids | refund_conflict_ids
    invalid_splits = split_ids & blocked_split_ids
    if invalid_splits:
        raise ValueError(
            f"全额退款、内部转账或退款证据冲突的交易不能分裂：{sorted(invalid_splits)}"
        )

    external_nodes = [
        node for node in state.get("nodes", [])
        if node.get("kind") == "external_income"
    ]
    if split_ids and len(external_nodes) != 1:
        raise ValueError("存在外部额度分裂时必须且只能有一个外部收入节点。")
    external_node_id = external_nodes[0]["id"] if external_nodes else None

    legs: list[dict[str, Any]] = []
    for tx_id in sorted(members, key=lambda value: (
        _text(facts_by_id[value].get("tx_time")), value
    )):
        tx = facts_by_id[tx_id]
        node_id = placements.get(str(tx_id))
        if node_id is None:
            raise ValueError(f"交易 {tx_id} 缺少主腿节点归属。")

        if tx_id in refund_zero_ids or tx_id in transfer_zero_ids:
            generated = (_engine_zero_leg(tx_id),)
        else:
            effective_tx = dict(tx)
            effective_tx["direction"] = _effective_product_direction(tx)
            refunded_cents = refund_amounts_by_original.get(tx_id, 0)
            original_cents = _amount_cents(tx)
            remaining_cents = original_cents - refunded_cents
            if remaining_cents < 0:
                raise ValueError(f"交易 {tx_id} 的退款金额超过原交易金额。")
            if remaining_cents == 0:
                generated = (_engine_zero_leg(tx_id),)
            else:
                effective_tx["amount"] = Decimal(remaining_cents) / Decimal(100)
                split_meta = splits.get(str(tx_id))
                funding = OWN_FUNDING
                if split_meta is not None:
                    funding = (
                        str(split_meta.get("funding") or "")
                        if isinstance(split_meta, Mapping)
                        else str(split_meta)
                    )
                    actual_funding = _text(tx.get("funding")) or OWN_FUNDING
                    if actual_funding not in EXTERNAL_FUNDING or funding != actual_funding:
                        raise ValueError(
                            f"交易 {tx_id} 的分裂额度与当前交易资金来源不一致。"
                        )
                generated = build_legs(effective_tx, funding=funding)

        for leg in generated:
            if leg.role == ROLE_MIRROR:
                destination_node_id = external_node_id
                if destination_node_id is None:
                    raise ValueError("镜像腿缺少外部收入节点。")
            else:
                destination_node_id = node_id
            role = ROLE_PRIMARY if leg.role == ROLE_REFUND_ZERO else leg.role
            legs.append({
                "leg_id": f"{tx_id}:{leg.leg_index}",
                "tx_id": tx_id,
                "leg_index": leg.leg_index,
                "role": role,
                "e": int(leg.e_cents),
                "i": int(leg.i_cents),
                "node_id": destination_node_id,
                "destination": leg.destination,
            })

    return {
        "legs": legs,
        "warnings": special["warnings"],
        "refund_zero_ids": refund_zero_ids,
        "transfer_zero_ids": transfer_zero_ids,
        "partial_refund_ids": partial_refund_ids,
        "refund_conflict_ids": refund_conflict_ids,
        "refund_amounts_by_original": refund_amounts_by_original,
        "refund_pairs": special["refund_pairs"],
        "transfer_pairs": special["transfer_pairs"],
    }


def _validate_refund_pair(
    original: Mapping[str, Any],
    refund: Mapping[str, Any],
) -> tuple[int, int]:
    """验证显式退款关联，返回 (original_id, refund_id)。"""
    original_id = _transaction_id(original)
    refund_id = _transaction_id(refund)
    if original_id == refund_id:
        raise ValueError("原交易和退款交易不能是同一笔交易。")

    original_direction = _direction(original)
    refund_direction = _direction(refund)
    if _DIRECTION_REVERSE.get(original_direction) != refund_direction:
        raise ValueError("退款必须与原交易方向相反，且只能是支出/收入配对。")

    original_amount = _amount_cents(original)
    refund_amount = _amount_cents(refund)
    if refund_amount > original_amount:
        raise ValueError("退款金额不能超过原交易金额。")

    if not _source_compatible(original, refund):
        raise ValueError("原交易与退款必须来自同一平台来源。")

    linked_original_id = refund.get("refund_of_tx_id")
    if isinstance(linked_original_id, bool) or linked_original_id != original_id:
        raise ValueError("退款必须通过 refund_of_tx_id 明确指向原交易。")

    return original_id, refund_id


def build_refund_pair_legs(
    original: Mapping[str, Any],
    refund: Mapping[str, Any],
) -> tuple[Leg, Leg]:
    """为明确关联的退款生成“原交易剩余腿 + 退款零值腿”。"""
    original_id, refund_id = _validate_refund_pair(original, refund)
    remaining_cents = _amount_cents(original) - _amount_cents(refund)
    if remaining_cents == 0:
        original_leg = _zero_leg(original_id)
    else:
        e, i = _primary_components(_direction(original), remaining_cents)
        original_leg = Leg(
            tx_id=original_id,
            leg_index=1,
            role=ROLE_PRIMARY,
            e_cents=e,
            i_cents=i,
            destination=DESTINATION_TRANSACTION,
        )
    return original_leg, _zero_leg(refund_id)


def build_legs(
    transaction: Mapping[str, Any],
    funding: str = OWN_FUNDING,
    *,
    refund_pair: tuple[Mapping[str, Any], Mapping[str, Any]] | None = None,
) -> tuple[Leg, ...]:
    """将一笔原始交易转换为纯代数记账腿。

    普通交易/自有资金：1 条腿。
    外部额度：2 条对称腿。
    明确关联的退款：原交易保留扣减退款后的剩余腿，退款流水生成零值腿。
    中性交易在本底层函数中保持零值，不应用产品层默认支出规则。
    """
    tx_id = _transaction_id(transaction)
    direction = _direction(transaction)
    amount_cents = _amount_cents(transaction)

    if funding not in SUPPORTED_FUNDING:
        raise ValueError(
            f"不支持的 funding：{funding!r}。允许值为：自有、亲情卡、他人代付。"
        )

    if refund_pair is not None:
        if not isinstance(refund_pair, tuple) or len(refund_pair) != 2:
            raise ValueError("refund_pair 必须是 (original, refund) 二元组。")
        original, refund = refund_pair
        pair_legs = build_refund_pair_legs(original, refund)
        pair_ids = {pair_legs[0].tx_id, pair_legs[1].tx_id}
        if tx_id not in pair_ids:
            raise ValueError("当前交易不属于传入的退款配对。")
        paired_tx = original if tx_id == pair_legs[0].tx_id else refund
        if (
            _direction(paired_tx) != direction
            or _amount_cents(paired_tx) != amount_cents
            or not _source_compatible(paired_tx, transaction)
        ):
            raise ValueError("当前交易与退款配对中的交易事实不一致。")
        if tx_id == pair_legs[1].tx_id:
            return (pair_legs[1],)

        remaining_cents = _amount_cents(original) - _amount_cents(refund)
        if remaining_cents == 0:
            return (pair_legs[0],)
        effective_original = dict(original)
        effective_original["amount"] = Decimal(remaining_cents) / Decimal(100)
        # 先抵扣部分退款，再按剩余金额判断外部额度分裂。
        return build_legs(effective_original, funding=funding)

    primary_e, primary_i = _primary_components(direction, amount_cents)
    primary = Leg(
        tx_id=tx_id,
        leg_index=1,
        role=ROLE_PRIMARY,
        e_cents=primary_e,
        i_cents=primary_i,
        destination=DESTINATION_TRANSACTION,
    )
    if funding == OWN_FUNDING:
        return (primary,)

    mirror = Leg(
        tx_id=tx_id,
        leg_index=2,
        role=ROLE_MIRROR,
        e_cents=primary_i,
        i_cents=primary_e,
        destination=DESTINATION_EXTERNAL_INCOME,
    )
    return primary, mirror
