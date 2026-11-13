"""第二轮记账代数：原子交易转换专项测试。"""
from __future__ import annotations

import pytest

from backend.ledger.atoms import (
    DESTINATION_EXTERNAL_INCOME,
    DESTINATION_TRANSACTION,
    ROLE_MIRROR,
    ROLE_PRIMARY,
    ROLE_REFUND_ZERO,
    build_legs,
    build_refund_pair_legs,
)


def tx(
    tx_id: int = 1,
    direction: str = "支出",
    amount: str = "100.00",
    source: str = "支付宝",
    **extra,
) -> dict:
    return {
        "id": tx_id,
        "direction": direction,
        "amount": amount,
        "source": source,
        **extra,
    }


@pytest.mark.parametrize(
    ("direction", "expected_e", "expected_i"),
    [
        ("支出", 10000, 0),
        ("收入", 0, 10000),
        ("中性", 0, 0),
    ],
)
def test_atom_direction_mapping(direction, expected_e, expected_i):
    legs = build_legs(tx(direction=direction))
    assert len(legs) == 1
    leg = legs[0]
    assert (leg.e_cents, leg.i_cents) == (expected_e, expected_i)
    assert leg.role == ROLE_PRIMARY
    assert leg.destination == DESTINATION_TRANSACTION


@pytest.mark.parametrize(
    ("amount", "expected_cents"),
    [
        ("0.01", 1),
        ("0.10", 10),
        ("0.29", 29),
        ("12.34", 1234),
        ("99999.99", 9999999),
    ],
)
def test_amount_is_stored_as_exact_integer_cents(amount, expected_cents):
    leg = build_legs(tx(amount=amount))[0]
    assert leg.e_cents == expected_cents
    assert leg.i_cents == 0


def test_own_funding_never_splits():
    legs = build_legs(tx(), funding="自有")
    assert len(legs) == 1
    assert legs[0].role == ROLE_PRIMARY


@pytest.mark.parametrize("funding", ["亲情卡", "他人代付"])
def test_external_funding_creates_symmetric_mirror(funding):
    primary, mirror = build_legs(tx(), funding=funding)

    assert primary.role == ROLE_PRIMARY
    assert mirror.role == ROLE_MIRROR
    assert primary.destination == DESTINATION_TRANSACTION
    assert mirror.destination == DESTINATION_EXTERNAL_INCOME

    # 分裂恒等式：两条腿相加后，支出和收入分量均为原金额。
    assert primary.e_cents + mirror.e_cents == 10000
    assert primary.i_cents + mirror.i_cents == 10000
    assert primary.e_cents == mirror.i_cents
    assert primary.i_cents == mirror.e_cents


def test_external_funding_income_mirrors_to_expense():
    primary, mirror = build_legs(
        tx(direction="收入", amount="25.50"),
        funding="他人代付",
    )
    assert (primary.e_cents, primary.i_cents) == (0, 2550)
    assert (mirror.e_cents, mirror.i_cents) == (2550, 0)
    assert mirror.destination == DESTINATION_EXTERNAL_INCOME


def test_neutral_external_transaction_produces_two_zero_legs():
    primary, mirror = build_legs(tx(direction="中性"), funding="亲情卡")
    assert (primary.e_cents, primary.i_cents) == (0, 0)
    assert (mirror.e_cents, mirror.i_cents) == (0, 0)
    assert mirror.destination == DESTINATION_EXTERNAL_INCOME


def test_unknown_funding_fails_closed():
    with pytest.raises(ValueError, match="不支持的 funding"):
        build_legs(tx(), funding="某种未知额度")


def make_refund_pair():
    original = tx(tx_id=101, direction="支出", amount="88.80")
    refund = tx(
        tx_id=202,
        direction="收入",
        amount="88.80",
        refund_of_tx_id=101,
    )
    return original, refund


def test_full_refund_pair_generates_two_zero_legs():
    original, refund = make_refund_pair()
    original_leg, refund_leg = build_refund_pair_legs(original, refund)

    assert original_leg.tx_id == 101
    assert refund_leg.tx_id == 202
    for leg in (original_leg, refund_leg):
        assert (leg.e_cents, leg.i_cents) == (0, 0)
        assert leg.role == ROLE_REFUND_ZERO
        assert leg.destination == DESTINATION_TRANSACTION


def test_refund_pair_takes_priority_over_external_funding_split():
    original, refund = make_refund_pair()

    original_legs = build_legs(
        original,
        funding="亲情卡",
        refund_pair=(original, refund),
    )
    refund_legs = build_legs(
        refund,
        funding="亲情卡",
        refund_pair=(original, refund),
    )

    assert len(original_legs) == len(refund_legs) == 1
    assert original_legs[0].role == ROLE_REFUND_ZERO
    assert refund_legs[0].role == ROLE_REFUND_ZERO
    assert original_legs[0].i_cents == refund_legs[0].i_cents == 0


def test_refund_pair_requires_explicit_original_link():
    original, refund = make_refund_pair()
    refund.pop("refund_of_tx_id")
    with pytest.raises(ValueError, match="明确指向原交易"):
        build_refund_pair_legs(original, refund)


def test_refund_pair_rejects_over_refund():
    original, refund = make_refund_pair()
    refund["amount"] = "88.81"
    with pytest.raises(ValueError, match="不能超过原交易金额"):
        build_refund_pair_legs(original, refund)


def test_partial_refund_pair_keeps_only_unrefunded_original_amount():
    original = tx(tx_id=101, direction="支出", amount="100.00")
    refund = tx(
        tx_id=202,
        direction="收入",
        amount="30.00",
        refund_of_tx_id=101,
    )

    original_leg, refund_leg = build_refund_pair_legs(original, refund)

    assert (original_leg.e_cents, original_leg.i_cents) == (7000, 0)
    assert original_leg.role == ROLE_PRIMARY
    assert (refund_leg.e_cents, refund_leg.i_cents) == (0, 0)
    assert refund_leg.role == ROLE_REFUND_ZERO

    # Refund is applied before splitting: the residual 70 is split, not the original 100.
    original_legs = build_legs(
        original, funding="亲情卡", refund_pair=(original, refund)
    )
    refund_legs = build_legs(
        refund, funding="亲情卡", refund_pair=(original, refund)
    )
    assert len(original_legs) == 2
    assert (original_legs[0].e_cents, original_legs[0].i_cents) == (7000, 0)
    assert (original_legs[1].e_cents, original_legs[1].i_cents) == (0, 7000)
    assert len(refund_legs) == 1
    assert refund_legs[0].e_cents == refund_legs[0].i_cents == 0


def test_refund_pair_rejects_different_sources():
    original, refund = make_refund_pair()
    refund["source"] = "微信支付"
    with pytest.raises(ValueError, match="同一平台来源"):
        build_refund_pair_legs(original, refund)


def test_refund_pair_rejects_same_direction():
    original, refund = make_refund_pair()
    refund["direction"] = "支出"
    with pytest.raises(ValueError, match="方向相反"):
        build_refund_pair_legs(original, refund)


def test_refund_pair_rejects_wrong_link():
    original, refund = make_refund_pair()
    refund["refund_of_tx_id"] = 999
    with pytest.raises(ValueError, match="明确指向原交易"):
        build_refund_pair_legs(original, refund)


def test_refund_pair_rejects_neutral_direction():
    original = tx(tx_id=101, direction="中性")
    refund = tx(
        tx_id=202,
        direction="收入",
        refund_of_tx_id=101,
    )
    with pytest.raises(ValueError, match="方向相反"):
        build_refund_pair_legs(original, refund)


def test_current_transaction_must_belong_to_refund_pair():
    original, refund = make_refund_pair()
    with pytest.raises(ValueError, match="不属于传入的退款配对"):
        build_legs(
            tx(tx_id=303),
            funding="自有",
            refund_pair=(original, refund),
        )


@pytest.mark.parametrize(
    ("bad_tx", "message"),
    [
        (tx(tx_id=0), "id 必须是正整数"),
        (tx(tx_id=True), "id 必须是正整数"),
        (tx(amount="0"), "amount 必须大于 0"),
        (tx(amount="-1"), "amount 必须大于 0"),
        (tx(direction="未知"), "direction 只能是"),
    ],
)
def test_invalid_transaction_is_rejected(bad_tx, message):
    with pytest.raises(ValueError, match=message):
        build_legs(bad_tx)



def test_wechat_full_refund_status_is_applied_to_original_not_misread_as_refund_row():
    from backend.ledger.atoms import analyze_special_transactions, is_refund_transaction

    original = {
        "id": 501, "source": "微信支付", "direction": "支出", "amount": 7.00,
        "tx_type": "转账", "status": "已全额退款",
        "tx_no": "mock-original-501", "merchant_no": "mock-merchant-501",
        "counterparty": "模拟联系人", "item": "转账备注:模拟",
        "raw_json": "{}",
    }
    refund = {
        "id": 502, "source": "微信支付", "direction": "收入", "amount": 7.00,
        "tx_type": "转账-退款", "status": "已全额退款",
        "tx_no": "mock-refund-502", "merchant_no": "",
        "counterparty": "", "item": "转账备注:模拟",
        "raw_json": "{}",
    }

    assert is_refund_transaction(original) is False
    assert is_refund_transaction(refund) is True
    result = analyze_special_transactions([original, refund])
    assert result["refund_zero_ids"] == {501, 502}
    assert result["refund_amounts_by_original"][501] == 700
