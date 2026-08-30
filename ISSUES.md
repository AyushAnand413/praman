# Issues — Repository Root (`/`)

> **Fixed in this pass (2026-08-29):** Critical/high items addressed — constant-time auth, CORS tighten, print removal, checkout error narrowing, catalog attrs sanitize, tenancy cache, duplicate generator removed, mandate length cap, dashboard visibility pause + error handling, gitignore expanded, structure cleaned. Remaining medium/low items documented above remain open for next pass.


Project-level gaps found by scanning `PRAMAN_2.0_Architecture.md` vs codebase.

## High

| # | Issue | Detail | Fix |
|---|---|---|---|
| R1 | No root README was present | Fresh clones have no entry point | **Fixed** — created `README.md` with folder map + 7-component diagram |
| R2 | Git root is `C:\Users\KIIT` (home dir) not `praman/` | `git add .` would stage entire user profile | Move repo or `git init` inside `praman/`; add explicit `.gitignore` at new root |
| R3 | No linter/formatter | `kernel/offer.py` has `print("PRE-FILTER REJECTED" ...)` leaking to stdout; no CI catches it | Add `ruff` + pre-commit or at least `grep -r "print("` in CI |
| R4 | Env bootstrapping fragility | `.env` is gitignored (correct) but missing secrets only warn at `api/app.py` lifespan; live deploy could silently run in shadow | Make `/health` expose `policy_mode` + missing-secret flag, fail boot in live mode |

## Medium

| # | Issue | Detail |
|---|---|---|
| R5 | Two economic vocabularies — `settings.py` frozen floats (`1.20×cost`, `MAX_DISCOUNT=12%`) vs `policy/mec.py` `HardConstraints` (`min_margin_pct`, `max_discount_pct`) | Risk of divergence; need single source or cross-check test |
| R6 | `PRAMAN_2.0_Architecture.md` documents 7 components + 8 invariants, but `CLAUDE.md` describes 9/10 bounds inconsistently | Docs drift: update `CLAUDE.md` and `api/README` bound count to 10 |
| R7 | `catalog.json` vs `settings` limits not linked | `MAX_CATALOG_RESULTS=10` vs 14 SKUs — growth needs bumping ceiling |

## Low

| # | Issue | Detail |
|---|---|---|
| R8 | `opencode.json` present but no `customize-opencode` skill docs | Minor — document opencode agents if team uses them |
| R9 | `data/bazaar.db` (302KB) committed on disk with WAL | Gitignored correctly but no backup/anchor schedule — `scripts/anchor_chain.py` is manual |

## What to do next

1. Stabilize git root
2. Add `python -m pytest --cov=kernel.bounds --cov=kernel.gates` gate in CI + `grep print(` guard
3. Unify MEC vs settings constants with a bridging test `test_settings_mec_parity.py`