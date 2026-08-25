"""Razorpay client v0 and secret handling — offline unit tests.

No network. The live smoke test is `scripts/razorpay_smoke.py`, which needs
real test-mode keys.
"""

from __future__ import annotations

import pytest

from kernel.payments import RazorpayClient, RazorpayError, _to_paise, _to_rupees
from settings import MissingSecret, Secret, assert_no_secrets_in_prompt, secret


# ── amount handling: rupees in, paise on the wire ──────────────────────────


def test_rupees_convert_to_paise():
    assert _to_paise(5598) == 559800
    assert _to_paise(0) == 0


def test_floats_are_not_accepted_as_amounts():
    """Money is integer rupees everywhere. A float amount is a bug, not input."""
    with pytest.raises(TypeError):
        _to_paise(5598.0)
    with pytest.raises(TypeError):
        _to_paise(True)


def test_negative_amounts_are_rejected():
    with pytest.raises(ValueError):
        _to_paise(-1)


def test_fractional_rupees_from_razorpay_are_refused_not_rounded():
    assert _to_rupees(559800) == 5598
    with pytest.raises(RazorpayError, match="not a whole number of rupees"):
        _to_rupees(559850)


# ── credentials: test mode only ────────────────────────────────────────────


def test_live_key_is_refused_at_construction():
    with pytest.raises(RazorpayError, match="must be a test key"):
        RazorpayClient(key_id="rzp_live_abc123", key_secret="x")


def test_test_key_is_accepted():
    client = RazorpayClient(key_id="rzp_test_abc123", key_secret="x")
    assert client.key_id == "rzp_test_abc123"


def test_repr_never_exposes_the_secret():
    client = RazorpayClient(key_id="rzp_test_abc123", key_secret="super-secret-value")
    assert "super-secret-value" not in repr(client)


# ── secrets: from the environment only ─────────────────────────────────────


def test_secret_masks_itself_in_every_string_context():
    value = Secret("hunter2-hunter2", "RAZORPAY_KEY_SECRET")
    assert "hunter2" not in repr(value)
    assert "hunter2" not in str(value)
    assert "hunter2" not in f"{value}"
    assert "hunter2" not in "{}".format(value)
    assert value.reveal() == "hunter2-hunter2"


def test_missing_required_secret_raises(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(MissingSecret):
        secret("GEMINI_API_KEY")
    assert secret("GEMINI_API_KEY", required=False).reveal() == ""


def test_undeclared_secret_name_is_rejected():
    with pytest.raises(KeyError):
        secret("SOME_KEY_NOBODY_DECLARED")


def test_prompt_guard_blocks_a_secret_bearing_prompt(monkeypatch):
    """A secret must never reach an LLM prompt. The prompt builder calls this."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSy-not-a-real-key-000")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        assert_no_secrets_in_prompt("catalog… key=AIzaSy-not-a-real-key-000")

    assert_no_secrets_in_prompt("buyer needs earbuds under Rs 5000")  # clean prompt


def test_prompt_guard_ignores_trivially_short_values(monkeypatch):
    """A 2-character secret would match everything; do not cry wolf."""
    monkeypatch.setenv("DEMO_KEY", "ab")
    assert_no_secrets_in_prompt("a buyer agent wants a black case")
