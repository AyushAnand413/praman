# Issues — `harness/` (Buyer Counterparty)

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


## High

| # | File | Issue |
|---|---|---|
| H1 | `harness/grahak.py:Wallet` | No spend tracking across mandates — multiple mandates can double-spend up to per-mandate limit exceeding intended lifetime budget. Nonce prevents replay of same token but not issuance of new tokens. |
| H2 | `harness/grahak.py:discover()` | Trusts manifest latency hints + mandate threshold from untyped JSON with no signature verification — tampered manifest could mis-scope mandate |
| H3 | `harness/grahak.py:Wallet.buy()` | Cart categories derived client-side from discovery, not server-validated — scope could be over-broad wildcard even if cart narrower |

## Medium

| # | Issue |
|---|---|
| H4 | `harness/ab.py:run_ab` runs sessions sequentially — 400 sessions × ~1s ≈ 7 min, no concurrency/async |
| H5 | `harness/grahak.py:shop_as` catches `StoreRefused`/`WalletRefused` as data points (correct) but `TestClient` HTTP errors (timeout/connection) abort batch as unhandled exceptions |
| H6 | `PERSONAS` 8 personas with budgets ₹2000–unbounded hardcoded — persona drifts not versioned; tightly coupled to gate tiers (₹2000/₹6000) — gate change breaks coverage silently |
| H7 | `harness/ab.py` control arm claims `_base_only_option` but if `offer` assembly changes recommendation logic, control could inadvertently receive treatment upsells — add assertion |

## Low

- Counterparty boundary (never imports `settings` private tables) is correctly enforced.
- `Discovery`/`Offer`/`Purchase` models are clean helpers for `choose()` strategies.
