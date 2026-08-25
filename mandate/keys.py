"""ed25519 key handling for the mandate layer.

Two sides use this module. The buyer's wallet holds a signing key and issues
mandates; the merchant holds only public keys and verifies them. Nothing here
ever moves a private key across that line — `public_key_hex` is the only export
that produces something safe to publish.

Seeds come from the environment. `MANDATE_SIGNING_SEED` is 32 bytes of hex,
which makes the demo wallet's identity stable across restarts so a mandate
issued yesterday still verifies today. When the seed is absent — a fresh
checkout, a test run — a random one is generated and cached for the life of the
process. That keeps the system runnable without configuration while making the
consequence explicit: an ephemeral wallet's mandates do not survive a restart.
"""

from __future__ import annotations

import secrets

from nacl.signing import SigningKey, VerifyKey

from settings import secret

#: ed25519 seeds are exactly 32 bytes.
SEED_BYTES = 32

#: Cached ephemeral wallet, used only when no seed is configured.
_ephemeral_seed: str | None = None


class MandateKeyError(RuntimeError):
    """A malformed seed or key. Never raised with key material in the message."""


def generate_seed() -> str:
    """A fresh 32-byte seed as hex. Print this once and store it as a secret."""
    return secrets.token_hex(SEED_BYTES)


def signing_key_from_seed(seed_hex: str) -> SigningKey:
    """Build a signing key from a hex seed.

    Deterministic: the same seed always yields the same key pair, which is what
    makes the wallet's public identity stable.
    """
    try:
        raw = bytes.fromhex(seed_hex.strip())
    except ValueError as exc:
        raise MandateKeyError(
            "mandate seed is not valid hex; expected "
            f"{SEED_BYTES * 2} hex characters"
        ) from exc
    if len(raw) != SEED_BYTES:
        raise MandateKeyError(
            f"mandate seed must be {SEED_BYTES} bytes ({SEED_BYTES * 2} hex "
            f"characters); got {len(raw)} bytes"
        )
    return SigningKey(raw)


def wallet_signing_key() -> SigningKey:
    """The buyer wallet's signing key.

    Reads `MANDATE_SIGNING_SEED` when set. Otherwise generates one random seed
    per process and reuses it, so every mandate signed by this process verifies
    against one public key.
    """
    global _ephemeral_seed
    configured = secret("MANDATE_SIGNING_SEED", required=False).reveal()
    if configured:
        return signing_key_from_seed(configured)
    if _ephemeral_seed is None:
        _ephemeral_seed = generate_seed()
    return signing_key_from_seed(_ephemeral_seed)


def public_key_hex(key: SigningKey | VerifyKey) -> str:
    """The public half as hex — the only form that leaves the wallet."""
    verify = key.verify_key if isinstance(key, SigningKey) else key
    return bytes(verify).hex()


def verify_key_from_hex(public_hex: str) -> VerifyKey:
    try:
        raw = bytes.fromhex(public_hex.strip())
    except ValueError as exc:
        raise MandateKeyError("public key is not valid hex") from exc
    if len(raw) != 32:
        raise MandateKeyError(
            f"an ed25519 public key is 32 bytes; got {len(raw)}"
        )
    return VerifyKey(raw)
