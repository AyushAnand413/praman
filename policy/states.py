"""Transaction state machine for autonomous commerce.

This module explicitly models every phase of a transaction lifecycle.
In an autonomous negotiation system, explicit states matter immensely
for idempotency, recovery, and preventing out-of-order operations.
"""

from __future__ import annotations

from enum import Enum


class TransactionState(Enum):
    """All possible states for a transaction."""
    # Happy path
    INTENT_CREATED = "INTENT_CREATED"
    PROPOSING = "PROPOSING"
    NEGOTIATING = "NEGOTIATING"
    DEAL_PROPOSED = "DEAL_PROPOSED"
    APPROVED = "APPROVED"
    RESERVATION_HELD = "RESERVATION_HELD"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAYMENT_CAPTURED = "PAYMENT_CAPTURED"
    COMMITTED = "COMMITTED"
    FULFILLED = "FULFILLED"

    # Failure paths
    EXPIRED = "EXPIRED"
    RESERVATION_EXPIRED = "RESERVATION_EXPIRED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    INVENTORY_COMMIT_FAILED = "INVENTORY_COMMIT_FAILED"
    COMPENSATING = "COMPENSATING"
    REFUNDED = "REFUNDED"
    NO_FEASIBLE_DEAL = "NO_FEASIBLE_DEAL"


VALID_TRANSITIONS: dict[TransactionState, frozenset[TransactionState]] = {
    TransactionState.INTENT_CREATED: frozenset({
        TransactionState.PROPOSING,
        TransactionState.EXPIRED,
    }),
    TransactionState.PROPOSING: frozenset({
        TransactionState.NEGOTIATING,
        TransactionState.DEAL_PROPOSED,
        TransactionState.NO_FEASIBLE_DEAL,
        TransactionState.EXPIRED,
    }),
    TransactionState.NEGOTIATING: frozenset({
        TransactionState.DEAL_PROPOSED,
        TransactionState.NO_FEASIBLE_DEAL,
        TransactionState.EXPIRED,
    }),
    TransactionState.DEAL_PROPOSED: frozenset({
        TransactionState.APPROVED,
        TransactionState.EXPIRED,
        TransactionState.NEGOTIATING, # Re-negotiation
    }),
    TransactionState.APPROVED: frozenset({
        TransactionState.RESERVATION_HELD,
        TransactionState.EXPIRED,
    }),
    TransactionState.RESERVATION_HELD: frozenset({
        TransactionState.PAYMENT_PENDING,
        TransactionState.RESERVATION_EXPIRED,
    }),
    TransactionState.PAYMENT_PENDING: frozenset({
        TransactionState.PAYMENT_CAPTURED,
        TransactionState.PAYMENT_FAILED,
        TransactionState.RESERVATION_EXPIRED,
    }),
    TransactionState.PAYMENT_CAPTURED: frozenset({
        TransactionState.COMMITTED,
        TransactionState.INVENTORY_COMMIT_FAILED,
    }),
    TransactionState.COMMITTED: frozenset({
        TransactionState.FULFILLED,
        TransactionState.COMPENSATING, # If something goes wrong post-commit
    }),
    TransactionState.INVENTORY_COMMIT_FAILED: frozenset({
        TransactionState.COMPENSATING,
    }),
    TransactionState.COMPENSATING: frozenset({
        TransactionState.REFUNDED,
    }),
    # Terminal states have no outbound transitions
    TransactionState.FULFILLED: frozenset(),
    TransactionState.REFUNDED: frozenset(),
    TransactionState.NO_FEASIBLE_DEAL: frozenset(),
    TransactionState.EXPIRED: frozenset(),
    TransactionState.RESERVATION_EXPIRED: frozenset(),
    TransactionState.PAYMENT_FAILED: frozenset(),
}

TERMINAL_STATES: frozenset[TransactionState] = frozenset({
    TransactionState.FULFILLED,
    TransactionState.REFUNDED,
    TransactionState.NO_FEASIBLE_DEAL,
    TransactionState.EXPIRED,
    TransactionState.RESERVATION_EXPIRED,
    TransactionState.PAYMENT_FAILED,
})


class InvalidTransition(Exception):
    """Exception raised when an invalid state transition is attempted."""
    pass


def transition(current: TransactionState, target: TransactionState) -> TransactionState:
    """Transitions a transaction from one state to another.

    Validates that the transition is allowed according to VALID_TRANSITIONS.

    Args:
        current: The current TransactionState.
        target: The target TransactionState.

    Returns:
        The target TransactionState if the transition is valid.

    Raises:
        InvalidTransition: If the transition from current to target is not allowed.
    """
    allowed_next_states = VALID_TRANSITIONS.get(current, frozenset())
    if target not in allowed_next_states:
        raise InvalidTransition(
            f"Cannot transition from {current.name} to {target.name}. "
            f"Allowed next states: {[s.name for s in allowed_next_states]}"
        )
    return target
