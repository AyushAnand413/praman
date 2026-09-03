# Layman Prompt — We Are Having This Issue, Figure It Out and Write Detailed Report

> Copy this whole prompt to any developer or AI. It is in layman shop language. Ask them to find exact bug in code and write a detailed report mentioning the proofs below.

---

## Prompt to Copy-Paste

```
We are having this issue in our shop PRAMAN. Figure it out and write a detailed report.

SHOP (layman):
- Praman is a shop where AI tries to sell and a fixed rule book checks every discount. Front door is https://praman-seven.vercel.app (code in dashboard/app/page.jsx). Warehouse brain is https://praman-brain.vercel.app (code in api/dashboard.py, api/auth.py, store/auth.py, store/db.py, store/ledger.py).

CREDS TO USE FOR TESTING (include in report):
- Login: ayushkumaranand413@gmail.com / #1234$Gada / store default (also test with freshly created prod_browser_...@example.com if needed)
- Shopify Gada: domain gada-electronics-6snotcdl.myshopify.com / token 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 — test via POST /merchant/v1/stores/connect/shopify => should be 202 in 1.93 sec

ISSUE WE SEE:
- User just opens https://praman-seven.vercel.app without typing email. Screen stays black: "Restoring session... First load after idle takes 3-5 sec" → after 12 sec → "Taking too long — database may be waking (12 sec). Retry" + "invalid session — sign in again" with buttons Retry / Sign in again. We did NOT log in, so why invalid session?
- Warehouse logs show many GET 401 for /merchant/v1/dashboard, also 401 for /stores/sync and /orders. POST /auth/signin was 200 earlier. Front logs show only GET / 200/304 — front looks fine, so bug is between front and brain handshake.

WHAT WE ALREADY MEASURED (include in report):
- Health: https://praman-brain.vercel.app/health first 2 polls hung 30 sec, 3rd poll 1.08 sec {"status":"ok","db":"ok","catalog_skus":114,"ledger_head_seq":24} — DB does wake but slow.
- Dashboard via API with fresh token: 19.5 sec then 200 feed 8 — should be <1 sec, but is 19 sec. Browser aborts at 12 sec → shows "database may be waking".
- Shopify connect via browser: POST /stores/connect/shopify => 202 in 1.93 sec + 3 polls 200 — Shopify async fix IS working after merge (old took 49.8 sec).
- Browser sessionStorage after bug: {"store-id":"default","praman-token":"6507..."} — but API test with that exact token gives 401 — token already invalid.
- Front env NEXT_PUBLIC_API_URL is correctly "https://praman-brain.vercel.app", so not CORS.

YOUR TASK — MAIN EMPHASIS IS DATABASE QUERY (19.5 sec) — this is the root cause:
1. Find exact issue in codebase and give file:line for each. Focus FIRST on database query slowness:
   - Why does GET /merchant/v1/dashboard take 19.5 sec (should be <1 sec)? Search api/dashboard.py — what DB queries does it run? Does it scan whole ledger (24 events) + count 114 SKUs + join catalog without cache/index? Check store/db.py, store/ledger.py, store/orders.py. Is it doing N+1 queries?
   - Check store/db.py synchronous=FULL and health query — does DB connection hang first 2 polls (30 sec) then wake at 1.08 sec?
   - Then check session bug: Where front restores session on reload? Search dashboard/app/page.jsx for "Restoring session", "praman-token", "sessionStorage", useEffect on load. Does it send old/invalid token to GET /merchant/v1/dashboard?
   - How brain validates it? Search api/auth.py / store/auth.py get_current_merchant / validate_token — why 401 when token is old or missing?
   - Why 401 loops every 20 sec instead of clearing bad token and showing login? Search dashboard/app/page.jsx polling interval.

2. Write a detailed report (layman + technical) that MUST mention with MAIN EMPHASIS ON DATABASE QUERY:
   - MAIN: Why dashboard DB query is 19.5 sec (health is 1.08 sec) — which query in api/dashboard.py / store/db.py is slow? Must give exact file:line and DB fix (cache/index/limit).
   - Also mention: Restoring session + invalid session screen
   - Warehouse logs GET 401 dashboard loops vs front logs only 200/304
   - Shopify 1.93 sec 202 success (so that fix is not the bug)
   - Token rotation: old token becomes invalid after new login
   - Root cause in one layman sentence + one technical sentence (main cause is DB query)
   - Exact files and lines to fix + what to change in shopkeeper words (main fix is DB query)
   - How to verify fix: what log should look like after (dashboard <1 sec, 200 not 401)

Do not write phase numbers in code comments. Keep report simple enough for shopkeeper but exact enough for coder (file:line).
```

---

## What We Already Measured (so you don't repeat)

- Health after deploy: first 2 polls `000` (hang 30 sec), 3rd poll **1.08 sec `{"status":"ok","db":"ok","catalog_skus":114,"ledger_head_seq":24}`** — DB does wake, just slow.
- Dashboard via API with fresh token: **19.5 sec** then `200 feed 8 orders 1` — should be <1 sec, but is 19 sec (heavy query). Browser aborts at 12 sec → shows waking.
- Shopify connect via browser: **1.93 sec POST 202 SYNC-792... + 3 polls 200** — Shopify fix IS working on prod after merge.
- Token in browser `sessionStorage` after bad reload: `{"store-id":"default","praman-token":"6507..."}` — but API test with that token gave `401 valid X-Merchant-Key or Bearer required` — token already invalid (rotated when same email logged in via API).
- Front code `dashboard/app/config.js` has `API_MISCONFIGURED` check, but prod had correct `NEXT_PUBLIC_API_URL`, so not env bug this time.

## Where to Look First (fastest)

1. `dashboard/app/page.jsx: Restoring session` — around line 80-120, useEffect that reads `sessionStorage.getItem('praman-token')` and fetches `/merchant/v1/dashboard`. If token is present but invalid, it should delete it and show login, not loop 401.
2. `api/dashboard.py: GET /merchant/v1/dashboard` — auth dependency `get_current_merchant` — does it raise 401 when token missing? Check `store/auth.py: validate_token`.
3. `store/db.py` — `synchronous=FULL` and `migrate()` — DB slow 19 sec may be ledger scan of 24 events + 114 SKUs without index.

## Expected Fix in Layman

- If pass is bad → throw it away, show `Sign in` form immediately, don't keep asking warehouse every 20 sec with same bad pass (that's why logs show 5x 401 in 1 minute).
- Make dashboard query faster (<2 sec) by caching `catalog_counts` and limiting `ledger` scan to last 8 only.

---

*This prompt is layman so even a non-tech shopkeeper can run it with AI and get exact file:line.*
