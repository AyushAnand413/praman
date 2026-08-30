from __future__ import annotations

import pytest

from policy.core_safety import (
    InvariantViolation,
    check_all_post_payment,
    check_all_pre_payment,
    check_invariant_1,
    check_invariant_2,
    check_invariant_3,
    check_invariant_4,
    check_invariant_5,
    check_invariant_6,
    check_invariant_7,
    check_invariant_8,
)


def test_invariant_1():
    check_invariant_1("deterministic")  # Should pass
    
    with pytest.raises(InvariantViolation) as exc:
        check_invariant_1("llm")
    assert exc.value.invariant_number == 1
    
    with pytest.raises(InvariantViolation):
        check_invariant_1("generative")


def test_invariant_2():
    check_invariant_2("verdict-123")  # Should pass
    
    with pytest.raises(InvariantViolation) as exc:
        check_invariant_2(None)
    assert exc.value.invariant_number == 2
    
    with pytest.raises(InvariantViolation):
        check_invariant_2("")


def test_invariant_3():
    check_invariant_3(1000, 1000)  # Should pass
    
    with pytest.raises(InvariantViolation) as exc:
        check_invariant_3(1000, 900)
    assert exc.value.invariant_number == 3


def test_invariant_4():
    check_invariant_4("hash-123", "hash-123")  # Should pass
    
    with pytest.raises(InvariantViolation) as exc:
        check_invariant_4("hash-123", "hash-abc")
    assert exc.value.invariant_number == 4


def test_invariant_5():
    check_invariant_5("HELD")       # Should pass
    check_invariant_5("COMMITTED")  # Should pass
    
    with pytest.raises(InvariantViolation) as exc:
        check_invariant_5("RELEASED")
    assert exc.value.invariant_number == 5


def test_invariant_6():
    check_invariant_6("idem-123")  # Should pass
    
    with pytest.raises(InvariantViolation) as exc:
        check_invariant_6(None)
    assert exc.value.invariant_number == 6
    
    with pytest.raises(InvariantViolation):
        check_invariant_6("")


def test_invariant_7():
    check_invariant_7("TDR-123")  # Should pass
    
    with pytest.raises(InvariantViolation) as exc:
        check_invariant_7(None)
    assert exc.value.invariant_number == 7


def test_invariant_8():
    check_invariant_8("TDR-123", 1000, 1000, "hash1", "hash1")  # Should pass
    
    with pytest.raises(InvariantViolation) as exc:
        check_invariant_8(None, 1000, 1000, "hash1", "hash1")
    assert exc.value.invariant_number == 8
    
    with pytest.raises(InvariantViolation):
        check_invariant_8("TDR-123", 1000, 900, "hash1", "hash1")
        
    with pytest.raises(InvariantViolation):
        check_invariant_8("TDR-123", 1000, 1000, "hash1", "hash2")


def test_check_all_pre_payment():
    # Valid
    check_all_pre_payment("deterministic", "verdict-1", 100, 100, "h1", "h1", "HELD", "idem-1")
    
    # Invalid (violates invariant 3)
    with pytest.raises(InvariantViolation) as exc:
        check_all_pre_payment("deterministic", "verdict-1", 100, 200, "h1", "h1", "HELD", "idem-1")
    assert exc.value.invariant_number == 3


def test_check_all_post_payment():
    # Valid
    check_all_post_payment("TDR-1", 100, 100, "h1", "h1")
    
    # Invalid (violates invariant 8)
    with pytest.raises(InvariantViolation) as exc:
        check_all_post_payment("TDR-1", 100, 100, "h1", "h2")
    assert exc.value.invariant_number == 8
