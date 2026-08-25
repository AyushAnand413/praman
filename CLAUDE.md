# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

BAZAAR / "Aether Audio" — a merchant-side agent-commerce storefront. An LLM sells to
autonomous AI buyer agents, a deterministic policy kernel holds veto power over
everything the LLM proposes, and every money action is hash-chained into a public
tamper-evident ledger. Payments run through Razorpay in test mode only.

Two documents describe the design: `plan.md` is the §-numbered architecture spec, and
`BAZAAR_BUILD_PHASES.md` is the phase plan. **Never write § numbers or phase numbers into
source comments** — code comments explain what the code does, not where it came from in
the plan.

## Commands

```bash
pip install -r requirements.txt
cp .env.example .env               # then fill in; .env is gitignored, never commit it
python scripts/init_db.py          # idempotent, and deliberately cannot drop anything

python -m uvicorn api.app:app --reload
```

Tests — the default suite is hermetic: no network, no credentials, no real Razorpay call.

```bash
python -m pytest                                   # full default suite
python -m pytest tests/test_bounds.py              # one file
python -m pytest tests/test_checkout.py::test_name # one test
python -m pytest -k "shadow"                       # by keyword

# bounds.py and gates.py are held at 100% line coverage — check before touching either
python -m pytest --cov=kernel.bounds --cov=kernel.gates --cov-report=term-missing

# opt in to tests that hit real Razorpay test mode (needs a filled-in .env)
python -m pytest --live-api
```

Tests marked `live_api` are deselected unless `--live-api` is passed (`pytest.ini`,
`tests/conftest.py::pytest_collection_modifyitems`). That is what keeps the default run
offline and safe. `pytest.ini` also turns `store`/`kernel`/`api` DeprecationWarnings into
errors — a warning from those packages fails the suite by design.

Demos and manual paths:

```bash
python scripts/razorpay_smoke.py [--amount N] [--order order_X] [--no-browser]
python scripts/checkout_live.py --live [--scenario tier0] [--settle ORD-xxxx] [--no-browser]
python -m scripts.seed_offer [--only tier2] [--list] [--json]
python scripts/tamper_demo.py [--in-place] [--target-seq N]
```

There is **no linter or formatter configured** — no ruff/black/flake8 config and no lint
dependency. Don't invent a lint command; match the surrounding style instead.

## Architecture

The spine is one direction only:

```
vyapaari/  (LLM proposes)  →  kernel/  (decides, can veto)  →  store/  (records, append-only)
                                  ↑
                          api/, mandate/  (surfaces and auth)
```

**`vyapaari/` proposes; it never decides.** The pipeline is
`envelope` → `prompt` → `gemini` → `schema` → `proposer`. It may pick SKUs, quantities,
and discounts; every one of those is re-checked by the kernel afterwards. `proposer.py`
falls back to a deterministic proposal (`SOURCE_FALLBACK`) after `MAX_ATTEMPTS = 2`, so a
model outage degrades rather than fails.

**The import boundary is a hard invariant, not a convention.** `vyapaari/` may not reach
`kernel.payments` by *any* import path, may hold no payment credential, and may not write
to the DB. `tests/test_import_boundary.py` enforces this with an AST walk plus BFS
reachability over the first-party import graph, and also checks that credential env-var
names never appear in `vyapaari/` source. If a change there fails that test, the change is
wrong — not the test.

**The nine bounds are the veto surface.** Limits are frozen named constants in
`settings.py`; `kernel/bounds.py` reads them as independent pure functions. `BOUND_IDS`
maps bound number → the identifier written to the ledger, and those identifiers name the
*rule*, never the private column the rule reads (bound 3 is `price_floor`, not
`floor_price_inr`) because they travel into public responses. A rejection with no bound id
is a bug. Bound 6 is the asymmetric one: tripping it **gates** (routes to a human) rather
than rejects.

Do not pre-filter a proposal before it reaches the bounds. The kernel is only demonstrably
in control if the bad proposal actually arrives and is actually refused; filtering upstream
leaves the veto untested.

**Three gate tiers** (`kernel/gates.py`): Tier 0 auto, Tier 1 signed mandate, Tier 2 human
approval. Highest matching trigger wins. Tier 0 is a narrow allowlist, not a fallback —
anything that does not qualify starts at Tier 1. `gates.py` only *assigns* a tier;
`kernel/checkout.py` is what makes it binding.

**`kernel/checkout.py` is a fixed 11-step money path.** Two orderings are non-negotiable:
the idempotency claim is step 1, and the ledger intent entry (step 5) is written *before*
the Razorpay call (step 6). The amount is read from the stored offer row by `option_id` —
there is no request field anywhere for a caller to state a price. Reservations
(`stock_hold_ids`, `budget_reserved_inr`) are written onto the order row so they outlive
the request and can be swept later.

**`POLICY_MODE` lives inside the kernel** (`kernel/mode.py`), not at the edge, so it cannot
be bypassed by calling a different endpoint. Read it with `current_mode()`; never copy the
constant. In `shadow` the kernel computes the identical verdict, skips the external calls,
and ledgers `would_have_charged` — and the receipt it issues is a real signed receipt
tagged `policy_mode: shadow`. Default is `shadow`: a misconfigured deploy moves no money.
`tests/conftest.py::forbidden_razorpay` raises on *any* payment call, which is how shadow
mode is proven rather than asserted.

**One canonical encoder feeds two hashes.** `store/canonical.py` (sorted keys,
`separators=(",",":")`, `ensure_ascii=True`, rejects `Decimal`/`set`/`NaN` rather than
guessing) is used by both the ledger chain hash and the receipt HMAC. Changing it changes
both, and invalidates every existing hash.

**The ledger is append-only.** `entry_hash = SHA256(prev_hash + canonical_json(core))`
covering the whole entry, genesis `prev_hash = "0"*64`. SQL `BEFORE UPDATE`/`BEFORE DELETE`
triggers `RAISE(ABORT)`. Corrections are new compensating entries; nothing is ever edited.
Mandate replay protection is a `UNIQUE` partial index on the ledger itself
(`idx_ledger_mandate_nonce`), so the audit trail *is* the nonce store. This is honestly
framed as tamper-**evidence**, not tamper-proofing — keep that framing in any docs or UI
copy you write.

**Public/private catalog split.** `products` vs `product_private`.
`store.catalog.to_public()` is the only DB→HTTP path for product data, and it is a
whitelist: it constructs a fresh dict from 7 declared public fields rather than deleting
private ones. That direction is the point — a blacklist leaks any new private column the
day it is added. `tests/test_no_private_leak.py` guards this.

**`api/mcp.py` is a wrapper and must stay one.** All four tools (`search_products`,
`get_offer`, `buy`, `check_order`) call the same handler functions and the same Pydantic
models as the HTTP endpoints, so validation, rate limiting, ledger writes, and the veto are
shared code. A tool here cannot be more permissive than the endpoint beside it because
there is no separate path for it to be permissive on. Errors are **raised** as `ToolError`,
never returned as content — a refusal returned as ordinary content reads to a calling agent
like a successful purchase. Tool docstrings are read by a model choosing a tool; write
maintainer notes in comments instead.

`build_server()` is called per app and held on `app.state`, not as a module singleton,
because a session manager may only be started once and a test suite runs two apps in one
process.

### Conventions that bite

- **Whole rupees everywhere.** Paise exist only inside `kernel/payments.py`
  (`_to_paise` / `_to_rupees`). Nothing else in the codebase should know about them.
- **`extra="forbid"` on every request model.** An agent that sends `amount_inr` gets a 422
  naming the unknown field, rather than having it silently dropped — a dropped price field
  is indistinguishable to the sender from an accepted one.
- **Two checks that agree are intentional.** The idempotency key is required by the schema
  *and* re-checked inside the kernel as bound 9. A validation layer can be bypassed by a
  new caller; a bound cannot. Don't "de-duplicate" these.
- **Outbound prose is controlled.** `kernel/reasons.py` holds `FORBIDDEN_PHRASES` and
  `VALUE_SCANNED_FIELDS`; `settings.assert_no_secrets_in_prompt()` is the inbound
  counterpart and must run before any LLM dispatch.
- **The ledger records shapes, not words.** Free text from a caller is stored as
  `need_sha256` / `need_chars`. `/audit` is public.
- **Latency hints are never tighter than real budgets.** `LATENCY_HINTS_MS` is published in
  the discovery manifest so agents budget their own timeouts instead of retrying; a retry
  storm is a double-charge problem. Do not tune them down.
- Ids come from `store/ids.py` using `secrets`, not `random`. Timestamps come from
  `store/timestamps.py` in a lexically sortable format.
- `store/db.py` uses `synchronous=FULL` — money data, do not relax it. `migrate()` is
  additive-only.
- `scripts/_env.py` reads `.env` off disk and is script-only; app code takes secrets from
  the environment. `scripts/_console.py::use_utf8_stdout()` exists because the Windows
  console is cp1252 — use it in any new script that prints non-ASCII.

### Security invariants

- Secrets come from the environment only — never a repo file, never hardcoded, never
  interpolated into a prompt. `settings.Secret` masks on `str()`/`repr()`; reading needs an
  explicit `.reveal()`.
- `kernel/payments.py` is the only module permitted to hold payment credentials.
- Use only `rzp_test_*` Razorpay keys. Never live keys in this project.

## Repo state

- **Git root is `C:\Users\KIIT` (the home directory), not this folder**, on branch
  `add-palindrome_number-cpp`. A bare `git add .` here would try to stage the entire user
  profile. `BAZAAR_BUILD_PHASES.md` defers all git setup to the user — do not run
  `git init` or restructure the repo unless asked.
- Phases 1–3 are complete and the full hermetic suite is green (282 passed, 38 live_api
  skipped). `harness/grahak.py` (buyer agent) exists.
- Phase 4 does not exist yet: no oversell saga, no `POST /demo/force_oversell`, no
  `OVERSOLD_MERCHANT_FAULT` structured failure response, no dashboard, no A/B session
  harness, no deploy. `store.catalog.set_offerable()` is the self-heal *hook* the saga
  will use; the saga itself is unwritten.
- Routes currently served: `/.well-known/agent-commerce.json`, `/audit/verify`,
  `/audit/{ref}`, `/agent/v1/{catalog,offer,checkout}`, `/agent/v1/order/{id}[/settle]`,
  `/merchant/v1/approvals[...]`, `/webhooks/razorpay`, `/health`, and `/mcp`.
