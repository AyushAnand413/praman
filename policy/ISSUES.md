# Issues — `policy/` 

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


Deterministic MEC resolver, optimizer, negotiation, TDR.

## High

| # | File | Issue |
|---|---|---|
| P1 | `policy/mec.py` + `settings.py` | Two floor formulas co-exist: `cost × 1.20` (bounds) vs `min_margin_pct=20%` (MEC). Must stay in sync manually — add parity test. |
| P2 | `policy/negotiation.py:MAX_NEGOTIATION_ROUNDS=3` | Hardcoded, not in `settings.py` — inconsistent with frozen-bound discipline, not tunable without code change |
| P3 | `policy/negotiation.py` floor: `variableCost/(1 - minMargin/100)` | `ZeroDivisionError` if `min_margin_pct=100` — no guard, should clamp to ≤99 or return `NO_FEASIBLE_DEAL` |
| P4 | `policy/resolver.py` | Claims "pure business logic, no I/O" but reads `store.mec_store` via `conn` — implicit DB dependency, hard to test without injection |

## Medium

| # | Issue |
|---|---|
| P5 | `policy/mec.py:EconomicObjectives` validates `sum==Decimal('1.0')` exact — `0.33*3+0.01` quantize mismatch fails; needs helper `normalize_weights()` |
| P6 | `policy/pre_filter.py` operates on `CandidateDeal.total_inr/discount_pct` computed in `kernel/offer.py:_to_candidate_deal` — if proposal parsing miscomputes `unit_discount`, pre-filter could accept over-discounted line before `kernel/bounds.py` double-check |
| P7 | `policy/mec.py:compute_hash` calls `canonical_json` which rejects `Decimal` — MEC with `Decimal` hard constraints could throw; caller must sanitize |
| P8 | `policy/tdr.py` immutability relies on `store/tdr_store` but no SQL trigger prevents `UPDATE/DELETE` of `transaction_decision_records` |
| P9 | `policy/snapshot.py` effective policy hash not versioned — if MEC v17 → v18 mid-flight, in-flight offer still references v17 (correct) but resolver must not return v18 for same `offer_id` — verify |

## Low

- `policy/README.md` duplicates kernel bound concepts without clarifying policy vs kernel ownership — keep policy as "pre-filter + optimizer" framing, kernel as final veto.
