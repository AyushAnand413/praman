"""Razorpay client — Orders, Payments, and Refunds.

**This is the only module in the project that holds payment credentials.**
That is an architectural invariant, and `tests/test_import_boundary.py` fails
the build if anything under `vyapaari/` can reach it.

Scope here is deliberately thin: create an order, fetch it, capture a payment,
refund a payment. Nothing in this module decides *whether* any of that should
happen — it is transport. Authorisation lives in the policy kernel, and webhook
handling and idempotency sit on top of it, because the kernel must exist to
authorise an action before anything automated can move money.

Implemented directly against the REST API with httpx rather than the razorpay
SDK: one fewer dependency, and credentials stay inside `settings.Secret` until
the exact line that builds the auth header.

Unit discipline: **Razorpay speaks paise, this system speaks whole rupees.**
The conversion happens in `_to_paise`/`_to_rupees` and nowhere else. Every
amount crossing this module's public surface is rupees.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from settings import RAZORPAY_API_BASE, secret

#: Razorpay sits in the checkout path, which is budgeted at 2-4s end to end.
DEFAULT_TIMEOUT_S = 10.0


class RazorpayError(RuntimeError):
    """A non-2xx response, or a response we refuse to interpret."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 body: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


def _to_paise(amount_inr: int) -> int:
    """Rupees -> paise. Integers only: no float ever touches an amount."""
    if not isinstance(amount_inr, int) or isinstance(amount_inr, bool):
        raise TypeError(
            f"amount must be an int number of rupees, got {type(amount_inr).__name__}"
        )
    if amount_inr < 0:
        raise ValueError("amount must not be negative")
    return amount_inr * 100


def _to_rupees(amount_paise: int) -> int:
    """Paise -> rupees. Refuses fractional rupees rather than rounding money."""
    paise = int(amount_paise)
    if paise % 100 != 0:
        raise RazorpayError(
            f"Razorpay returned {paise} paise, which is not a whole number of "
            "rupees. This system has no paise representation; refusing to round."
        )
    return paise // 100


def verify_webhook_signature(body: bytes, signature: str | None) -> bool:
    """Is this webhook body genuinely from Razorpay?

    HMAC-SHA256 of the raw request body under the webhook secret, hex-encoded,
    compared in constant time against the `X-Razorpay-Signature` header.

    Takes `bytes`, not a parsed object, and that matters: the signature covers
    the exact bytes Razorpay sent. Re-serialising the parsed JSON would change
    key order or spacing and every legitimate webhook would fail. The caller must
    read the raw body and must not trust it until this returns True.

    Lives here because this is the module that holds Razorpay credentials, and
    the webhook secret is one of them.
    """
    if not signature:
        return False
    expected = hmac.new(
        secret("RAZORPAY_WEBHOOK_SECRET").reveal().encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


class RazorpayClient:
    """Thin, synchronous client over the surfaces this system actually uses."""

    def __init__(
        self,
        *,
        key_id: str | None = None,
        key_secret: str | None = None,
        base_url: str = RAZORPAY_API_BASE,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        # Credentials are read here and nowhere else. `.reveal()` marks the one
        # line where the secret becomes a plain string.
        self._key_id = key_id if key_id is not None else secret("RAZORPAY_KEY_ID").reveal()
        self._key_secret = (
            key_secret if key_secret is not None else secret("RAZORPAY_KEY_SECRET").reveal()
        )
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

        if not self._key_id.startswith("rzp_test_"):
            # Live mode is never used. A live key here is a bug that costs real
            # money, so fail at construction rather than at capture.
            raise RazorpayError(
                f"RAZORPAY_KEY_ID must be a test key (rzp_test_...); got "
                f"{self._key_id[:9]}.... This project never touches live mode."
            )

    @property
    def key_id(self) -> str:
        """Safe to expose: the key id is public (it ships in Checkout)."""
        return self._key_id

    def __repr__(self) -> str:  # never print the secret
        return f"<RazorpayClient key_id={self._key_id} base={self._base_url}>"

    # -- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        # Callers may add headers (refund idempotency, for one) without being
        # able to drop the content type by accident.
        merged_headers = {"Content-Type": "application/json", **(headers or {})}
        try:
            response = httpx.request(
                method,
                url,
                auth=(self._key_id, self._key_secret),
                timeout=self._timeout,
                headers=merged_headers,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise RazorpayError(f"{method} {path} failed to reach Razorpay: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            body = {"raw": response.text[:500]}

        if response.status_code >= 400:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            raise RazorpayError(
                f"{method} {path} -> {response.status_code}: "
                f"{error.get('description') or body}",
                status_code=response.status_code,
                body=body if isinstance(body, dict) else {},
            )
        return body

    # -- Orders ------------------------------------------------------------

    def create_order(
        self,
        amount_inr: int,
        *,
        receipt: str,
        notes: dict[str, str] | None = None,
        currency: str = "INR",
    ) -> dict[str, Any]:
        """Create an order with the amount fixed server-side.

        The buyer agent never supplies the amount to Razorpay; it supplies an
        option_id, and the server looks up the price. That is what makes price
        tampering structurally impossible rather than merely validated.
        """
        payload: dict[str, Any] = {
            "amount": _to_paise(amount_inr),
            "currency": currency,
            "receipt": receipt,
        }
        if notes:
            payload["notes"] = notes
        order = self._request("POST", "/orders", json=payload)
        return self._normalize_order(order)

    def fetch_order(self, order_id: str) -> dict[str, Any]:
        return self._normalize_order(self._request("GET", f"/orders/{order_id}"))

    def fetch_order_payments(self, order_id: str) -> list[dict[str, Any]]:
        """Payments attempted against an order — how a pay_... id is found."""
        body = self._request("GET", f"/orders/{order_id}/payments")
        return [self._normalize_payment(item) for item in body.get("items", [])]

    # -- Payments ----------------------------------------------------------

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        return self._normalize_payment(self._request("GET", f"/payments/{payment_id}"))

    def capture_payment(
        self, payment_id: str, amount_inr: int, *, currency: str = "INR"
    ) -> dict[str, Any]:
        """Capture an authorized payment.

        Razorpay requires the amount on capture and rejects a mismatch, which
        is a second server-side check that the captured figure is the one we
        priced.
        """
        body = self._request(
            "POST",
            f"/payments/{payment_id}/capture",
            json={"amount": _to_paise(amount_inr), "currency": currency},
        )
        return self._normalize_payment(body)

    # -- Refunds -----------------------------------------------------------

    def refund_payment(
        self,
        payment_id: str,
        *,
        amount_inr: int | None = None,
        speed: str = "normal",
        notes: dict[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Refund a captured payment, in full by default.

        `amount_inr=None` refunds the whole payment. Passing an amount makes it
        partial, and Razorpay rejects an amount larger than what remains — a
        second server-side check on top of the caller's own.

        `idempotency_key` is forwarded as Razorpay's `X-Razorpay-Idempotency`
        header. Refunds are the one surface where the gateway offers its own
        idempotency, and using it means a retried refund cannot become two
        refunds even if this process dies between the call and recording it.
        """
        payload: dict[str, Any] = {"speed": speed}
        if amount_inr is not None:
            payload["amount"] = _to_paise(amount_inr)
        if notes:
            payload["notes"] = notes

        headers = (
            {"X-Razorpay-Idempotency": idempotency_key} if idempotency_key else None
        )
        body = self._request(
            "POST",
            f"/payments/{payment_id}/refund",
            json=payload,
            headers=headers,
        )
        return self._normalize_refund(body)

    def fetch_refund(self, refund_id: str) -> dict[str, Any]:
        return self._normalize_refund(self._request("GET", f"/refunds/{refund_id}"))

    def fetch_payment_refunds(self, payment_id: str) -> list[dict[str, Any]]:
        """Refunds already issued against a payment.

        Worth checking before issuing another: it turns "did my refund go
        through?" into a question with an answer from the gateway rather than a
        guess from local state.
        """
        body = self._request("GET", f"/payments/{payment_id}/refunds")
        return [self._normalize_refund(item) for item in body.get("items", [])]

    # -- normalization -----------------------------------------------------

    def _normalize_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": order.get("id"),
            "amount_inr": _to_rupees(order["amount"]),
            "amount_paid_inr": _to_rupees(order.get("amount_paid", 0)),
            "currency": order.get("currency"),
            "receipt": order.get("receipt"),
            "status": order.get("status"),
            "attempts": order.get("attempts"),
            "notes": order.get("notes") or {},
            "created_at": order.get("created_at"),
        }

    def _normalize_payment(self, payment: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": payment.get("id"),
            "order_id": payment.get("order_id"),
            "amount_inr": _to_rupees(payment["amount"]),
            "currency": payment.get("currency"),
            "status": payment.get("status"),
            "method": payment.get("method"),
            "captured": bool(payment.get("captured")),
            "error_code": payment.get("error_code"),
            "error_description": payment.get("error_description"),
            "created_at": payment.get("created_at"),
        }

    def _normalize_refund(self, refund: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": refund.get("id"),
            "payment_id": refund.get("payment_id"),
            "amount_inr": _to_rupees(refund["amount"]),
            "currency": refund.get("currency"),
            "status": refund.get("status"),
            "speed_processed": refund.get("speed_processed"),
            "notes": refund.get("notes") or {},
            "created_at": refund.get("created_at"),
        }
