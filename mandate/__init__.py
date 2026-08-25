"""Buyer mandates — ed25519 keygen, signing, verification, issuer registry.

A mandate is the buyer principal's signed authorisation: what may be bought,
up to what amount, until when, by which agent. Verification failures are
rejections with a named reason, never warnings.

Modules:
    keys.py      ed25519 seed and key handling; the only holder of a private key
                 is the wallet side
    token.py     the wire format and claim vocabulary, shared by both sides
    signer.py    the buyer's wallet — issues mandates
    verifier.py  the merchant — runs the eight checks and records every outcome
    issuers.py   the trusted-issuer registry (a declared simulation)
"""
