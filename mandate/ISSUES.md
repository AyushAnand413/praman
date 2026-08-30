# Issues — `mandate/` 

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


Ed25519 buyer-authority layer.

## Critical

| # | File | Issue | Fix |
|---|---|---|---|
| M1 | `mandate/issuers.py:TrustedIssuerRegistry` | In-memory only — restart wipes all issuers; `bootstrap_demo_issuer` re-creates only `demo-wallet-01` | Persist registry or re-bootstrap all onboarded issuers at startup from DB |
| M2 | `mandate/token.py` | No max token length check before base64url decode — 10MB token causes large alloc before shape check (DoS) | Cap `len(token) < 8192` at top of `split()` |
| M3 | `mandate/verifier.py` wildcard | `scope="purchase:*"` covers all categories — authorized signer could issue wildcard for any amount up to limit with no category allowlist at issuer level | Add per-issuer allowed-categories registry |

## High

| # | Issue |
|---|---|
| M4 | `MANDATE_SIGNING_SEED` 32-byte hex via env — no rotation/expiry. `revoke()` exists but no CRL; compromised key stays trusted until manual revoke |
| M5 | `mandate/signer.py:DEFAULT_TTL_SECONDS=900` hardcoded, not in `settings.py` — inconsistent with frozen-config discipline; no max TTL cap (year-long mandate possible) |
| M6 | `mandate/signer.py` holds private keys and is importable by any module — no AST test prevents `api/` or `store/` from mistakenly importing signer and holding private key in merchant process |

## Medium

| # | Issue |
|---|---|
| M7 | `mandate/verifier.py` does `SELECT` nonce check then `ledger.append` with `UNIQUE` fallback — pre-check is redundant (extra index probe). Comment acknowledges but could just rely on INSERT. |
| M8 | `mandate/token.py` `scope_covers()` handles `purchase:*` but no test for empty scope list `[]` — should be explicit reject |
| M9 | `mandate/keys.py` `generate_seed()` should use `secrets` not `os.urandom` for consistency — verify impl uses `secrets.token_hex` |
| M10 | No manifest signature verification in `harness` — tampered latency hints could mis-scope wallet mandate |

## Low

- 8-step pipeline order (shape→issuer→sig→expiry→nonce→agent→scope→amount) is correct cheapest-first.
- `UNIQUE` partial index on ledger is the true nonce store — audit trail *is* the nonce store (good).
