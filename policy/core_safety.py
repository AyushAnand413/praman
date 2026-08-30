from __future__ import annotations

INVARIANTS: dict[int, str] = {
    1: "No LLM output can directly cause money movement",
    2: "Every payment must reference an approved deterministic policy verdict",
    3: "The amount executed must exactly equal the amount approved",
    4: "The cart executed must exactly equal the cart that was authorized",
    5: "Inventory must be reserved or revalidated immediately before commit",
    6: "Every state-changing operation must be idempotent",
    7: "Every completed transaction must produce a reconstructable decision record",
    8: "Every payment execution must reference exactly one immutable approved TDR and the payment amount and cart must match that TDR",
}

class InvariantViolation(RuntimeError):
    """Exception raised when a core system invariant is violated."""
    
    def __init__(self, invariant_number: int, detail: str) -> None:
        self.invariant_number = invariant_number
        self.detail = detail
        super().__init__(f"Invariant {invariant_number} Violated: {INVARIANTS.get(invariant_number, 'Unknown')} - {detail}")


def check_invariant_1(decision_source: str) -> None:
    """Check that LLM output does not directly cause money movement."""
    if decision_source.lower() in ('llm', 'generative'):
        raise InvariantViolation(1, f"Decision source cannot be '{decision_source}'.")

def check_invariant_2(policy_verdict_id: str | None) -> None:
    """Check that every payment references an approved deterministic policy verdict."""
    if not policy_verdict_id:
        raise InvariantViolation(2, "Missing policy verdict ID.")

def check_invariant_3(approved_amount: int, executed_amount: int) -> None:
    """Check that executed amount exactly equals the approved amount."""
    if approved_amount != executed_amount:
        raise InvariantViolation(3, f"Approved amount {approved_amount} does not match executed amount {executed_amount}.")

def check_invariant_4(approved_cart_hash: str, executed_cart_hash: str) -> None:
    """Check that executed cart exactly equals the authorized cart."""
    if approved_cart_hash != executed_cart_hash:
        raise InvariantViolation(4, f"Approved cart hash {approved_cart_hash} does not match executed cart hash {executed_cart_hash}.")

def check_invariant_5(reservation_status: str) -> None:
    """Check that inventory is reserved or revalidated before commit."""
    if reservation_status.upper() not in ('HELD', 'COMMITTED'):
        raise InvariantViolation(5, f"Invalid reservation status: {reservation_status}.")

def check_invariant_6(idempotency_key: str | None) -> None:
    """Check that state-changing operations are idempotent."""
    if not idempotency_key:
        raise InvariantViolation(6, "Missing idempotency key.")

def check_invariant_7(tdr_id: str | None) -> None:
    """Check that a completed transaction produces a reconstructable decision record."""
    if not tdr_id:
        raise InvariantViolation(7, "Missing TDR ID.")

def check_invariant_8(tdr_id: str | None, tdr_amount: int | None, payment_amount: int, tdr_cart_hash: str | None, payment_cart_hash: str) -> None:
    """Check that payment execution matches exactly one immutable approved TDR."""
    if not tdr_id:
        raise InvariantViolation(8, "Missing TDR ID.")
    if tdr_amount is None or tdr_amount != payment_amount:
        raise InvariantViolation(8, f"TDR amount {tdr_amount} does not match payment amount {payment_amount}.")
    if tdr_cart_hash is None or tdr_cart_hash != payment_cart_hash:
        raise InvariantViolation(8, f"TDR cart hash {tdr_cart_hash} does not match payment cart hash {payment_cart_hash}.")


def check_all_pre_payment(
    decision_source: str,
    policy_verdict_id: str | None,
    approved_amount: int,
    executed_amount: int,
    approved_cart_hash: str,
    executed_cart_hash: str,
    reservation_status: str,
    idempotency_key: str | None
) -> None:
    """Run invariants 1-6 as a batch before payment execution."""
    check_invariant_1(decision_source)
    check_invariant_2(policy_verdict_id)
    check_invariant_3(approved_amount, executed_amount)
    check_invariant_4(approved_cart_hash, executed_cart_hash)
    check_invariant_5(reservation_status)
    check_invariant_6(idempotency_key)


def check_all_post_payment(
    tdr_id: str | None,
    tdr_amount: int | None,
    payment_amount: int,
    tdr_cart_hash: str | None,
    payment_cart_hash: str
) -> None:
    """Run invariants 7-8 as a batch after payment execution."""
    check_invariant_7(tdr_id)
    check_invariant_8(tdr_id, tdr_amount, payment_amount, tdr_cart_hash, payment_cart_hash)
