# Issues — `store/` 

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


Hash-chained persistence + public/private split.

## Critical

| # | File | Issue | Fix |
|---|---|---|---|
| S1 | `store/ledger.py` triggers | `BEFORE UPDATE/DELETE` triggers block edits, but attacker with DB write can `DROP TRIGGER` then rewrite chain | Schedule `scripts/anchor_chain.py` + external anchor verification; consider SQL `PRAGMA writable_schema=OFF` hardening |
| S2 | `store/catalog.py:to_public()` whitelist | `attrs` JSON field is passed through verbatim — if Shopify `attrs` contains private data, it leaks. `tests/test_no_private_leak.py` checks field names, not `attrs` contents. | Sanitize or whitelist `attrs` keys; extend leak test to deep-scan `attrs` |
| S3 | `store/db.py:write_lock` | `threading.Lock` protects intra-process tip→hash→insert; inter-process contention relies on `BEGIN IMMEDIATE` → `SQLITE_BUSY` with no retry | Add busy-handler retry loop (3× with backoff) |

## High

| # | Issue |
|---|---|
| S4 | `store/db.py:TABLES` tuple lists 14 names but `SCHEMA_SQL` creates 15 tables — drift if `TABLES` used for init check |
| S5 | `store/pairings.py` decay is lazy on write only — stale pairs never decay until touched, long-idle SKUs retain inflated strength |
| S6 | `store/ledger.py:trail()` does `OR` over 4 `json_extract` — full table scan. Noted as "honest at this scale" but will not scale |
| S7 | `store/tenancy.py:cluster_for_store()` parses `PRAMAN_STORE_CLUSTER_MAP_JSON` per-call, no cache/validation at startup — malformed JSON throws at runtime |
| S8 | `store/db.py` `migrate()` `ADDED_COLUMNS` tracks only 3 columns — if new columns added elsewhere without updating map, `ALTER TABLE` never runs |
| S9 | `store/orders.py:ALLOWED_TRANSITIONS` — webhook `refund.processed` after saga `REFUNDED` returns `already` vs `stale` ambiguity |

## Medium

| # | Issue |
|---|---|
| S10 | `store/canonical.py` rejects `Decimal`/`set`/`NaN` — correct strictness, but MEC hashing with `Decimal` fields must stringify first |
| S11 | `store/catalog.py:seed_database_from_rows` batch abort leaves cache stale vs DB on partial rollback |
| S12 | `store/db.py` `PRAGMA synchronous=FULL` is safest for money data but fixed — no env override for deploy tuning |
| S13 | `store/tdr_store.py` has no SQL triggers preventing `UPDATE/DELETE` of `transaction_decision_records` unlike `ledger` — TDR could be mutated silently |

## Low

- `store/timestamps.py` `utc_now()` with microsecond lex-sort works, but `plus_seconds()` addition doesn't clamp to valid ISO boundary — minor.
