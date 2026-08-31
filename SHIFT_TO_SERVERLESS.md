# Shift to Postgres + Serverless — Detailed Migration Guide

> Goal: Move PRAMAN from `Render + SQLite file` (one writer, sleeps on free) to `Vercel Serverless + Postgres` (stateless, 24×7, auto-scales). Kernel, Vyapaari, MCP, ledger logic stays identical — only `store/db.py` + deploy changes.

---

## 1. Why shift

| Now (Render + SQLite) | Target (Serverless + Postgres) |
|---|---|
| One machine, `data/bazaar.db` WAL, `BEGIN IMMEDIATE` lock. 10 parallel checkouts queue. | Postgres handles 100 parallel checkouts with row locks. |
| Disk on one host (`/var/data` 1GB). Free tier sleeps after 15 min → first request 2-3s wake. | No disk. DB is Neon/Supabase network, API is 0 machines → wakes per request (<1s cold). Free 0.5GB. |
| Laptop can be off, but Render free is not 24×7. | Laptop off, Vercel is 24×7 (serverless pay-per-request). |
| Demo perfect. | Prod path judges want to hear. |

**You keep:** Python FastAPI, `kernel/`, `vyapaari/`, `policy/`, `api/mcp.py` 4 tools, ledger hash chain, 477 tests. **You change:** 2 files + 1 config.

---

## 2. Create Postgres (5 mins, free)

**Option A — Neon (recommended, free forever):**
1. Go to `neon.tech` → Sign in with GitHub → Create Project `praman` → Region Singapore (near Razorpay) → Copy `DATABASE_URL` like `postgresql://neel....neon.tech/praman?sslmode=require`
2. Free: 0.5GB storage, 3 projects, no card. Sleeps never — pooler handles serverless connections.

**Option B — Supabase:** `supabase.com` → New Project → Copy `postgresql://postgres:...@db.supabase.co:5432/postgres`

Keep the URL in `.env` as `DATABASE_URL` (never commit `.env`).

---

## 3. Code changes (2 files)

### 3.1 `store/db.py` — one-line switch

**Before:**
```python
from settings import DATABASE_PATH
def connect(path = None):
    target = Path(path) if path else DATABASE_PATH
    conn = sqlite3.connect(str(target), ...)
    conn.execute("PRAGMA journal_mode=WAL")
```

**After:**
```python
import os, psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get("DATABASE_URL")

def connect(...):
    if DATABASE_URL and DATABASE_URL.startswith("postgresql"):
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        conn.autocommit = False
        return conn
    # fallback SQLite for local dev without DATABASE_URL
    target = Path(...)
    conn = sqlite3.connect(...)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
```

* Add `psycopg2-binary` to `requirements.txt`: `psycopg2-binary>=2.9`
* Keep `PRAGMA` only for SQLite branch. Postgres branch uses `BEGIN` + `SELECT ... FOR UPDATE` where you had `BEGIN IMMEDIATE`.
* `get_connection()` stays thread-local, but serverless is per-request — add at top of each request: `store.db.reset_connection()` in `api/app.py` lifespan not needed; instead make `get_connection` request-scoped (FastAPI dependency).

### 3.2 `api/index.py` — wrapper for Vercel (new file, 3 lines)

Create `api/index.py`:
```python
from api.app import app
from mangum import Mangum
handler = Mangum(app)  # Vercel/AWS calls handler(event, context)
```

Add to `requirements.txt`: `mangum>=0.17`

### 3.3 `store/db.py` helper for serverless connections

Add `conn.execute("SELECT pg_advisory_lock(...)")` where you had `write_lock` threading.Lock for ledger tip read→hash→insert (SQLite single writer lock becomes Postgres advisory lock).

---

## 4. Config changes

### 4.1 `vercel.json` (new at repo root, replaces render.yaml for API)

```json
{
  "functions": {
    "api/index.py": { "maxDuration": 10 }
  },
  "env": {
    "DATABASE_URL": "@database_url",
    "RAZORPAY_KEY_ID": "@razorpay_key_id",
    "RAZORPAY_KEY_SECRET": "@razorpay_key_secret",
    "POLICY_RECEIPT_HMAC_SECRET": "@policy_secret",
    "MANDATE_SIGNING_SEED": "@mandate_seed",
    "GEMINI_API_KEY": "@gemini_key"
  }
}
```

### 4.2 `settings.py`

Add:
```python
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith("postgresql")
```

### 4.3 `render.yaml`

Keep for dashboard only, or delete. API moves to Vercel. Dashboard `dashboard/vercel.json` stays as is (already builds Next.js).

---

## 5. Migrate data (one-time)

```bash
# 1. Export SQLite → SQL dump
sqlite3 data/bazaar.db .dump > dump.sql

# 2. Import to Postgres (Neon provides psql string)
psql "postgresql://.../praman?sslmode=require" < dump.sql

# 3. Verify
psql "postgresql://..." -c "SELECT count(*) FROM ledger; SELECT count(*) FROM products;"
# Should match: ledger 62+, products 14

# 4. Test locally with Postgres before deploy
DATABASE_URL="postgresql://..." python -m pytest -q  # expect 477 passed, 38 skipped
DATABASE_URL="postgresql://..." python -m uvicorn api.app:app --port 8000
# then MCP inspector http://localhost:8000/mcp still works
```

If you skip migration, `python scripts/init_db.py` with `DATABASE_URL` set will recreate genesis + 14 SKUs fresh (loses old orders but demo still works).

---

## 6. Deploy

**Backend (Vercel, serverless, 24×7):**
```bash
vercel --prod   # link to github.com/AyushAnand413/praman, set env vars in Vercel dashboard
# Vercel gives https://praman-api.vercel.app  → MCP at https://praman-api.vercel.app/mcp
```

**Dashboard (Vercel, already):**
```bash
cd dashboard
vercel --prod  # env NEXT_PUBLIC_API_URL=https://praman-api.vercel.app
# → https://aether-audio.vercel.app/panel
```

**ChatGPT MCP:** Settings → Connectors → Add MCP Server → `https://praman-api.vercel.app/mcp`

---

## 7. Testing after shift

```bash
# Hermetic still green (uses Postgres now)
DATABASE_URL="postgresql://..." python -m pytest -q

# Live (real Razorpay, no browser)
python -m pytest tests/test_live_razorpay.py --live-api -v
python -m pytest tests/test_live_checkout.py --live-api -v

# No browser inspector
npx @modelcontextprotocol/inspector https://praman-api.vercel.app/mcp
# List Tools → search_products → Call

# Ledger still verified
curl https://praman-api.vercel.app/audit/verify
# {"intact": true, "head_seq": 63}
```

---

## 8. Rollback plan (if shift fails)

- Revert `store/db.py` to SQLite branch, clear `DATABASE_URL` env, redeploy `render.yaml` (`pip install -r requirements.txt` + `uvicorn`). Ledger file `data/bazaar.db` still exists locally. No code in `kernel/` needed to change.

---

## 9. Cost after shift

| Service | Free tier | After |
|---|---|---|
| Vercel API (serverless) | 100GB-hrs/month | $20 per 100GB extra |
| Neon Postgres | 0.5GB, 1 project | $19 for 10GB |
| Vercel Dashboard | 100GB bandwidth | $20 |
| Render old | 750 hrs sleeps | Delete or keep $0 |

Demo < $0 forever. For pitch say: **"Demo on SQLite + Render, prod path is Vercel serverless + Neon Postgres — one line switch in `store/db.py`, zero kernel change, same 477 tests."**
