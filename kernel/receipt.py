"""The policy receipt — a signed record binding a decision to its reason.

Issued at decision time, before any money moves, and returned to the buyer
alongside the offer. The signature is the entire point: without it, the reason
attached to a transaction is a log line that could have been written afterward.
With it, the reason provably existed before the charge.

    receipt_id, offer_id, issued_at
    verdicts[]    per-item APPROVE / REJECT_ITEM plus the bound that fired
    reasons[]     the human-readable strings, exactly as shown to the buyer
    gate_tier     and the triggers that produced it
    policy_mode   shadow receipts are signed too, and are marked as shadow
    signature     HMAC-SHA256 over canonical JSON, server-side secret

Design notes:

* **HMAC, not a public-key signature.** The verifier is the merchant, using a
  secret only the merchant holds, so a symmetric MAC is the honest primitive.
  Calling it a "signature" that a third party could verify would overclaim —
  a buyer can verify the *chain* through the public audit endpoint, and that is
  the guarantee actually on offer.

* **The signature covers the body, never itself.** `_signing_body` builds the
  material, `issue` appends the MAC, and `verify` recomputes over the body with
  the MAC removed. One function produces the material for both paths, so the
  signer and the verifier cannot drift apart.

* **Comparison is constant-time.** `hmac.compare_digest`, not `==`, so a
  tampered receipt cannot be brute-forced a byte at a time.

* **Shadow receipts are real receipts.** Same evaluation, same signature,
  tagged `policy_mode: shadow`. That is what makes shadow mode a genuine
  rehearsal rather than a different code path.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any

from settings import secret
from kernel import mode
from kernel.bounds import BoundResult, CartEvaluation
from kernel.gates import GateDecision
from store import ids
from store.canonical import canonical_json
from store.timestamps import now_ts

#: Bumped if the signed shape changes, so an old receipt is recognised as old
#: rather than as forged.
RECEIPT_VERSION = "1"

SIGNATURE_ALGORITHM = "HMAC-SHA256"


class ReceiptError(RuntimeError):
    """A receipt that cannot be issued or cannot be trusted."""


@dataclass(frozen=True)
class PolicyReceipt:
    receipt_id: str
    offer_id: str
    issued_at: str
    gate_tier: int
    policy_mode: str
    verdicts: tuple[dict[str, Any], ...]
    reasons: tuple[str, ...]
    gate: dict[str, Any]
    totals: dict[str, Any]
    signature: str

    def as_payload(self) -> dict[str, Any]:
        """The full receipt, signature included. This is what ships to the buyer."""
        return {**_signing_body(self), "signature": self.signature}


def _signing_body(receipt: "PolicyReceipt | dict[str, Any]") -> dict[str, Any]:
    """The exact material the MAC covers.

    Accepts either a receipt object or a parsed receipt dict so that issuing and
    verifying build the material through the same code path.
    """
    if isinstance(receipt, PolicyReceipt):
        source: dict[str, Any] = {
            "version": RECEIPT_VERSION,
            "receipt_id": receipt.receipt_id,
            "offer_id": receipt.offer_id,
            "issued_at": receipt.issued_at,
            "gate_tier": receipt.gate_tier,
            "policy_mode": receipt.policy_mode,
            "verdicts": list(receipt.verdicts),
            "reasons": list(receipt.reasons),
            "gate": receipt.gate,
            "totals": receipt.totals,
            "algorithm": SIGNATURE_ALGORITHM,
        }
        return source

    body = {key: value for key, value in receipt.items() if key != "signature"}
    missing = {
        "version", "receipt_id", "offer_id", "issued_at", "gate_tier",
        "policy_mode", "verdicts", "reasons", "gate", "totals", "algorithm",
    } - set(body)
    if missing:
        raise ReceiptError(
            f"receipt is missing signed field(s) {sorted(missing)}; refusing to "
            "verify a partial receipt"
        )
    return body


def _mac(body: dict[str, Any], *, key: bytes | None = None) -> str:
    material = canonical_json(body).encode("ascii")
    signing_key = key if key is not None else _signing_key()
    return hmac.new(signing_key, material, sha256).hexdigest()


def _signing_key() -> bytes:
    return secret("POLICY_RECEIPT_HMAC_SECRET").reveal().encode("utf-8")


def issue(
    *,
    offer_id: str,
    evaluation: CartEvaluation,
    gate: GateDecision,
    reasons: list[str] | tuple[str, ...] = (),
    extra_bounds: list[BoundResult] | tuple[BoundResult, ...] = (),
    key: bytes | None = None,
) -> PolicyReceipt:
    """Sign the decision that was just made.

    `reasons` are the human-readable strings shown to the buyer, recorded here
    verbatim. If the prose shown to a human and the prose in the receipt could
    differ, the receipt would not be evidence of anything.

    `extra_bounds` carries results evaluated outside the cart evaluation — the
    mandate checks, for instance — so the receipt covers every bound that
    contributed to the verdict rather than only the ones the cart produced.
    """
    verdicts: list[dict[str, Any]] = [v.as_payload() for v in evaluation.item_verdicts]
    cart_bounds = [b.as_payload() for b in evaluation.cart_bounds]
    cart_bounds += [b.as_payload() for b in extra_bounds]

    receipt = PolicyReceipt(
        receipt_id=ids.receipt_id(),
        offer_id=offer_id,
        issued_at=now_ts(),
        gate_tier=gate.tier,
        policy_mode=mode.mode_value(),
        verdicts=tuple(verdicts),
        reasons=tuple(reasons),
        gate={**gate.as_payload(), "cart_bounds": cart_bounds},
        totals={
            "total_inr": evaluation.total_inr,
            "list_total_inr": evaluation.list_total_inr,
            "discount_inr": evaluation.discount_inr,
            "discount_pct": str(evaluation.discount_pct),
            "offer_failed": evaluation.offer_failed,
            "failure_detail": evaluation.failure_detail,
        },
        signature="",
    )
    return replace(receipt, signature=_mac(_signing_body(receipt), key=key))


def verify(receipt: dict[str, Any], *, key: bytes | None = None) -> bool:
    """True when the receipt's MAC matches its contents.

    Constant-time comparison. A missing or non-string signature is a failure,
    not an exception, so a forged receipt and a malformed one are handled the
    same way by callers.
    """
    presented = receipt.get("signature")
    if not isinstance(presented, str) or not presented:
        return False
    expected = _mac(_signing_body(receipt), key=key)
    return hmac.compare_digest(expected, presented)


def require_valid(receipt: dict[str, Any], *, key: bytes | None = None) -> None:
    """Raise unless the receipt verifies. Used where proceeding would be unsafe."""
    if not verify(receipt, key=key):
        raise ReceiptError(
            f"policy receipt {receipt.get('receipt_id')!r} failed signature "
            "verification: its contents do not match the decision that was signed"
        )
