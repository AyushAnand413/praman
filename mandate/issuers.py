"""The trusted-issuer registry — which wallets the merchant will believe.

A mandate is only as good as the answer to "who signed it". This registry is
that answer: an issuer whose public key is not here is a stranger claiming
authority, and the verifier rejects it before it spends time on cryptography.

⚠️ **Known simplification, stated openly.** This registry is simulated. The demo
generates the buyer wallet's keypair and pre-loads its public key here. In
production the trust anchor comes from a real wallet provider, a bank, or an
NPCI-designated issuer, delivered over a channel the merchant already trusts.
What is implemented is the mandate *pattern*, not the mandate *rails* — when
those rails exist, this module is what gets replaced, and nothing above it
changes.

The registry is deliberately not loaded from a file the buyer can influence.
Registration is an explicit call, made at startup by code the merchant controls.
"""

from __future__ import annotations

import threading

from nacl.signing import VerifyKey

from mandate import keys

#: The simulated buyer wallet. One issuer is enough to demonstrate the pattern;
#: the registry itself is a mapping, so adding real issuers is data, not code.
DEMO_ISSUER_ID = "demo-wallet-01"


class TrustedIssuerRegistry:
    """Issuer id -> ed25519 public key. Read-mostly, guarded for thread safety.

    Note: in-memory only, not persisted across restarts. Suitable for demo;
    production would use persistent storage.
    """

    def __init__(self) -> None:
        self._keys: dict[str, VerifyKey] = {}
        self._lock = threading.Lock()

    def register(self, issuer_id: str, public_key_hex: str) -> None:
        """Add or replace an issuer's public key.

        Replacement is allowed because key rotation is a real operation, but it
        is a deliberate call — nothing rotates a key as a side effect.
        """
        key = keys.verify_key_from_hex(public_key_hex)
        with self._lock:
            self._keys[issuer_id] = key

    def revoke(self, issuer_id: str) -> None:
        with self._lock:
            self._keys.pop(issuer_id, None)

    def get(self, issuer_id: str) -> VerifyKey | None:
        """The issuer's key, or None when the issuer is unknown."""
        return self._keys.get(issuer_id)

    def is_trusted(self, issuer_id: str) -> bool:
        return issuer_id in self._keys

    def issuer_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))

    def public_keys(self) -> dict[str, str]:
        """Issuer id -> public key hex. Safe to serialize; contains no secrets."""
        return {name: bytes(key).hex() for name, key in sorted(self._keys.items())}

    def clear(self) -> None:
        with self._lock:
            self._keys.clear()

    def __len__(self) -> int:
        return len(self._keys)


#: Process-wide registry. Populated by `bootstrap_demo_issuer` at startup.
registry = TrustedIssuerRegistry()


def bootstrap_demo_issuer(issuer_id: str = DEMO_ISSUER_ID) -> str:
    """Pre-load the simulated wallet's public key. Returns the public key hex.

    This one call is the whole simulation: it stands in for a wallet provider
    publishing its key to a merchant out of band.
    """
    public_hex = keys.public_key_hex(keys.wallet_signing_key())
    registry.register(issuer_id, public_hex)
    return public_hex
