# Issues — `scripts/` 

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


## High

| # | File | Issue |
|---|---|---|
| Sc1 | `scripts/_env.py` hand-rolled parser | Mishandles quoted `#` / `export` prefix / multiline values — secret with `"` or `#` truncated silently → wrong Razorpay key with no error. Consider `python-dotenv` or harden parser. |
| Sc2 | `scripts/tamper_demo.py:--in-place` | Permanently tampers `data/bazaar.db` with no confirmation prompt beyond flag — easy to run accidentally and break chain. Require `--yes` + auto-backup. |
| Sc3 | `scripts/checkout_live.py:--live` | No pre-check that `POLICY_MODE=live` and `rzp_test_` key present before placing order — could capture in shadow as `would_have_charged` confusingly |
| Sc4 | `scripts/run_ab.py` 400 sessions | Uses isolated tempfile DB so pairing learning not persisted; also persona rotation hardcoded, not configurable |

## Medium

| # | Issue |
|---|---|
| Sc5 | `scripts/init_db.py` additive-only — cannot drop columns/tables; schema evolution beyond additive needs manual sqlite3 intervention, no down migration |
| Sc6 | `scripts/anchor_chain.py` `verify` scans linearly O(n*m) — no index on anchor hash |
| Sc7 | `scripts/_env.py` is script-only per `CLAUDE.md` (app uses `os.environ`) — correct, but no CI guard prevents prod startup via `scripts._env` loader |
| Sc8 | `scripts/razorpay_smoke.py` two-step (create → browser → capture) splits across invocations — no state file, user must manually paste `order_id` |

## Low

- `scripts/_console.py:use_utf8_stdout()` correctly handles Windows cp1252 — keep in any new script printing `₹`/box-drawing.
- All scripts correctly use `scripts/_env.py` not app secrets path.