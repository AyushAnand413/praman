from __future__ import annotations

import pytest

from policy.states import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidTransition,
    TransactionState,
    transition,
)


def test_valid_transitions():
    # Test a few representative valid transitions
    assert transition(TransactionState.INTENT_CREATED, TransactionState.PROPOSING) == TransactionState.PROPOSING
    assert transition(TransactionState.PROPOSING, TransactionState.DEAL_PROPOSED) == TransactionState.DEAL_PROPOSED
    assert transition(TransactionState.DEAL_PROPOSED, TransactionState.APPROVED) == TransactionState.APPROVED
    assert transition(TransactionState.APPROVED, TransactionState.RESERVATION_HELD) == TransactionState.RESERVATION_HELD
    assert transition(TransactionState.RESERVATION_HELD, TransactionState.PAYMENT_PENDING) == TransactionState.PAYMENT_PENDING
    assert transition(TransactionState.PAYMENT_PENDING, TransactionState.PAYMENT_CAPTURED) == TransactionState.PAYMENT_CAPTURED
    assert transition(TransactionState.PAYMENT_CAPTURED, TransactionState.COMMITTED) == TransactionState.COMMITTED
    assert transition(TransactionState.COMMITTED, TransactionState.FULFILLED) == TransactionState.FULFILLED


def test_invalid_transitions():
    with pytest.raises(InvalidTransition):
        transition(TransactionState.INTENT_CREATED, TransactionState.APPROVED)
        
    with pytest.raises(InvalidTransition):
        transition(TransactionState.FULFILLED, TransactionState.COMPENSATING)


def test_terminal_states():
    for state in TERMINAL_STATES:
        # Should have an empty frozenset for outgoing transitions in VALID_TRANSITIONS
        assert VALID_TRANSITIONS.get(state, frozenset()) == frozenset()
        
        # Transitioning away from terminal state should raise InvalidTransition
        with pytest.raises(InvalidTransition):
            # Arbitrary target state
            transition(state, TransactionState.INTENT_CREATED)


def test_happy_path():
    state = TransactionState.INTENT_CREATED
    path = [
        TransactionState.PROPOSING,
        TransactionState.DEAL_PROPOSED,
        TransactionState.APPROVED,
        TransactionState.RESERVATION_HELD,
        TransactionState.PAYMENT_PENDING,
        TransactionState.PAYMENT_CAPTURED,
        TransactionState.COMMITTED,
        TransactionState.FULFILLED,
    ]
    
    for next_state in path:
        state = transition(state, next_state)
        
    assert state == TransactionState.FULFILLED


def test_failure_path():
    state = TransactionState.INTENT_CREATED
    state = transition(state, TransactionState.PROPOSING)
    state = transition(state, TransactionState.DEAL_PROPOSED)
    state = transition(state, TransactionState.EXPIRED)
    
    assert state == TransactionState.EXPIRED


def test_compensation_path():
    state = TransactionState.PAYMENT_CAPTURED
    state = transition(state, TransactionState.INVENTORY_COMMIT_FAILED)
    state = transition(state, TransactionState.COMPENSATING)
    state = transition(state, TransactionState.REFUNDED)
    
    assert state == TransactionState.REFUNDED
