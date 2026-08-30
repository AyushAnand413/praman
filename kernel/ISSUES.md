# Issues — `kernel/`

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


Deterministic money authority — every bug here can be financial.

## Critical

| # | File:line | Issue |
|---|---|---|
| K1 | `kernel/checkout.py:_proceed_to_payment` | `except Exception` lumps `RazorpayError`, `httpx` timeouts, and programming errors into same 502 path — a code bug would be retried as gateway error. Catch `RazorpayError`/`httpx.RequestError` explicitly. |
| K2 | `kernel/checkout.py` mandate scope | `catalog.cache.public(item.sku).get('category','')` returns `''` if SKU missing from public cache — `scope_covers('', ...)` fails for non-wildcard mandates with silent `SCOPE_MISMATCH`. Validate SKU presence before scope check. |
| K3 | `kernel/offer.py:print(...)` | Two `print("PRE-FILTER REJECTED" / "ASSEMBLE REJECTED")` leak to stdout in prod, bypass logging, expose internal counts | 
| K4 | `kernel/payments.py:verify_webhook_signature` | No length check before `hmac.compare_digest`; empty signature returns False but not constant-time. Also calls `secret().reveal()` per request without caching. |

## High

| # | File | Issue |
|---|---|---|
| K5 | `kernel/offer.py:_build_product_context` | Hardcodes `inventory_age_days=30, demand_velocity=1.0, conversion_rate=5.0, return_rate=1.0` — optimizer scoring is mock-driven, not DB-driven |
| K6 | `kernel/gates.py:agent_first_order` | Param exists but caller never supplies it (intentionally disabled). Dead code — could be re-enabled unsafely with unsanitized `agent_id`. Remove or document deprecation. |
| K7 | Docs drift | `settings.py` comment says "nine bounds" but `kernel/bounds.py` implements 10 (relatedness). Update all READMEs to 10. |
| K8 | `kernel/stock.py` + `kernel/saga.py:_ShelfFault` | `_ShelfFault` zeroes stock via raw `UPDATE products` without touching `stock_holds` rows — relies on `commit_settled` tolerant path. If `HOLD_TTL=120s` > `CHECKOUT_ABANDONED_AFTER_SECONDS=1800s` sweep interaction could mis-expire. |

## Medium

| # | Issue |
|---|---|
| K9 | `idempotency` + Bound 9 + schema `Header` — triple check intentional (defense in depth) but Header alias change could silently break one layer — add cross-test |
| K10 | `kernel/approvals.py` no timeout that approves (correct), but pending approvals hold no stock — good, yet dashboard shows them as live risk — clarify UX |
| K11 | `kernel/receipt.py` canonical JSON + HMAC covers verdicts/reasons/gate/totals/exploration — good coverage, but no test for trail length limit |
| K12 | `kernel/search.py` zero-embedding keyword search — fast but synonym expansion is one-way curated dict — missing synonyms silently degrade relevance |

## Low

- `LATENCY_BUDGETS_MS['offer']=3000` vs `GEMINI_TIMEOUT_SECONDS=12` — single LLM call can exceed offer budget; fallback ladder mitigates but tail latency remains 12s. Consider lowering Gemini timeout or raising budget.
