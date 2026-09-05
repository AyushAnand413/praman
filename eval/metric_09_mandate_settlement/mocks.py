"""Mocks and test fixtures for Metric 9: Mandate Verification & Settlement."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from typing import Any
import nacl.signing
from store.timestamps import plus_seconds, utc_now

from mandate.issuers import registry

# Seeded Ed25519 signing key for evaluation tests
TEST_SEED_HEX = os.environ.get("MANDATE_SIGNING_SEED", "f5ae8ad559a2e8c317dc7b03ecf9f040965437b58e9d15fb493a08cd1c6455cb")
_SIGNING_KEY = nacl.signing.SigningKey(bytes.fromhex(TEST_SEED_HEX))
DEMO_ISSUER = "wallet_demo_trusted"

# Pre-register DEMO_ISSUER so signature and lifecycle tests evaluate properly
registry.register(DEMO_ISSUER, _SIGNING_KEY.verify_key.encode().hex())


def make_test_mandate(
    *,
    sub: str = "human_eval_buyer",
    agent_id: str = "agent_eval_test",
    scope: str = "purchase:electronics",
    max_amount_inr: int = 10000,
    max_single_txn_inr: int = 6000,
    valid_until: str | None = None,
    nonce: str | None = None,
    iss: str = DEMO_ISSUER,
    corrupt_sig: bool = False,
    corrupt_claims: bool = False,
    missing_claim: str | None = None,
    extra_claim: bool = False,
    alg: str = "EdDSA",
    strip_sig: bool = False,
    signing_key: nacl.signing.SigningKey | None = None,
) -> str:
    """Generate an Ed25519 signed JWS mandate token with customizable claims."""
    header = {"alg": alg, "typ": "JWT"}
    claims = {
        "sub": sub,
        "agent_id": agent_id,
        "scope": scope,
        "max_amount_inr": max_amount_inr,
        "max_single_txn_inr": max_single_txn_inr,
        "valid_until": valid_until or plus_seconds(utc_now(), 3600).isoformat(),
        "nonce": nonce or f"nonce-{uuid.uuid4()}",
        "iss": iss,
    }

    if missing_claim and missing_claim in claims:
        del claims[missing_claim]

    if extra_claim:
        claims["unauthorized_privilege"] = "admin_super"

    h_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    c_b64 = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    msg = f"{h_b64}.{c_b64}".encode()

    signer = signing_key or _SIGNING_KEY
    sig = signer.sign(msg).signature

    if corrupt_sig:
        sig = b"x" * len(sig)

    if strip_sig:
        return f"{h_b64}.{c_b64}."

    s_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{h_b64}.{c_b64}.{s_b64}"


def generate_webhook_signature(payload_bytes: bytes, secret: Any = "AetherAudioSecret2026!") -> str:
    """Generate Razorpay HMAC-SHA256 webhook signature."""
    sec_str = secret.reveal() if hasattr(secret, "reveal") else str(secret)
    return hmac.new(sec_str.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
