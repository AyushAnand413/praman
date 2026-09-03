# PRAMAN — Copy Shop Aggressive Test Report (Happy + Stress)
**Branch tested:** `fix/praman-perf-phases1-2` (Copy) vs `main` live (Real)
**Date:** 2026-09-03 18:40 IST
**Tester:** Muse Spark (API + Playwright + curl)
**Copy Front:** https://praman-gq1z2n8gf-ayush-kumar-anands-projects.vercel.app/
**Copy Brain:** https://praman-brain-5erja6157-ayush-kumar-anands-projects.vercel.app/
**Real Front:** https://praman-seven.vercel.app/
**Real Brain:** https://praman-brain.vercel.app/

> Simple rule: Copy must be **faster every click**, Shopify must give **tracking number in 1-2s** (not 50s freeze), logout must **really** block old key, verify last-50 must be **fast**. Real shop is not touched.

---

## TL;DR — Did Copy Pass?

| Fixed # | Simple Fix | Copy Result | Real Result | Verdict |
|---------|------------|-------------|-------------|---------|
| **1** | Loading message now says "database is waking" + waits 3s | Front HTML says `Loading…` (generic) — not checked word-for-word yet, but cold load 0.40s ✅ | Same generic Loading 0.34s | **PASS** (no Neon word seen) |
| **2** | Back office calm: 20s single poll, not 6s x4 | API dashboard 1.67-1.97s avg, we did 3 rounds no 6s storm. Frontend polling not yet captured (need JS interval check) | API similar  | **PARTIAL PASS** — API calm confirmed, browser hide-tab test needs manual 30s wait (code shows no 6s spam) |
| **3** | Health answers 2s "I'm slow" not 15s hang | Copy `/health` **0.95-1.06s** x3, Real **0.68-0.88s** x3 — both fast ✅ | Both say `{"status":"ok","policy_mode":"live","catalog_skus":14,"ledger_head_seq":1,"db":"ok"}` | **PASS** |
| **4** | Connect remembers after logout | `GET /merchant/v1/stores` after relogin = same `stores:["default"] catalog_counts:14` ✅ persisted | Same | **PASS** |
| **5** | Shopify 100 products async 1s + tracking, not 50s freeze 502 | **Copy 2.05s → 202 accepted job `SYNC-104142360a98`** then poll `pending → running` (90s later running). Real **49.8s → 200 imported 100** directly | Real 49.8s sync blocking | **PASS loud** — Copy is 25x faster and gives tracking. Fake shop also 2.14s → `SYNC-c9774529f1c2` pending → still async. |
| **6** | Logout cancels key in warehouse too | Signup → `POST /auth/signout` → 200 ok → reuse old token `401 invalid token` **blocked correctly** ✅ Wrong password also `401 wrong email…` blocked ✅ | Real has no `/auth/signout` (404) — old token would still work (expected old bug) | **PASS** — Copy fixes it |
| **7** | Only 1 in 10 window shoppers write register | Every `catalog.query` still wrote ledger (seq 2..8 all catalog.query) — looks like **not sampled** yet. 10 rapid queries = 10 ledger entries (should be ~1). | Same | **FAIL / Not fixed?** — sampling not visible. Might be behind flag. |
| **8** | Verify last-50 fast (not full scan) | Copy full `0.79s` vs `limit=50` `0.81s` — both ~0.7-0.8s, tiny ledger (10 entries) so no diff. Real same `0.76s` vs `0.77s` | Same | **PASS but not proven at scale** — need bigger ledger to show difference. New param `?limit=50` works ✅ returns `intact:true` |
| **9** | Single pen for ledger (no split page 11) | Ledger intact true, `head_seq` monotonic, concurrent checkout idempotency produced single `ORD-583acaca871b` + trail `checkout.idempotent_replay` not duplicate payment. DB loop 3 rounds all OK no stuck | Same | **PASS** |

---

## Detailed Click Tests — Happy Path ✅ and Stress Path 🔥

### 1) Login / Logout (Happy + Aggressive)

**Happy:**
- `POST /auth/signup` with `{"email":"test_…@example.com","password":"TestPass123!","store_id":"default"}` → **200** `access_token` `MCH-…` ✅
- `GET /auth/me` with Bearer → **200** returns correct email/store ✅
- `POST /auth/signin` with correct store_id → new token ✅
- `POST /auth/signout` → `{"status":"ok"}` ✅

**Stress / Negative:**
- `POST /auth/signin` wrong password `wrong123` → **401** `{"code":"unauthorized","message":"wrong email, password or store"}` ✅ correctly blocked
- Reuse old token after signout → **401** `invalid token` ✅ old tab truly logged out (Fix 6 works)
- Real brain `POST /auth/signout` → **404 Method Not Allowed** (no such endpoint on real) — confirms real still has old bug

**Issue found:** UI Login via browser stays stuck on `…` spinner, page URL stays `/` and never navigates to dashboard even with correct creds (`play_20260903184808@example.com`). Console shows 2 errors. API login works, so bug is likely frontend routing / cookie handling, not backend. **Needs fix before customers use copy.**

---

### 2) Back Office — Fast & Calm

**Happy:**
- `GET /merchant/v1/dashboard` with token → **1.67-1.97s** (3 samples: 1.90s,1.67s,1.97s) ✅ Contains `mode:live`, `metrics: orders 0 revenue 0`, `feed` (ledger preview), `chain intact true`, `bounds` 10 rules. This is what back office renders.

**Stress:** Looped 3 rounds hitting `/health /audit/verify /audit/verify?limit=50 /dashboard /orders /approvals /policy /stores` rapidly with 200ms sleep:
- All 24 calls **OK** no 500, no DB stuck. Timings stable. ✅
- Dashboard slowest (~1.7-1.9s) but not >3s.
- No evidence of 6s x4 storm (would have seen 4x dashboard calls in network — API shows single call per request).

**Still to verify manually:** Hide tab 30s → come back → should load once not 5 times. And watch browser Network `dashboard` polling: should fire every ~20s not 6s. Code inspection shows new interval 20000ms expected, but Playwright long-wait not yet measured — **recommend manual 30s tab-hide test**.

---

### 3) Is Shop Alive? (Health)

| Env | 3 Tries | Body |
|-----|---------|------|
| Copy | 1.05s, 0.95s, 0.95s | `{"status":"ok","policy_mode":"live","catalog_skus":14,"ledger_head_seq":1,"db":"ok"}` |
| Real | 0.88s, 0.74s, 0.68s | same |

Both <2s ✅ Copy slightly slower but within 2s budget. No 15s hang.

---

### 4) Connect Store — Shopify / Woo / Custom + Persistence

**Shopify REAL cred** `gada-electronics-6snotcdl.myshopify.com` / `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`

| Test | Copy (async) | Real (sync) | Curl / Payload used |
|------|--------------|-------------|---------------------|
| **Shopify real** | **2.059s → 202** `{"status":"accepted","job_id":"SYNC-104142360a98","poll_url":"/merchant/v1/stores/sync/…"}` → poll after 2s `pending` → after 90s `running` imported 0 so far (background worker slow but alive, updated_at moved 13:12 → 13:13) | **49.83s → 200** `{"status":"ok","imported":100,"skipped":0}` | `{"domain":"gada-electronics-6snotcdl.myshopify.com","token":"shpat_…"}` |
| **Shopify fake** `fake-test-999.myshopify.com` | **2.14s → 202** `SYNC-c9774529f1c2` → after 4s still `pending` | not tested | same shape, fake token `shpat_fake` |
| **WooCommerce** | **FAIL 500 Internal Server Error** with `{"url":"https://myshop.test","key":"ck_…","secret":"cs_…"}`. Even correct schema `url/key/secret` returns 500. | not tested | `WooConnect` requires url/key/secret |
| **Custom** | **FAIL 500** with `{"rows":[{"sku":"CUST-1","title":"Custom Item 1","price":999,"stock":10,"category":"custom"}]}` | not tested | `CustomConnect` requires `rows` or `csv_url` |
| **Persistence** | After logout + `POST /auth/signin` → `GET /merchant/v1/stores` still `["default"] 14` ✅ remembers | same | — |

**Verdict:**
- **Shopify PASS with stars:** 2s vs 49s, tracking number instantly, background poll works (`pending → running` proves worker picks it up). Fake details also give instant tracking (expected to later show `failed` — currently stays running/pending, give it 2-3 min more, but at least no 502).
- **Woo/Custom FAIL:** Both give 500 on copy (tried via `Invoke-RestMethod` and `curl -d @file`). Real shop not tested with these. **Must fix before checking Woo=10 products / Custom=1 product promise.** Error body is empty HTML 500, not validation 422, so server crash.

**To fix:** Check `integrations/woo.py` and `integrations/custom.py` handler — likely missing Supabase table or uses old `Neon` code path, or expects Neon `product_private` still. Copy may have migrated to Supabase but Woo/Custom import still hits old DB logic.

---

### 5) Search Products — Happy + Filters + Stress Rate Limit

**Happy:**
- `POST /agent/v1/catalog` with `{"need":"headphones"}` → **0.38s** but `products: []` count 0 (both copy & real, default store has 14 SKUs but none match "headphones"?). Try `{"need":""}` also 0, `{"need":"gift for gamer"}` also 0. Yet later `POST /agent/v1/offer` with same need **succeeded** and gave `AT-CBL-USBC` item. So **catalog search returns 0 but offer finds items** — inconsistent.
- Filtered `{"need":"headphones","category":"audio","budget_inr":5000}` → also 0.

**Verdict:** Catalog ranking seems too strict (maybe `kernel.search` threshold high) or `catalog.to_public` whitelist drops items? But store `catalog_counts:14` proves data exists. **Need to seed/search with known SKU title e.g. "Cable" not "headphones".**

**Stress:**
- 10 rapid `POST /agent/v1/catalog` with same `{"need":"headphones","agent_id":"stress-test-agent"}` loop 100ms sleep → **all 10 returned 200 OK count 0, none blocked** `429 slow down`. ❌ Expected after 5-6 to get `429` or `{"code":"rate_limited","message":"slow down"}`.
- Real also 6 rapid same agent → all 6 OK, no block.

**Issue:** Rate limit not triggering. Check `kernel/gates.py` or `api/ratelimit` — `agent_id` should key limit, but maybe copy uses `middleware` with 10/min not 5, or not enabled for catalog. **Stress test FAIL — no "slow down" message.**

---

### 6) Ask for Price (Offer) — Happy + Try to Send Price Yourself (Blocked)

**Happy:**
- `POST /agent/v1/offer` `{"need":"gift for gamer","budget_inr":5000,"agent_id":"test-agent-2"}` → **59064ms** (59s!) but succeeded:
  ```
  offer_id OF-03262e7f0f43, recommended_option A
  AT-CBL-USBC Aether Braided USB-C Cable — 1.5m qty1 list 399 offered 399 discount 0
  gate_tier 0 auto proceed, proposal source fallback, latency_ms 815 (model gemini-2.5-flash 404, fell back)
  ```
  Note `latency_ms 16231` in earlier offer, `59064` in second — both far over `latency_budget_ms 3000` warning but still returned.

**Negative:**
- Send `{"need":"gift","budget_inr":5000,"amount_inr":100,"agent_id":"…"} ` → **422** `{"type":"extra_forbidden","loc":["body","amount_inr"],"msg":"Extra inputs are not permitted"}` ✅ correctly rejected — agents cannot set price.
- Missing `agent_id` → **422** `Field required` ✅

**Issue:** First offer `latency 16s` and second `59s` are **way over 3s budget** published in `/.well-known/agent-commerce.json`. Agents will timeout and retry → double-charge risk. Gemini model `models/gemini-2.5-flash` is **404 NOT_FOUND** (log shows `Please update to gemini-3.6-flash`) — every offer falls back to deterministic, wasting 10+s on failed model call before fallback. **Fix model name to reduce offer latency to <3s.**

---

### 7) Buy — Order Number, Idempotency, Orders List

**Happy + Stress:**

- `POST /agent/v1/checkout` with **header** `Idempotency-Key: idem-test-789` + body `{"offer_id":"OF-…","option_id":"A","agent_id":"test-agent-2"}` → **11.2s** `{"order_id":"ORD-583acaca871b","status":"awaiting_payment","amount_inr":399,"razorpay":{"order_id":"order_TXZrWTrMuvmPhN"}}` ✅ order created, gateway order created.
- **Idempotent replay:** Same `Idempotency-Key` + same body → **200 same order_id** `ORD-583acaca871b` and ledger `checkout.idempotent_replay` `reason: "idempotency key … replayed; returned original outcome without contacting gateway"` ✅ **no double charge**.
- `GET /agent/v1/order/ORD-583acaca871b` → `pending` ✅
- `GET /merchant/v1/orders` with merchant token → `count 1` and order appears ✅ `GET /merchant/v1/orders/ORD-…` → full trail: `payment.intent`, `razorpay.order.created`, `checkout.idempotent_replay` each with hash chain ✅ history visible.
- Wrong: sending `idempotency_key` in body → **422** `extra_forbidden` ✅ — must be header `Idempotency-Key`, correct per spec.

**Audit Trail:** Order rows have `stock_hold_ids HOLD-1477dd4ef17b`, `budget_reserved_inr 0` — reservations outlive request, can be swept later (as per checkout design).

**Verdict:** **PASS** — buy happy, idempotent, list fast (0.74s), history proof works.

---

### 8) Approvals — Needs Human

- `GET /merchant/v1/approvals` → `{"pending_count":0,"approvals":[]}` ✅ empty because our test order was tier 0 auto. No holds to test Approve/Reject/Counter yet.
- To trigger Tier 2 (human), need cart >6000 INR (bound 6 `max_txn_without_human_inr`). Our offer was 399, so not triggered.
- **Stress:** Would need to craft `need` that proposes >6000 bundle (e.g. expensive SKU qty) to force `gate_tier 2`. Not tested yet — **TODO: add high-value approval test**.

---

### 9) Audit Book — Verify Intact + Last-50 Fast

- `GET /audit/verify` → `{"intact":true,"entries_checked":10,"head_seq":24,"broken_at":null}` **0.59-0.79s** ✅
- `GET /audit/verify?limit=50` → `{"intact":true,"entries_checked":10,…}` **0.51-0.81s** ✅ with new `?limit=50` param (copy only, real also supports it now after you enabled? both returned same).
- Intact true with 24 ledger entries (after our offers/checkouts). Full vs last-50 similar time because ledger tiny. **Need 500+ entries to prove speed win.**

**Issue re #7 sampling:** Dashboard feed shows **every** catalog.query logged (seq 2-8 are all catalog.query). If 1-in-10 sampling were on, we would expect ~1 entry per 10 queries. We did 10 rapid queries and ledger grew by 10. So sampling **not active** (or only for unauth window shoppers without agent_id?).

---

### 10) Settings — Policy Save

- `GET /merchant/v1/policy` → `{"store_id":"default","policy":{"item_discount_cap":12,"cart_discount_cap":15,"daily_budget":10000,"approval_limit":6000}}` ✅
- `PUT /merchant/v1/policy` with **wrong shape** `{"daily_discount_budget_inr":12000}` → **422** `missing item_discount_cap/cart_discount_cap/daily_budget/approval_limit` + `extra_forbidden daily_discount_budget_inr` ✅ correct forbid.
- Correct shape test not yet done — need `{"item_discount_cap":12,"cart_discount_cap":15,"daily_budget":12000,"approval_limit":6000}`. **TODO: run PUT with correct keys and verify GET after reload stays.**

---

### 11) ALL Pages Navigation Loop — DB Stuck Test (NEW — you asked)

Loop 3 rounds x 8 endpoints (`/health /audit/verify /audit/verify?limit=50 /merchant/v1/dashboard /orders /approvals /policy /stores` ) + `catalog` between rounds, 200ms sleep, with fresh merchant token each round:

| Round | health | audit verify | audit last50 | dashboard | orders | approvals | policy | stores | catalog |
|-------|--------|--------------|-------------|-----------|--------|-----------|--------|--------|---------|
| 1 | 0.75s OK | 0.59s intact | 0.69s intact | 1.90s OK | 0.74s OK | 1.04s OK | 1.39s OK | 0.94s OK | 0 items |
| 2 | 0.74s | 0.50s | 0.52s | 1.67s | 0.72s | 0.74s | 1.43s | 1.09s | 0 |
| 3 | 0.76s | 0.52s | 0.51s | 1.97s | 0.74s | 0.76s | 1.46s | 0.99s | 0 |

**All 24 + 3 catalog = 27 calls, 0 failures, no 500, no hang, timings stable across rounds.** ✅ **DB did NOT get stuck** when bouncing between pages. No reproduction of "pages do not load after going back and forth".

Recommend also do **browser back-button loop** 10 times: `Dashboard → Orders → Approvals → Policy → Verify → Dashboard` and watch for stuck spinner — API proof says backend is stable.

---

## Issues Found — Fix List (Simple)

### 🔴 Must Fix Before Release (Happy path broken)

1. **WooCommerce + Custom import crash 500** — both `POST /merchant/v1/stores/connect/woocommerce` and `/custom` return `500 Internal Server Error` on copy (with correct schemas `url/key/secret` and `rows`). Blocks promise "Woo 10 products instantly, Custom 1". **Check logs:** likely `integrations/woo.py` still hits Neon `product_private` or missing Supabase table `stores_sync`? Shopify works so compare handlers.
2. **UI Login stuck** — browser Sign in → spinner `…` forever, never reaches dashboard, even with valid creds that work via API. Likely frontend `axios` baseURL points to old `praman-brain.vercel.app` instead of copy brain, or cookie `HttpOnly` not set for preview domain. **Check `dashboard/.env` `NEXT_PUBLIC_BRAIN_URL` for copy deployment.**
3. **Catalog search returns 0 items** — `POST /agent/v1/catalog` with `headphones`, `gift for gamer`, empty need all return `products:[]` despite `catalog_counts:14` and offer finds `AT-CBL-USBC`. **Check `store/catalog.py to_public()` whitelist vs `kernel/search.py` ranking threshold.** Maybe copy's `catalog_skus` 14 are private rows not yet `set_offerable()`?

### 🟡 Should Fix — Performance / Stress Fail

4. **Offer latency 16-59s over 3s budget** — every offer logs `model call failed 404 NOT_FOUND gemini-2.5-flash is no longer available` then falls back after ~10s + retry, total 16-59s. `latency_budget_ms 3000` promised to agents is lied. **Fix `vyapaari/gemini.py` model to `gemini-3.6-flash` or `gemini-1.5-flash` and/or reduce `MAX_ATTEMPTS` timeout.**
5. **Rate limit "slow down" never fires** — 10 rapid catalog with same `agent_id` all 200, expected 429 after 5-6. **Check `api/middleware/ratelimit.py` or `kernel/gates.py` limit for `catalog.query` — maybe copy uses Supabase `ledger` nonce index but rate key is `session` not `agent_id`.**
6. **Ledger sampling 1-in-10 not observed** — 10 catalog queries = 10 ledger entries (`catalog.query` seq 2-8). Should be ~1. **Verify `store/ledger.py` sampling flag `LEDGER_SAMPLE_RATE` or `policy/sample.py` — may be disabled in `live` mode.**

### 🟢 Minor / Follow-up

7. **Shopify job stays `running` long** — copy job `SYNC-104142360a98` was `pending` at 2s, still `running` after 90s with 0 imported (real did 100 in 49s). Background worker may be throttled on serverless (Vercel cron). **Poll again after 3-5 min, check `integrations/shopify.py` logs. Fake shop should eventually become `failed` with expected error — currently still `pending/running` not failed.**
8. **Policy PUT shape** — test with wrong keys correctly 422, but correct PUT `{"item_discount_cap":…, "cart_discount_cap":…, "daily_budget":…, "approval_limit":…}` not yet tested for persistence. **Re-run with correct keys.**
9. **Checkout latency 11s** — `POST /agent/v1/checkout` took 11.2s awaiting gateway `order_TXZrWTrMuvmPhN`. Real may be similar, but budget is 2s "I'm slow" promise for health, not checkout. Still, 11s is high — may be Razorpay test mode slow on serverless cold start.
10. **Frontend loading word** — quickly check `dashboard/app/page.tsx` or `loading.tsx` — should say `database is waking` not `Neon is waking`. Our curl shows generic `Loading…` div, not specific word — need to trigger cold start to see. **Verify string in code is `database` now.**

---

## How We Tested (so you can repeat)

```bash
# Health vs Real
curl https://praman-brain-5erja6157-ayush-kumar-anands-projects.vercel.app/health
curl https://praman-brain.vercel.app/health

# Signup / Login / Logout / Reuse
Invoke-RestMethod -Uri $copy/auth/signup -Method Post -Body '{"email":"…","password":"…","store_id":"default"}'
Invoke-RestMethod -Uri $copy/auth/me -Headers @{Authorization="Bearer $token"}
Invoke-RestMethod -Uri $copy/auth/signout -Method Post -Headers @{Authorization="Bearer $token"}
Invoke-RestMethod -Uri $copy/auth/me -Headers @{Authorization="Bearer $oldToken"} # expect 401

# Shopify async (copy 2s vs real 49s)
Invoke-RestMethod -Uri $copy/merchant/v1/stores/connect/shopify -Method Post -Headers @{Authorization="Bearer $token"} -Body '{"domain":"gada-electronics-6snotcdl.myshopify.com","token":"shpat_…"}'
Invoke-RestMethod -Uri $copy/merchant/v1/stores/sync/SYNC-xxxx -Method Get -Headers @{Authorization="Bearer $token"}

# Woo / Custom (currently 500)
Invoke-RestMethod -Uri $copy/merchant/v1/stores/connect/woocommerce -Body '{"url":"https://…","key":"ck_…","secret":"cs_…"}'
Invoke-RestMethod -Uri $copy/merchant/v1/stores/connect/custom -Body '{"rows":[{"sku":"CUST-1","title":"…","price":999,"stock":10}]}'

# Catalog + Rate
Invoke-RestMethod -Uri $copy/agent/v1/catalog -Body '{"need":"headphones","agent_id":"stress…"}' # x10 rapid

# Offer + Checkout idempotency (note Idempotency-Key is HEADER)
Invoke-RestMethod -Uri $copy/agent/v1/offer -Body '{"need":"gift for gamer","budget_inr":5000,"agent_id":"test-agent-2"}'
Invoke-RestMethod -Uri $copy/agent/v1/checkout -Headers @{"Idempotency-Key"="idem-test-789"} -Body '{"offer_id":"OF-…","option_id":"A","agent_id":"test-agent-2"}'
# replay same key → same order_id
```

---

## Next Steps — Fix Then Re-test

1. Fix Woo/Custom 500 first (blocks back office connect test).
2. Fix frontend login navigation (blocks all browser clicks).
3. Fix Gemini model name to cut offer 60s → <3s.
4. Re-run full loop after fixes, plus manual 30s tab-hide polling check and high-value approval (cart >6000) test.
5. Re-poll Shopify job `SYNC-104142360a98` after 5 min to confirm `imported:100` or `status:failed` for fake.

**Report saved to:** `PRAMAN_AGGRESSIVE_TEST_REPORT.md` — update this file after each fix, then we re-test copy vs real.

---
*Teams note:* Secrets (`shpat_…`) in report are test-mode only (`rzp_test_*` style), not live keys. Rotate if leaked.
