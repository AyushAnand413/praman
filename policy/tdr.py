from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from store.canonical import canonical_json
from store.ids import new_id
from store.timestamps import now_ts


@dataclass(frozen=True)
class BuyerAuthority:
    """Records the authority and limits granted by the buyer."""
    max_amount_inr: int
    authority_type: str  # 'buyer_mandate' | 'auto' | 'merchant_approved'
    authority_ref: str | None


@dataclass(frozen=True)
class CartItem:
    """Represents a single item in the cart."""
    sku: str
    quantity: int
    unit_price_inr: int


@dataclass(frozen=True)
class CartSnapshot:
    """A snapshot of the cart at the time of the decision."""
    items: tuple[CartItem, ...]
    total_inr: int

    def compute_hash(self) -> str:
        """Compute the hash of the cart snapshot."""
        data = {
            "items": [asdict(item) for item in self.items],
            "total_inr": self.total_inr
        }
        return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EconomicDecision:
    """The economic decision made for the transaction."""
    selected_price_inr: int
    expected_contribution_inr: int
    score: Decimal | None
    score_breakdown: dict[str, Any] | None


@dataclass(frozen=True)
class ReservationRef:
    """Reference to the inventory reservation."""
    reservation_id: str
    status: str


@dataclass(frozen=True)
class PaymentRef:
    """Reference to the payment details."""
    provider: str = 'razorpay'
    order_id: str | None = None
    payment_id: str | None = None


@dataclass(frozen=True)
class PolicyReference:
    """Reference to the policy used for the decision."""
    policy_id: str
    version: int
    hash: str


@dataclass(frozen=True)
class TransactionDecisionRecord:
    """
    The Transaction Decision Record (TDR).
    The canonical record binding intent + cart + policy + economics + reservation + payment.
    """
    transaction_id: str
    intent_id: str
    buyer_authority: BuyerAuthority
    policy: PolicyReference
    cart: CartSnapshot
    economic_decision: EconomicDecision
    reservation: ReservationRef
    payment: PaymentRef
    outcome: str
    tdr_hash: str
    created_at: str


def _compute_tdr_hash(tdr_without_hash: dict[str, Any]) -> str:
    """Compute the SHA-256 hash of the canonical JSON of the TDR fields, excluding tdr_hash."""
    data_to_hash = dict(tdr_without_hash)
    if "tdr_hash" in data_to_hash:
        del data_to_hash["tdr_hash"]
    
    econ = data_to_hash.get("economic_decision", {})
    if isinstance(econ.get("score"), Decimal):
        econ["score"] = str(econ["score"])
        
    return hashlib.sha256(canonical_json(data_to_hash).encode("utf-8")).hexdigest()


def build_tdr(
    intent_id: str,
    buyer_authority: BuyerAuthority,
    policy: PolicyReference,
    cart: CartSnapshot,
    economic_decision: EconomicDecision,
    reservation: ReservationRef,
    payment: PaymentRef,
    outcome: str,
) -> TransactionDecisionRecord:
    """Construct a Transaction Decision Record and compute its hash."""
    transaction_id = f"TDR-{new_id('TDR')}"
    created_at = now_ts()
    
    temp_tdr_data = {
        "transaction_id": transaction_id,
        "intent_id": intent_id,
        "buyer_authority": asdict(buyer_authority),
        "policy": asdict(policy),
        "cart": {
            "items": [asdict(item) for item in cart.items],
            "total_inr": cart.total_inr
        },
        "economic_decision": asdict(economic_decision),
        "reservation": asdict(reservation),
        "payment": asdict(payment),
        "outcome": outcome,
        "created_at": created_at,
    }
    
    tdr_hash = _compute_tdr_hash(temp_tdr_data)
    
    return TransactionDecisionRecord(
        transaction_id=transaction_id,
        intent_id=intent_id,
        buyer_authority=buyer_authority,
        policy=policy,
        cart=cart,
        economic_decision=economic_decision,
        reservation=reservation,
        payment=payment,
        outcome=outcome,
        tdr_hash=tdr_hash,
        created_at=created_at,
    )


def verify_tdr(tdr: TransactionDecisionRecord) -> bool:
    """Verify that the TDR hash matches its contents."""
    temp_tdr_data = {
        "transaction_id": tdr.transaction_id,
        "intent_id": tdr.intent_id,
        "buyer_authority": asdict(tdr.buyer_authority),
        "policy": asdict(tdr.policy),
        "cart": {
            "items": [asdict(item) for item in tdr.cart.items],
            "total_inr": tdr.cart.total_inr
        },
        "economic_decision": asdict(tdr.economic_decision),
        "reservation": asdict(tdr.reservation),
        "payment": asdict(tdr.payment),
        "outcome": tdr.outcome,
        "created_at": tdr.created_at,
    }
    
    expected_hash = _compute_tdr_hash(temp_tdr_data)
    return expected_hash == tdr.tdr_hash
