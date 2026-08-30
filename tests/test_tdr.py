from __future__ import annotations

import pytest
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

from policy.tdr import (
    BuyerAuthority,
    CartItem,
    CartSnapshot,
    EconomicDecision,
    PaymentRef,
    PolicyReference,
    ReservationRef,
    TransactionDecisionRecord,
    build_tdr,
    verify_tdr,
)


def test_build_tdr_valid_hash():
    buyer = BuyerAuthority(max_amount_inr=5000, authority_type="auto", authority_ref=None)
    policy = PolicyReference(policy_id="pol-1", version=1, hash="phash")
    cart = CartSnapshot(items=(CartItem(sku="sku-1", quantity=1, unit_price_inr=1000),), total_inr=1000)
    econ = EconomicDecision(selected_price_inr=950, expected_contribution_inr=200, score=Decimal("0.85"), score_breakdown=None)
    res = ReservationRef(reservation_id="res-1", status="HELD")
    pay = PaymentRef(provider="razorpay", order_id="ord-1", payment_id=None)
    
    tdr = build_tdr(
        intent_id="intent-1",
        buyer_authority=buyer,
        policy=policy,
        cart=cart,
        economic_decision=econ,
        reservation=res,
        payment=pay,
        outcome="APPROVED",
    )
    
    assert tdr.transaction_id.startswith("TDR-")
    assert tdr.tdr_hash is not None
    assert verify_tdr(tdr) is True


def test_verify_tdr_tampered():
    buyer = BuyerAuthority(max_amount_inr=5000, authority_type="auto", authority_ref=None)
    policy = PolicyReference(policy_id="pol-1", version=1, hash="phash")
    cart = CartSnapshot(items=(CartItem(sku="sku-1", quantity=1, unit_price_inr=1000),), total_inr=1000)
    econ = EconomicDecision(selected_price_inr=950, expected_contribution_inr=200, score=Decimal("0.85"), score_breakdown=None)
    res = ReservationRef(reservation_id="res-1", status="HELD")
    pay = PaymentRef(provider="razorpay", order_id="ord-1", payment_id=None)
    
    tdr = build_tdr(
        intent_id="intent-1",
        buyer_authority=buyer,
        policy=policy,
        cart=cart,
        economic_decision=econ,
        reservation=res,
        payment=pay,
        outcome="APPROVED",
    )
    
    # Tamper with the outcome
    tampered_tdr = replace(tdr, outcome="REJECTED")
    assert verify_tdr(tampered_tdr) is False
    
    # Tamper with economics
    tampered_econ = replace(econ, selected_price_inr=900)
    tampered_tdr2 = replace(tdr, economic_decision=tampered_econ)
    assert verify_tdr(tampered_tdr2) is False


def test_tdr_is_frozen():
    buyer = BuyerAuthority(max_amount_inr=5000, authority_type="auto", authority_ref=None)
    policy = PolicyReference(policy_id="pol-1", version=1, hash="phash")
    cart = CartSnapshot(items=(CartItem(sku="sku-1", quantity=1, unit_price_inr=1000),), total_inr=1000)
    econ = EconomicDecision(selected_price_inr=950, expected_contribution_inr=200, score=Decimal("0.85"), score_breakdown=None)
    res = ReservationRef(reservation_id="res-1", status="HELD")
    pay = PaymentRef(provider="razorpay", order_id="ord-1", payment_id=None)
    
    tdr = build_tdr(
        intent_id="intent-1",
        buyer_authority=buyer,
        policy=policy,
        cart=cart,
        economic_decision=econ,
        reservation=res,
        payment=pay,
        outcome="APPROVED",
    )
    
    with pytest.raises(FrozenInstanceError):
        tdr.outcome = "REJECTED"


def test_cart_snapshot_compute_hash():
    cart1 = CartSnapshot(items=(CartItem(sku="sku-1", quantity=1, unit_price_inr=1000),), total_inr=1000)
    cart2 = CartSnapshot(items=(CartItem(sku="sku-1", quantity=1, unit_price_inr=1000),), total_inr=1000)
    cart3 = CartSnapshot(items=(CartItem(sku="sku-2", quantity=1, unit_price_inr=1000),), total_inr=1000)
    
    hash1 = cart1.compute_hash()
    hash2 = cart2.compute_hash()
    hash3 = cart3.compute_hash()
    
    assert hash1 == hash2
    assert hash1 != hash3
