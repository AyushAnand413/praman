# Future Fixes — Parked (do not implement now)

## 1. Remove SQLite entirely — make Postgres the only DB

**Current:** Hybrid. `store/db.py` has two paths:
- `DATABASE_URL` set → Postgres via `psycopg2` + `_PGWrapper` (prod, serverless)
- `DATABASE_URL` unset or `path == ":memory:"` / tmp file → SQLite `bazaar.db` / `:memory:` (local dev + all 477 hermetic tests)

Why hybrid exists: tests are fast hermetic (`:memory:` 0.02s) and don't need a running Postgres. Server can already run on Postgres when env is set, but falls back to SQLite file for demo.

**Future:** Delete SQLite branches entirely. `store/db.py:connect()` should *require* `DATABASE_URL=postgresql://...` and remove all `sqlite3` imports, `PRAGMA journal_mode=WAL` etc., `SCHEMA_SQL` SQLite triggers, and the `:memory:` special case. Tests must then use a test Postgres (Neon free branch or `docker-compose` local Postgres). Steps:
- Add `docker-compose.yml` with `postgres:16` for local dev
- Update `tests/conftest.py` `db` fixture to do `psycopg2.connect(TEST_DATABASE_URL)` instead of `sqlite3.connect(":memory:")`
- Delete `DATABASE_PATH`, `SCHEMA_SQL` SQLite triggers, `journal_mode()` SQLite branch
- Verify `python -m pytest -q` still 477 passed against Postgres (will be slower, needs `TEST_DATABASE_URL`)

**Do not do now** — keep hybrid until you have a free Neon DB and are ready to make `pytest` require it. Stale feeling is intentional: hybrid is the migration bridge, not the final state.

## 2. Use Razorpay MCP for checkout UI (after deploy)

**Plan:** Keep **our** `kernel/payments.py` as truth for now. After `main` is deployed to Render and `localhost:3000` dashboard + `python -m pytest --live-api` (515 passed) are green, try swapping the last mile inside `api/mcp.py:buy`:
- Before: `buy` → `ledger.payment.intent` → `RazorpayClient.create_order` (direct REST) → `ledger.payment.captured`
- After: `buy` → `ledger.payment.intent` → `POST https://agent.razorpay.com/mcp` `initiate_checkout` (or `create_payment_link`) with your `order_id` → their Magic Checkout (UPI Reserve/Circle, no re-entry) → returns same `pay_xxx` → `ledger.payment.captured`

Keep your 10 bounds + ledger wrapping it, so you still show `Audit report Paid ₹399, Verify chain ✓` + `saga compensation` failure demo. If it works, keep it; if not, revert one line.

**Cons of using theirs (why we delay):**
- Extra hop → +50–100ms, depends on `agent.razorpay.com` uptime (your direct REST is already 20 passed in `tests/test_live_razorpay.py`).
- Local `localhost:8000/mcp` needs `ngrok`/Tunnel for ChatGPT to reach it; their hosted MCP is already public but you lose a bit of control.
- No real downside for the bar — you still own the audit trail because you log before/after. Delay just keeps demo stable first.

## 3. Other parked items
- MCP auto-routing by `need` keywords (earbuds → aether-audio, laptop → voltmart) — currently hardcoded demo.
- Shopify pagination `Link` header vs `since_id` — fine for demo, fix before 5k SKU prod.
