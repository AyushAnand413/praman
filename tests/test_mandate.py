"""The eight mandate checks, each proved reachable by its own token.

The verifier's value is entirely in what it refuses, so every rejection code gets
a test that constructs the specific token which triggers it — a real signed token
with exactly one thing wrong, not a hand-written string that happens to fail. A
suite that only tested acceptance would pass against a verifier that returned
`valid=True` unconditionally.

Two distinctions carry most of the weight and are asserted directly:

* An unknown issuer escalates to a human; a bad signature does not. Both are
  "the signature story does not check out", and treating them the same would
  either send forgeries to a person or refuse wallets that simply are not
  onboarded yet.
* A nonce is burned only on acceptance. A mandate refused on scope or amount must
  stay usable, because the buyer is expected to correct the cart and present it
  again.
"""

from __future__ import annotations

import base64
import json

import pytest

from mandate import signer, token as token_format
from mandate.issuers import DEMO_ISSUER_ID, TrustedIssuerRegistry
from mandate.verifier import (
    AGENT_MISMATCH,
    AMOUNT_EXCEEDED,
    BAD_SIGNATURE,
    EXPIRED,
    MALFORMED_MANDATE,
    NONCE_REPLAYED,
    REJECTION_CODES,
    SCOPE_MISMATCH,
    UNKNOWN_ISSUER,
    verify,
)
from store import ledger
from store.timestamps import plus_seconds, utc_now

AGENT = "agent-test"
CART_CATEGORIES = ("audio_accessories",)


def _verify(mandate: str, **overrides):
    """Verify with the arguments a Tier 1 headphone cart would supply."""
    kwargs = {
        "agent_id": AGENT,
        "cart_total_inr": 4_599,
        "categories": CART_CATEGORIES,
    }
    kwargs.update(overrides)
    return verify(mandate, **kwargs)


def _tamper_claims(mandate: str, **changes) -> str:
    """Rewrite a signed token's claims without re-signing it.

    This is what an attacker with a captured token can actually do: the segments
    are base64, not encryption. The signature therefore no longer matches, which
    is the point — it produces a genuine forgery rather than a simulated one.
    """
    header_segment, claims_segment, signature_segment = mandate.split(".")
    padded = claims_segment + "=" * (-len(claims_segment) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    claims.update(changes)
    reencoded = token_format.encode_json_segment(claims)
    return f"{header_segment}.{reencoded}.{signature_segment}"


# ── acceptance ────────────────────────────────────────────────────────────────


def test_a_well_formed_mandate_passes_all_eight_checks(db, mandate_for):
    verdict = _verify(mandate_for())

    assert verdict.valid
    assert verdict.code is None
    assert verdict.issuer == DEMO_ISSUER_ID
    assert verdict.agent_id == AGENT
    assert verdict.issuer_trusted is True
    assert verdict.ledger_seq is not None


def test_acceptance_is_recorded_on_the_ledger_with_its_nonce(db, mandate_for):
    verdict = _verify(mandate_for())

    entry = ledger.get(verdict.ledger_seq)
    assert entry is not None
    assert entry.event == "mandate.accepted"
    assert entry.payload["nonce"] == verdict.nonce
    assert entry.payload["checks_passed"] == len(REJECTION_CODES)


# ── 1. shape ──────────────────────────────────────────────────────────────────


def test_check_1_rejects_a_token_that_is_not_a_mandate(db, trusted_issuer):
    verdict = _verify("this is not a token at all")

    assert verdict.code == MALFORMED_MANDATE
    assert verdict.check == 1
    # No issuer was ever read, so the gate must get "no opinion", not "untrusted".
    assert verdict.issuer_trusted is None


def test_check_1_rejects_a_mandate_whose_header_names_a_different_issuer(
    db, trusted_issuer
):
    """A `kid` that disagrees with the signed `iss` is malformed, not untrusted.

    Caught at shape rather than at issuer lookup on purpose: the two fields name
    who signed it, and a token that answers that question twice with two answers
    has no issuer to look up.
    """
    claims = signer.build_claims(
        subject="user-test",
        agent_id=AGENT,
        categories=CART_CATEGORIES,
        max_amount_inr=50_000,
        max_single_txn_inr=50_000,
    )
    mandate = signer.sign(claims, issuer="some-other-wallet")

    verdict = _verify(mandate)

    assert verdict.code == MALFORMED_MANDATE
    assert verdict.check == 1


@pytest.mark.parametrize(
    "bad_limit", [0, -1, "5000", True], ids=["zero", "negative", "string", "bool"]
)
def test_check_1_rejects_a_non_positive_integer_spending_limit(
    db, trusted_issuer, mandate_for, bad_limit
):
    """`True` is in this list deliberately: in Python it is an int, and equal to 1.

    A spending limit of `True` must not be read as a limit of one rupee.
    """
    mandate = _tamper_claims(mandate_for(), max_single_txn_inr=bad_limit)

    verdict = _verify(mandate)

    assert verdict.code == MALFORMED_MANDATE
    assert verdict.check == 1


# ── 2. issuer ─────────────────────────────────────────────────────────────────


def test_check_2_rejects_an_issuer_that_is_not_registered(db, mandate_for):
    """Verified against an empty registry — the unbootstrapped-startup case."""
    verdict = verify(
        mandate_for(),
        agent_id=AGENT,
        cart_total_inr=4_599,
        categories=CART_CATEGORIES,
        issuers=TrustedIssuerRegistry(),
    )

    assert verdict.code == UNKNOWN_ISSUER
    assert verdict.check == 2
    assert verdict.issuer_trusted is False


def test_an_unknown_issuer_escalates_but_a_forgery_does_not(db, mandate_for):
    """The one distinction that decides whether a person gets involved."""
    unknown = verify(
        mandate_for(),
        agent_id=AGENT,
        cart_total_inr=4_599,
        categories=CART_CATEGORIES,
        issuers=TrustedIssuerRegistry(),
    )
    forged = _verify(_tamper_claims(mandate_for(), max_single_txn_inr=999_999))

    assert unknown.escalates_to_human is True
    assert forged.code == BAD_SIGNATURE
    assert forged.escalates_to_human is False


# ── 3. signature ──────────────────────────────────────────────────────────────


def test_check_3_rejects_claims_edited_after_signing(db, mandate_for):
    """Raising your own spending limit is the attack this check exists for."""
    original = mandate_for(max_single_txn_inr=100)
    raised = _tamper_claims(original, max_single_txn_inr=1_000_000)

    verdict = _verify(raised)

    assert verdict.code == BAD_SIGNATURE
    assert verdict.check == 3
    # The issuer was known; it is the signature that failed.
    assert verdict.issuer_trusted is True


def test_check_3_rejects_a_mandate_signed_by_the_wrong_key(db, trusted_issuer):
    """A real ed25519 signature, from a key the registry does not hold."""
    from nacl.signing import SigningKey

    impostor = SigningKey(b"\x02" * 32)
    mandate = signer.issue(
        subject="user-test",
        agent_id=AGENT,
        categories=CART_CATEGORIES,
        max_amount_inr=50_000,
        max_single_txn_inr=50_000,
        signing_key=impostor,
    )

    verdict = _verify(mandate)

    assert verdict.code == BAD_SIGNATURE
    assert verdict.check == 3


# ── 4. expiry ─────────────────────────────────────────────────────────────────


def test_check_4_rejects_an_expired_mandate(db, mandate_for):
    """Signed 20 minutes ago with a 15-minute TTL — genuinely lapsed, not faked."""
    issued_at = plus_seconds(utc_now(), -1_200)
    mandate = mandate_for(now=issued_at)

    verdict = _verify(mandate)

    assert verdict.code == EXPIRED
    assert verdict.check == 4


def test_expiry_is_checked_against_the_supplied_clock(db, mandate_for):
    """The same token is valid now and invalid later. Confirms `now` is honoured."""
    mandate = mandate_for(ttl_seconds=60)

    assert _verify(mandate, now=utc_now()).valid
    # A different nonce is not needed: the later call fails at check 4, which runs
    # before the nonce check, so the first acceptance cannot be what refuses it.
    assert _verify(mandate, now=plus_seconds(utc_now(), 3_600)).code == EXPIRED


# ── 5. nonce ──────────────────────────────────────────────────────────────────


def test_check_5_rejects_a_replayed_mandate(db, mandate_for):
    mandate = mandate_for()

    assert _verify(mandate).valid
    second = _verify(mandate)

    assert second.code == NONCE_REPLAYED
    assert second.check == 5


def test_a_rejected_mandate_does_not_burn_its_nonce(db, mandate_for):
    """Refused on amount, corrected, then accepted — with the same token.

    The nonce is burned by acceptance only. If a failed check consumed it, a buyer
    whose cart was momentarily over the limit could never use that mandate again,
    and the honest recovery path would look identical to a replay attack.
    """
    mandate = mandate_for(max_single_txn_inr=1_000)

    over_limit = _verify(mandate, cart_total_inr=4_599)
    assert over_limit.code == AMOUNT_EXCEEDED

    within_limit = _verify(mandate, cart_total_inr=900)
    assert within_limit.valid


def test_the_database_is_the_authority_on_replay(db, mandate_for):
    """The UNIQUE index refuses a second acceptance even if the pre-check misses it.

    Proved by writing the acceptance entry directly, bypassing `verify` entirely:
    what protects against a concurrent double-accept is the index, not the check
    that ran a moment earlier.
    """
    import sqlite3

    verdict = _verify(mandate_for())
    assert verdict.valid

    with pytest.raises(sqlite3.IntegrityError):
        ledger.append(
            "policy_kernel",
            "mandate.accepted",
            {"nonce": verdict.nonce, "replayed_by": "a concurrent request"},
            reason="second acceptance of one nonce, which must not be possible",
        )


# ── 6. agent identity ─────────────────────────────────────────────────────────


def test_check_6_rejects_a_mandate_presented_by_another_agent(db, mandate_for):
    """The stolen-token case: valid signature, wrong bearer."""
    mandate = mandate_for(agent_id="agent-authorised")

    verdict = _verify(mandate, agent_id="agent-thief")

    assert verdict.code == AGENT_MISMATCH
    assert verdict.check == 6


def test_identity_is_checked_before_commercial_limits(db, mandate_for):
    """A stolen mandate is rejected as stolen, even when it is also over budget.

    Check order is a property, not an implementation detail: an audit trail that
    logged this as AMOUNT_EXCEEDED would hide a theft behind a billing complaint.
    """
    mandate = mandate_for(agent_id="agent-authorised", max_single_txn_inr=100)

    verdict = _verify(mandate, agent_id="agent-thief", cart_total_inr=4_599)

    assert verdict.code == AGENT_MISMATCH


# ── 7. scope ──────────────────────────────────────────────────────────────────


def test_check_7_rejects_a_cart_outside_the_mandate_scope(db, mandate_for):
    mandate = mandate_for(categories=("cables",))

    verdict = _verify(mandate, categories=("audio_accessories",))

    assert verdict.code == SCOPE_MISMATCH
    assert verdict.check == 7
    assert "audio_accessories" in verdict.detail


def test_scope_must_cover_every_category_in_the_cart(db, mandate_for):
    """One uncovered line in a mixed cart is enough to refuse the whole cart."""
    mandate = mandate_for(categories=("audio_accessories",))

    verdict = _verify(mandate, categories=("audio_accessories", "cables"))

    assert verdict.code == SCOPE_MISMATCH


# ── 8. amount ─────────────────────────────────────────────────────────────────


def test_check_8_rejects_a_cart_over_the_single_transaction_limit(db, mandate_for):
    mandate = mandate_for(max_single_txn_inr=4_000, max_amount_inr=50_000)

    verdict = _verify(mandate, cart_total_inr=4_599)

    assert verdict.code == AMOUNT_EXCEEDED
    assert verdict.check == 8
    assert "max_single_txn_inr" in verdict.detail


def test_check_8_rejects_a_cart_over_the_overall_limit(db, mandate_for):
    mandate = mandate_for(max_single_txn_inr=50_000, max_amount_inr=4_000)

    verdict = _verify(mandate, cart_total_inr=4_599)

    assert verdict.code == AMOUNT_EXCEEDED
    assert "max_amount_inr" in verdict.detail


def test_a_cart_exactly_at_the_limit_is_allowed(db, mandate_for):
    """The boundary is inclusive. An off-by-one here refuses honest purchases."""
    mandate = mandate_for(max_single_txn_inr=4_599, max_amount_inr=4_599)

    assert _verify(mandate, cart_total_inr=4_599).valid


# ── the whole set ─────────────────────────────────────────────────────────────


def test_every_rejection_code_is_reachable(db, mandate_for, trusted_issuer):
    """One token per code, in one test, so a new code cannot be added untested.

    `REJECTION_CODES` is the verifier's own list. Comparing against it means this
    fails if someone adds a ninth check and never builds a token that trips it.
    """
    from nacl.signing import SigningKey

    reached: dict[str, int] = {}

    reached[MALFORMED_MANDATE] = _verify("not-a-token").check
    reached[UNKNOWN_ISSUER] = verify(
        mandate_for(),
        agent_id=AGENT,
        cart_total_inr=4_599,
        categories=CART_CATEGORIES,
        issuers=TrustedIssuerRegistry(),
    ).check
    reached[BAD_SIGNATURE] = _verify(
        signer.issue(
            subject="user-test",
            agent_id=AGENT,
            categories=CART_CATEGORIES,
            max_amount_inr=50_000,
            max_single_txn_inr=50_000,
            signing_key=SigningKey(b"\x03" * 32),
        )
    ).check
    reached[EXPIRED] = _verify(
        mandate_for(now=plus_seconds(utc_now(), -1_200))
    ).check

    replayed = mandate_for()
    assert _verify(replayed).valid
    reached[NONCE_REPLAYED] = _verify(replayed).check

    reached[AGENT_MISMATCH] = _verify(
        mandate_for(agent_id="someone-else")
    ).check
    reached[SCOPE_MISMATCH] = _verify(
        mandate_for(categories=("cables",)), categories=("audio_accessories",)
    ).check
    reached[AMOUNT_EXCEEDED] = _verify(
        mandate_for(max_single_txn_inr=10), cart_total_inr=4_599
    ).check

    assert set(reached) == set(REJECTION_CODES)
    # Each code reports its own position in the documented check order.
    assert [reached[code] for code in REJECTION_CODES] == list(
        range(1, len(REJECTION_CODES) + 1)
    )
