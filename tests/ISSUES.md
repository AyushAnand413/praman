# Issues — `tests/` 

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


41-file hermetic suite (282 passed, 38 live skipped).

## What this folder does (simple)

Proves the system is safe without needing real keys/network. Each file tests one safety property.

- `conftest.py` — creates temp SQLite DB, `TEST_SECRETS`, `FakeRazorpay` (never hits internet), `ForbiddenRazorpay` (proves shadow never charges)
- `test_bounds.py` + `test_gates.py` — 10 bounds + 3 gate tiers at 100% line coverage
- `test_checkout.py`, `test_saga.py` — 11-step money path + compensation saga
- `test_import_boundary.py` — AST walk proving `vyapaari` can never import `kernel.payments`
- `test_no_private_leak.py` — crawls all routes, asserts `cost_inr`/`floor_price_inr` never leak

Connected to: every other folder (tests import them).

## Gaps

| # | File | Issue | Fix |
|---|---|---|---|
| T1 | `tests/conftest.py` + coverage gate | `bounds.py`/`gates.py` held at 100% line coverage, but `kernel/checkout.py` (1300 lines, money path) is not | Add `kernel/checkout.py` to coverage gate (start at 85%+) |
| T2 | `tests/test_no_private_leak.py` | Checks field names only, not `attrs` JSON contents — Shopify `attrs` could embed cost and leak undetected | Deep-scan `attrs` values for `cost`/`margin` substrings |
| T3 | `tests/test_import_boundary.py` | Catches static `import` but not dynamic `__import__('kernel.payments')` / `importlib` | Add runtime `sys.modules` guard in `vyapaari` |
| T4 | `tests/conftest.py:live_api` | `live_api` marked tests deselected by default (correct hermetic), but `pytest.ini` turns `DeprecationWarning` → error for `store/kernel/api` — future dep warning could fail suite as if code bug | Pin warning filter to specific modules or `ignore::DeprecationWarning:pkg` |
| T5 | Hermetic vs live parity | `FakeRazorpay` has no webhook HMAC path — hermetic suite never exercises `api/webhooks.py` HMAC failure mode; only `live_api` does | Add unit test with HMAC-signed `FakeRazorpay` event |
| T6 | `tests/README.md:3` | First line says "write_to_file to `c:\Users\KIIT\Downloads\New folder\tests\README.md`" — stale generator path | **Fixed** in this pass |

## Good patterns to keep

- `tests/test_kernel_catches_llm.py` — hallucinated SKU/negative price/excessive discount/prompt injection all vetoed with bound IDs
- `tests/test_ledger.py` — genesis `prev_hash="0"*64` + hash chain verify
- `tests/test_mandate.py` — 8-step mandate checks in order cheapest-first