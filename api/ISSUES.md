# Issues — `api/` 

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


## Critical

| # | File:line | Issue | Impact | Fix |
|---|---|---|---|---|
| A1 | `api/dashboard.py:require_merchant_key` | Uses `==` not `hmac.compare_digest` (timing side-channel). `api/approvals.py` does it correctly. | Key brute-force slightly easier | Copy `approvals.py` constant-time pattern |
| A2 | `api/app.py:cors allow_headers=["*"]` | Header wildcard contradicts "dashboard origin only" comment | Over-permissive CORS | Restrict to `["Content-Type","Authorization","X-Merchant-Key","X-Demo-Key","Idempotency-Key"]` |
| A3 | `api/ratelimit.py` | In-memory `FixedWindowLimiter` per process — resets on restart, not shared across workers, keyed on unauthenticated `agent_id` | Burst bypass under multi-worker deploy | Move to Redis/edge limiter for prod |

## High

| # | File | Issue |
|---|---|---|
| A4 | `api/app.py:lifespan` | Missing env vars only warn, not fail. Live mode could run in shadow silently. |
| A5 | `api/agent.py:catalog_query` | `narrowed or rows` and `matched or unranked` return full catalog on nonsense query — masks client bugs + scraping risk |
| A6 | `api/webhooks.py` | Returns 200 for unknown events/unknown orders (avoids retry storm but hides integration errors). Amount mismatch only logged. |
| A7 | No rate limit on `/offer` and `/checkout` | LLM prompt + checkout burst possible. Justified by cost today but should have at least coarse limit. |

## Medium

| # | Issue |
|---|---|
| A8 | `MAX_CATALOG_RESULTS=10` hardcoded in `api/agent.py` vs 14 SKUs — growth needs settings-driven value |
| A9 | Ledger `append` on free `/catalog` has no try/except — ledger failure 500s a free endpoint |
| A10 | `dashboard.py:_metrics` sums `charged = FAILED+VOIDED+CAPTURED` then subtracts refunds — drifts if new order state added |
| A11 | `api/mcp.py` `build_server()` held on `app.state` correctly (per-app), but no test for concurrent `TestClient` apps sharing transport |
| A12 | `api/demo.py` `POST /demo/force_oversell` requires `DEMO_KEY` + live-mode gate — correct, but `X-Demo-Key` header validation duplicates merchant-key logic without compare_digest |

## Low / Nits

- `api/__init__.py:1` is just a package marker but documents DB→HTTP single-serializer rule well — keep.
- `panel/index.html` is minimal SPA shell — no CSP meta tag.