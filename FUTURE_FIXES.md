# FUTURE_FIXES

Audit of PRAMAN's core logic against `PRAMAN_2.0_Architecture.md`.
Scope: `kernel/`, `policy/`, `store/`, `vyapaari/`, `api/`, `mandate/`, `settings.py`.
Tests were deliberately **not** audited — this is about whether the brain is correct,
not whether it is covered.

Section references (§) point at `PRAMAN_2.0_Architecture.md`.

---

## The one-paragraph verdict

The zero-trust spine is real and provable: Vyapaari cannot reach payments, the ten
bounds genuinely run, the money path is ordered correctly, the ledger chains, and the
oversell saga is wired into the live checkout. That is the hard half and it is done.

The **economic control plane is not connected**. The Merchant Economic Constitution is
never written, so the policy hierarchy always falls back to global defaults. The
merchant-facing policy editor writes to a table nothing reads. The optimizer computes
the wrong objective function and, verifiably, prefers the *cheapest* valid deal — the
opposite of what §8 specifies. The negotiation engine, the TDR, the core-safety
invariants and the state machine are all written, tested, and reachable from nothing.

Net: **PRAMAN currently enforces `settings.py`, not the merchant's constitution.**

---

## Severity summary

| # | Finding | Where | Sev |
|---|---|---|---|
| F1 | MEC is never written; hierarchy always falls back to defaults | `policy/resolver.py:92` | **P0** |
| F2 | `store_id="default"` hardcoded in the offer path | `kernel/offer.py:843` | **P0** |
| F3 | Two policy systems; merchant edits the one nothing reads | `api/policy.py` | **P0** |
| F4 | Two different price floors, both called "20% margin" | `kernel/bounds.py:266` | **P0** |
| F5 | Optimizer maximizes the wrong thing; picks cheapest valid deal | `policy/optimizer.py:150` | **P0** |
| F6 | Weights contradict spec; validator exists but is never called | `settings.py:247`, `policy/mec.py:48` | **P0** |
| F7 | LLM told `settings` caps, not effective policy | `vyapaari/prompt.py:117` | **P0** |
| F8 | Negotiation engine unreachable — no route, no MCP tool | `policy/negotiation.py` | **P0** |
| F9 | No TDR is ever written | `kernel/checkout.py` | **P0** |
| F10 | No effective-policy snapshot persisted on offer/order | `store/db.py:92` | **P0** |
| F11 | `core_safety.py` and `states.py` enforce nothing | `policy/` | **P0** |
| F12 | Missing cost defaults to 0 → perfect margin score | `kernel/offer.py:186` | P1 |
| F13 | Misconfigured margin disables the floor (fails open) | `policy/pre_filter.py` | P1 |
| F14 | Unknown SKU silently excluded from cost sum | `policy/optimizer.py:49` | P1 |
| F15 | All product context is mock; inventory term is inert | `kernel/offer.py:182` | P1 |
| F16 | AOV score is relative to the candidate batch | `policy/optimizer.py:72` | P1 |
| F17 | Approval thresholds have two independent sources | `kernel/gates.py:54` | P1 |
| F18 | Policy API swallows DB errors, serves defaults as real | `api/policy.py:38` | P1 |
| F19 | Policy API bypasses tenancy; cross-store write | `api/policy.py:91` | P1 |
| F20 | Bare `except:` in optimizer fails to worst value | `policy/optimizer.py:67` | P1 |
| F21 | Only `ranked[0]` used; rest of ranking discarded | `kernel/offer.py:884` | P2 |
| F22 | Candidate id round-trips through `int(str(idx))` | `kernel/offer.py:885` | P2 |
| F23 | `_compute_feasible_range` contains `min(x, x)` | `policy/negotiation.py:73` | P2 |
| F24 | Three names for variable cost across adjacent modules | several | P2 |
| F25 | `pre_filter` docstring contradicts check order | `policy/pre_filter.py:47` | P2 |
| F26 | `missing` stock holds never compensated | `kernel/checkout.py:1079` | P2 |
| F27 | Ten bounds, documented as nine | `settings.py:150` | P2 |
| F28 | Tie-break by candidate id decides near-ties | `policy/optimizer.py:174` | P2 |
| F29 | Optimizer magic numbers not in settings | `policy/optimizer.py` | P2 |
| F30 | No Intent Gateway component | — | P2 |

---

# A. Wiring — components built but connected to nothing

## F1 — The MEC is never written, so the hierarchy never runs  **P0**

`save_mec_version()` (`store/mec_store.py:19`) is called from **tests only**. In any real
deploy `mec_versions` is empty, so `resolve_effective_policy()` takes its
`if not chain:` branch at `policy/resolver.py:92` and returns
`settings.DEFAULT_MEC_HARD_CONSTRAINTS` every time.

Consequence: §5 (store → category → SKU → campaign), §6 (merchant configuration),
§12 (policy snapshot) and §32 differentiator #1 are all inert. The resolver's merge
logic is written and correct; it simply never receives input.

**Fix:** on merchant signup, seed a store-scope MEC v1 from
`settings.DEFAULT_MEC_*`. Then make `PUT /merchant/v1/policy` mint MEC v(n+1) rather
than write a separate table (see F3). Add a startup assertion that a store with no MEC
is a hard error rather than a silent fallback.

## F2 — `store_id="default"` is hardcoded in the offer path  **P0**

```python
# kernel/offer.py:843
effective_policy = resolve_effective_policy(
    store_id="default",          # ← ignores tenancy.current_store()
    category=category,
    sku=base_sku,
    conn=conn
)
```

`store/tenancy.py` exists precisely to answer this, resolves `X-Store-Id` into a
contextvar, and its own docstring states: *"any route that reads or writes per-store
data must resolve explicitly."* This is that route, and it does not.

Consequence: in a multi-store deployment every store is priced by store `default`'s
constitution. Combined with F1 this is currently masked (everyone gets global
defaults) — which means fixing F1 alone would silently introduce cross-tenant policy
bleed. **Fix F2 before F1.**

**Fix:** `store_id=tenancy.current_store()`. Add an isolation test that store B's
offer cannot be priced by store A's MEC.

## F3 — Two policy systems; the merchant edits the one nothing reads  **P0**

| | reads | writes |
|---|---|---|
| `mec_versions` (the real one) | `policy/resolver.py` | nothing |
| `merchant_policy` (the API one) | `api/policy.py` GET only | `api/policy.py` PUT |

`api/policy.py` stores four flat keys (`item_discount_cap`, `cart_discount_cap`,
`daily_budget`, `approval_limit`). No kernel module, no bound, no gate and no resolver
reads that table.

Consequence: a merchant changing their discount cap in the dashboard gets HTTP 200, a
ledger entry saying `policy.updated`, and **zero change in behaviour**. The audit trail
records a policy change that did not happen — worse than no feature.

**Fix:** delete `merchant_policy`. Make `PUT /merchant/v1/policy` construct an `MEC`
and call `save_mec_version()`, returning the new version number. Keep the 4-field shape
as the API surface if you like — just land it in the MEC.

## F8 — The Negotiation Engine is unreachable  **P0**

`policy/negotiation.py` is 238 lines implementing merchant floor, feasible range,
counter-offer generation and `NO_FEASIBLE_DEAL`. Nothing outside `tests/` imports it.

- No `POST /agent/v1/negotiate` route (`api/agent.py` has catalog/offer/checkout only)
- No `negotiate` MCP tool (`api/mcp.py` exposes exactly `search_products`, `get_offer`,
  `buy`, `check_order`)

Consequence: §4, §24 and differentiator #2 cannot occur. §25 explicitly asks for
`negotiate()` and `authorize_purchase()` in the agent-facing toolset. A buyer agent
today can accept or walk away — it cannot counter, which is the single most demo-able
behaviour in the whole document.

**Fix:** add `POST /agent/v1/negotiate` calling `evaluate_buyer_proposal()`, then wrap
it as an MCP tool in `api/mcp.py` — same handler, same Pydantic model, per the wrapper
invariant. Counter-offers must be re-checked by the bounds before being returned.

## F9 — No TDR is ever written  **P0**

The schema exists (`store/db.py:306`, `transaction_decision_records`). The dataclass
exists (`policy/tdr.py`). The store exists (`store/tdr_store.py`). `kernel/checkout.py`
contains **zero** references to `tdr`.

Consequence: §7, §29, Invariant 7 ("every completed transaction must produce a
reconstructable decision record") and Invariant 8 ("every payment execution must
reference exactly one immutable approved TDR") are unmet. Invariant 8 is currently not
merely unenforced but *structurally impossible* — there is no TDR to reference.

**Fix:** write the TDR at checkout step 5, before the Razorpay call, in the same
transaction as the intent ledger entry. Store `tdr_id` on the order row and carry it
into the receipt.

## F10 — No effective-policy snapshot is persisted  **P0**

Neither `offers` (`store/db.py:92`) nor `orders` (`:107`) carries `policy_id`,
`policy_version`, `policy_hash`, or even `store_id`. `policy/snapshot.py` computes a
policy hash which is then discarded.

Consequence: §12's guarantee — *"an order authorized at 10:10 AM still references MEC
v17"* — cannot be reconstructed. And §11's "Final Policy Kernel" cannot verify the deal
against *the policy that authorized it*, because that policy was never recorded.

**Fix:** add `store_id`, `policy_id`, `policy_version`, `policy_hash` to `offers`; copy
them onto `orders` at checkout. `store/db.py::migrate()` is additive-only, which this
respects.

## F11 — `core_safety.py` and `states.py` enforce nothing  **P0**

Both are imported only by their own test files.

- `policy/core_safety.py` — the eight invariants of §13. Documented, not enforced. An
  invariant that no code path calls is a comment.
- `policy/states.py` — the state machine of §15. Meanwhile `orders` has its own
  independent `CHECK (state IN (...))` constraint whose transition set does not
  correspond to `states.py`. **Two state machines, neither aware of the other.**

**Fix:** call `core_safety.assert_*` at checkout steps 3, 5 and 6 — cheap, and it turns
§13 into a runtime guarantee. Either make `orders.transition()` delegate to
`states.py`, or delete `states.py`; do not keep both.

## F30 — No Intent Gateway  **P2**

§1's component does not exist. `intent_id` appears only in `policy/tdr.py` (itself
unwired). The buyer's authority reference and the normalized intent record have nowhere
to live, so the TDR's `intent_id` field can only ever be synthesised.

**Fix:** low priority relative to the above, but the TDR needs *something* stable to
reference. Minimum viable: persist the normalized offer request under an `INT-` id and
reference that.

---

# B. Economics — the parts that are wired but wrong

## F4 — Two different price floors, both claiming "20% minimum margin"  **P0**

| Site | Formula | Floor at cost ₹800 | Implied contribution margin |
|---|---|---|---|
| `policy/pre_filter.py` | `cost / (1 − m/100)` | **₹1,000** | 20.0% ✅ matches §7 |
| `policy/negotiation.py:56` | `cost / (1 − m/100)` | **₹1,000** | 20.0% ✅ |
| `kernel/bounds.py:266` | `ceil(cost × 1.20)` | **₹960** | **16.67%** ❌ |

`bounds.py` implements a *markup*, not a *margin*. §7 is explicit:
`Price_min = 800 / (1 − 0.20) = ₹1,000`.

Two of three sites agree with the spec; the outlier is the one that is **binding at
checkout**, because `evaluate_item()` is what the final kernel runs. Today the
pre-filter's stricter floor masks this in the offer path — but the two disagree, and the
laxer one is the one that guards the money.

Also note the tests hold `bounds.py` at 100% line coverage, so the wrong formula is
fully covered. Coverage is not correctness.

**Fix:** make `floor_price_inr()` take `min_margin_pct` from the effective policy and
use `ceil(cost / (1 − m/100))`. Delete `FLOOR_PRICE_COST_MULTIPLIER`. Update the
docstring's ₹3,299 → ₹3,959 worked example, which is derived from the wrong formula.

## F5 — The optimizer maximizes the wrong objective and prefers the cheapest deal  **P0**

This is the most consequential economics bug.

§8 and §18 specify **expected contribution**: `P(accept) × (price − cost)`, which is
concave in price and has an interior maximum. That is the entire reason the doc's
worked example selects ₹3,100 over both ₹3,000 and ₹3,200.

`policy/optimizer.py:150` instead computes a weighted **sum of four 0–1 ratios**:

```
Score(d) = w_m·M + w_c·C + w_a·A + w_i·I
```

where `M` is a margin *ratio*, not a contribution *amount*. A sum of ratios has no
interior maximum. Worked through with the doc's own numbers (3 units, cost ₹800 each,
buyer budget ₹3,200) and the shipped weights of 0.25 each:

| Price | M | C | A | I | Score |
|---|---|---|---|---|---|
| ₹3,000 | 0.200 | 0.651 | 0.938 | 0.225 | **0.5034** ← picked |
| ₹3,100 | 0.226 | 0.578 | 0.969 | 0.225 | 0.4993 |
| ₹3,200 | 0.250 | 0.500 | 1.000 | 0.225 | 0.4938 |

It picks **₹3,000**. The doc says ₹3,100.

And with the doc's intended 0.4 / 0.3 / 0.2 / 0.1 weights it is still monotonically
decreasing (0.4853 / 0.4799 / 0.4725) — so this is not a weight-tuning problem. The
conversion sigmoid at `k=10` declines faster than margin and AOV rise, so **the score is
monotonically decreasing in price for any weight vector.** The optimizer is a
lowest-valid-price picker. §8's "It does not simply minimize the price" is exactly
inverted.

**Fix — multiply instead of adding.** Score expected contribution directly:

```python
contribution_inr = revenue - total_cost          # rupees, not a ratio
expected_contribution = conversion_score * contribution_inr
```

Checked against the same numbers:

| Price | C | Contribution | Expected |
|---|---|---|---|
| ₹3,000 | 0.651 | ₹600 | ₹390.6 |
| ₹3,100 | 0.578 | ₹700 | **₹404.3** ← picked |
| ₹3,200 | 0.500 | ₹800 | ₹400.0 |

That reproduces §8 and §18 exactly. Keep AOV and inventory as multiplicative or
additive *tilts* on top of expected contribution, weighted by the merchant's objectives
— but the base quantity being maximized must be money, not a ratio.

## F6 — Objective weights contradict the spec, and the validator is never called  **P0**

```python
# settings.py:247
DEFAULT_MEC_OBJECTIVES = {
    "margin_weight": "0.25", "conversion_weight": "0.25",
    "aov_weight": "0.25", "inventory_velocity_weight": "0.25",
}
```

§4 and §5 both specify the store default as margin 40 / conversion 30 / AOV 20 /
inventory 10. Shipped as flat 0.25.

`policy/mec.py:48` **does** contain a correct `validate_objectives()` that enforces
sum == 1.0 — and it is called from nowhere outside `tests/`. So this is the same shape as
F11: the guard is written, tested, and never invoked. The fix is one call site, not new
logic.

Two gaps remain even once it is called. It does not check non-negativity, so
`(2.0, −1.0, 0, 0)` sums to 1.0 and passes — a negative conversion weight would make the
optimizer actively prefer deals the buyer will reject. And with all-zero weights (which
do *not* sum to 1, so calling the validator closes this) every score would be 0 and
ranking would fall entirely to the `candidate_id` tie-break (F28).

**Fix:** set the documented 0.40/0.30/0.20/0.10. Call `validate_objectives()` in the MEC
constructor and in `resolve_effective_policy()` after the merge — the merge is where a
category or SKU override can produce a vector that no single scope violated. Add a
non-negativity check.

## F7 — The proposer is told `settings` caps, not the effective policy  **P0**

`vyapaari/prompt.py:117` builds the model's stated limits from
`settings.MAX_CART_DISCOUNT_PCT`. The effective policy is resolved *after* the proposer
runs (`kernel/offer.py:842` comes after `proposer.propose()` at `:821`).

Consequence: a merchant who tightens their per-SKU cap from 12% to 5% gets this
sequence — the LLM is still told 15%/12%, proposes deals at up to 12%, `pre_filter`
rejects every one, and the buyer receives **HTTP 403 `CODE_POLICY_REFUSED` "All
candidates violated merchant policies."** Tightening policy does not produce tighter
offers; it produces total offer failure.

This also violates the spirit of §11: the pre-filter is meant to catch the occasional
impossible candidate, not to reject 100% of them because the proposer was briefed from
the wrong rulebook.

**Fix:** resolve the effective policy *before* calling the proposer, and pass its limits
into `envelope`/`prompt`. The kernel still re-checks everything afterwards — that
invariant is untouched. This is telling the LLM the truth, not trusting it.

## F15 — All product context is mock data; the inventory term is inert  **P1**

```python
# kernel/offer.py:182-186
inventory_age_days=30,              # default mock since DB doesn't have it
demand_velocity=Decimal("1.0"),     # default mock
conversion_rate_pct=Decimal("5.0"), # default mock
return_rate_pct=Decimal("1.0"),     # default mock
```

Every SKU gets identical context. Therefore in `_inventory_score`:
`age_factor = 30/120 = 0.25`, `demand_factor = 1 − 1/10 = 0.9`, so
`I(d) = 0.225` for **every candidate, always**. A constant term cannot change a ranking:
`w_i · I(d)` is dead weight.

Consequence: §9's "Old Headphones, 90 days in inventory, priority = inventory
movement", §20 and §21 are all inert. The clearance-vs-flagship distinction that
motivates the whole per-product economics story does not happen.

Separately, `conversion_rate_pct` and `return_rate_pct` are carried in `ProductContext`
and **never read by the optimizer at all** — `_conversion_score` uses only budget
proximity, ignoring the trailing conversion rate sitting right there in the dataclass.

**Fix:** add `inventory_age_days` and `demand_velocity` to `product_private` (or derive
velocity from order history), and populate from the DB. Then either use
`conversion_rate_pct` in `_conversion_score` as a prior, or drop the field.

## F16 — AOV score is relative to the candidate batch  **P1**

`_aov_score` normalizes against `max_total` **of the current candidate set**
(`policy/optimizer.py:135`). The same deal therefore scores differently depending on
which other candidates the LLM happened to propose that request, and adding one
expensive decoy candidate lowers every other candidate's AOV score and can flip the
winner.

Merchant economics must be a function of the deal and the policy — not of its
competitors in one LLM sample. This also makes the optimizer's output non-reproducible
across identical requests whenever the proposer varies.

**Fix:** normalize against something stable — the buyer's budget, or a configured AOV
target from the MEC.

## F17 — Approval thresholds have two independent sources  **P1**

`kernel/gates.py:54` sets `TIER_AUTO_MAX_TOTAL_INR = MANDATE_REQUIRED_ABOVE_INR` from
`settings`. The MEC separately carries
`approval_thresholds.auto_max_inr` / `mandate_max_inr` (`settings.py:241`).

§6 Step 5 has the merchant configure exactly these three bands. Configuring them changes
nothing, because `assign_tier()` never sees the effective policy.

The two agree **today** only by construction: `settings.py:242` seeds
`"auto_max_inr": MANDATE_REQUIRED_ABOVE_INR` from the same constant `gates.py` reads.
So there is no visible disagreement until a merchant overrides the MEC — at which point
the gate keeps using the settings value and the merchant's configured threshold is
silently ignored. The same seeding pattern appears at `settings.py:235` for
`max_cart_discount_pct`, which is why F7 is also currently invisible.

Note `gates.py`'s own docstring makes the right argument for a single source — *"Passing
it in is what makes the amount rule and the bound agree instead of being two thresholds
that can drift apart"* — and then reads a second source anyway.

**Fix:** thread `effective_policy.hard_constraints.approval_thresholds` into
`assign_tier()`. Same reasoning as F4.

## F21 — Only the top-ranked candidate survives  **P2**

```python
# kernel/offer.py:884
best_candidate_id = ranked[0].candidate.candidate_id
best_candidate = outcome.candidates[int(best_candidate_id)]
```

The full ranking is ledgered (good, auditable) and then discarded. All buyer-visible
options are derived by `assemble()` from that single proposal.

§3 and §20 both produce "Ranked Valid Offers" plural, and §8's table is a comparison
*across* price points. Presenting options from one proposal only is a narrower product
than specified.

**Fix:** carry the top-N ranked deals into `assemble()` so the options the buyer sees
span distinct candidates, and the offer response can honestly say why one was
recommended over the others.

## F23 — `_compute_feasible_range` is a no-op with a bug in it  **P2**

```python
# policy/negotiation.py:72-73
def _compute_feasible_range(floor: int, ceiling: int) -> tuple[int, int]:
    return (floor, min(ceiling, ceiling))       # min(x, x) == x
```

Never clamps ceiling against floor, so when the buyer's ceiling is below the merchant
floor it returns an inverted range `(3000, 2700)` instead of signalling no overlap —
which is §7's central case.

**Fix:** return `None` (or an explicit `NoOverlap`) when `ceiling < floor`, and have
`evaluate_buyer_proposal` branch on that rather than on an inverted tuple.

---

# C. Fail-open paths

Every one of these fails in the direction of *permitting* a deal. §16 is explicit that
policy problems must fail closed.

## F12 — Missing cost defaults to zero, producing a perfect margin score  **P1**

```python
# kernel/offer.py:186
variable_cost_inr=private.get("cost_inr", 0)
```

Cost 0 → `_margin_score` = `(revenue − 0) / revenue` = **1.0**. A SKU whose cost column
is missing or NULL scores as maximally profitable and wins the ranking outright.

**Fix:** raise. A product without a cost cannot be priced against a margin floor, and
guessing zero is the worst available guess.

## F13 — A misconfigured margin disables the floor entirely  **P1**

```python
# policy/pre_filter.py
margin_factor = Decimal(1) - (min_margin / Decimal(100))
...
if margin_factor <= Decimal(0):
    floor_price = Decimal(0)        # ← no floor at all
```

`min_margin_pct >= 100` removes the price floor rather than refusing to operate.
`policy/negotiation.py:56` raises `ValueError` for the identical condition. Two modules,
same guard, opposite behaviour — and the comment in each cites the other as precedent.

**Fix:** raise in both. Validate `0 <= min_margin_pct < 100` at MEC construction so this
is unreachable at evaluation time.

## F14 — Unknown SKUs are silently excluded from the cost sum  **P1**

```python
# policy/optimizer.py:46-50
total_cost = sum(
    item.quantity * product_context[item.sku].variable_cost_inr
    for item in candidate.items
    if item.sku in product_context          # ← silently drops the line's cost
)
```

An item absent from `product_context` contributes its **revenue** to the numerator but
**zero cost**, inflating the margin score. `pre_filter` does guarantee SKU existence
upstream, but the optimizer does not know that and is a public function.

**Fix:** `raise KeyError` on a missing SKU. If pre-filter's guarantee holds, the raise is
unreachable; if it ever stops holding, you want the crash rather than the inflated
score.

## F18 — Policy API swallows DB errors and serves hardcoded defaults as real  **P1**

`api/policy.py` has `except Exception: pass` at lines 38, 59 and 110. In `_get_policy`,
any DB failure falls through to `return dict(DEFAULT_POLICY)` — the merchant sees
`12 / 15 / 10000 / 6000` and cannot distinguish it from their actual saved
configuration.

`DEFAULT_POLICY` also hardcodes four values that duplicate `settings` constants, under a
module docstring that reads *"editable 3 fields, wired to DB, no hardcoding."* Three
claims, three inaccuracies: there are four fields, the DB write is best-effort, and the
values are hardcoded.

**Fix:** let DB errors 500. Read defaults from `settings`, never inline. Fix the
docstring.

## F19 — Policy API bypasses tenancy and permits cross-store writes  **P1**

```python
# api/policy.py:91, :103
sid = (store_id or "default").strip() or "default"
```

No `tenancy.resolve()`, no validation against `configured_stores()`. Any string in
`X-Store-Id` becomes a row key. `_require_merchant()` authenticates a merchant but does
not bind that merchant to a store, so an authenticated merchant can PUT policy under any
other store's id.

Currently low-impact only because nothing reads the table (F3) — but if F3 is fixed by
pointing this endpoint at the MEC, **this becomes a cross-tenant policy write against
live pricing.** Fix F19 in the same change as F3.

**Fix:** `sid = tenancy.resolve(store_id)`, and check the authenticated merchant owns
that store before writing.

## F20 — Bare `except:` in the optimizer, failing to the worst value  **P1**

```python
# policy/optimizer.py:63-70
try:
    k = Decimal(10)
    exp_val = Decimal(math.exp(float(-k * x)))
    score = Decimal(1) / (Decimal(1) + exp_val)
except:                        # line 67 — catches KeyboardInterrupt, SystemExit, everything
    score = Decimal(0)         # silently the *minimum* conversion score
```

Two problems. The bare `except` catches non-`Exception` `BaseException`s including
Ctrl-C. And the fallback of 0 silently assigns the worst possible acceptance
probability, which distorts the ranking rather than failing.

Also `math.exp(float(...))` drops to float inside a module that otherwise uses `Decimal`
throughout for money-adjacent arithmetic. `OverflowError` here is a real possibility for
large `|x|`, which is presumably why the handler exists.

**Fix:** `except (OverflowError, InvalidOperation)`, and clamp `x` to a sane range
(±40) before exponentiating so the handler becomes unreachable. Note `k = 10` is the
constant responsible for F5 — see F29.

## F26 — `missing` stock holds are recorded but never compensated  **P2**

`kernel/checkout.py:1079` ledgers a `stock.commit_anomaly` for `recovered`, `oversold`
and `missing`. But only `oversold` triggers the saga (`:1192`).

`recovered` is genuinely benign — a lapsed hold whose stock still decremented.
`missing` is not: a hold that cannot be found means the reservation is unaccounted for,
and the order still proceeds to CONFIRMED on a captured payment that may be
unfulfillable. No refund, no self-heal, just a ledger note.

**Fix:** decide explicitly. Either `missing` implies possible non-fulfilment and should
compensate, or it is provably benign and the comment should say why. Right now it is
neither.

---

# D. Hygiene and drift

## F22 — Candidate id round-trips through a list index  **P2**

`_to_candidate_deal(cand, by_sku, cand_id=str(idx))` then
`outcome.candidates[int(best_candidate_id)]` (`kernel/offer.py:885`). Any change to the
id scheme silently mis-selects a candidate or raises `ValueError` deep in the offer
path. Carry the object or a real map instead of an index laundered through a string.

## F24 — Three names for variable cost  **P2**

- `cost_inr` — `product_private` column, `pre_filter`, `bounds.evaluate_offer`
- `variable_cost_inr` — `ProductContext`, `negotiation._compute_merchant_floor`
- "variable cost" — the architecture doc

`pre_filter` reads `product_economics[sku]["cost_inr"]` from an untyped `dict[str,
dict[str, Any]]`, so a rename anywhere is a runtime `KeyError` rather than a type error.
Pick one name; give the private-economics dict a dataclass.

## F25 — `pre_filter` docstring contradicts the code  **P2**

The docstring (`policy/pre_filter.py:47`) numbers the checks 1–5 in order; the
implementation runs check 3 (cart discount) first, before check 1 (SKU existence). Since
the first failure short-circuits, the reported `bound_violated` for a
multiply-invalid candidate is not the one the docstring predicts.

## F27 — Ten bounds, documented as nine  **P2**

`settings.BOUND_IDS` has entries 1–10 (bound 10 = `relatedness_required`) and
`kernel/bounds.py` defines ten `check_*` functions. `kernel/mode.py`'s docstring says
*"all nine bounds are evaluated"*, and `CLAUDE.md` says "The nine bounds are the veto
surface." Pick the true number and correct every site — this one is quoted in public
copy.

## F28 — Tie-break by candidate id decides real money  **P2**

```python
# policy/optimizer.py:167
scored_candidates.sort(key=lambda x: (x[0], x[1].candidate_id), reverse=True)
```

Deterministic, but arbitrary — and `reverse=True` makes it *descending* by id. Given the
flat 0.25 weights (F6), score gaps of ~0.002 are normal (see F5's table), so this
tie-break selects the winner more often than it should. Once F5 lands, scores are in
rupees and separate properly; until then this is doing real work.

## F29 — Optimizer magic numbers are inline and merchant-invisible  **P2**

`policy/optimizer.py` hardcodes:

- `Decimal(120)` — inventory age ceiling in days
- `Decimal(10)` — demand velocity divisor
- `k = Decimal(10)` — sigmoid steepness, **the direct cause of F5**
- `Decimal("0.5")` — conversion score when no budget is stated

None are in `settings`, none are merchant-tunable, and `k` silently determines whether
the merchant's margin preference can ever win. Move all four to `settings` (or the MEC),
and document `k` as the price-sensitivity parameter it is.

---

# Suggested order of work

Dependencies matter here — two of these make things worse if done alone.

1. **F2** (`store_id`) — must land *before* F1, or fixing F1 introduces cross-tenant
   policy bleed that F1 currently masks.
2. **F1 + F3 + F19 together** — seed MECs, point the policy API at `save_mec_version()`,
   and fix tenancy on that endpoint in one change. Doing F3 without F19 turns a dead
   table into a live cross-tenant write.
3. **F4** — unify the floor formula on `cost / (1 − m/100)`. Single formula, from the
   effective policy. Expect `bounds.py` coverage work.
4. **F5 + F6 + F29** — rewrite the objective as expected contribution, set the
   documented weights, lift the constants out. This is the change that makes §8 and §18
   demonstrable, and it is verifiable against the doc's own table.
5. **F7** — resolve policy before the proposer runs and brief the model honestly.
   Without this, every policy tightening becomes an outage.
6. **F12 – F14, F18, F20** — close the fail-open paths. Small, independent, each one a
   direction-of-failure fix.
7. **F8** — expose `negotiate` over HTTP and MCP. Mostly already written.
8. **F9 + F10 + F11** — TDR, policy snapshot, and calling `core_safety`. These three
   together are what make Invariants 7 and 8 true rather than aspirational.
9. **F15** — real product context, so the inventory objective stops being a constant.
10. Remaining P2s.

Items 1–5 are what turn "PRAMAN enforces `settings.py`" into "PRAMAN enforces the
merchant's constitution." Everything after that is depth.

---

## Things that are correct and should not be touched

Worth recording, so a later refactor does not undo them:

- **The import boundary holds.** `vyapaari/` reaches nothing in `kernel.payments`, holds
  no credential, writes no DB.
- **The LLM cannot state a price.** `_to_candidate_deal` (`kernel/offer.py:141`)
  recomputes every unit price from `list_price × discount_pct` and re-derives
  `total_inr` from the line items. The model's own arithmetic is never trusted — only
  its `discount_pct` and `qty`, both of which are then bounded. This is the single most
  important thing in the codebase and it is right.
- **`checkout.py` step ordering.** Idempotency claim first; ledger intent (step 5)
  before the Razorpay call (step 6). On a Vercel `maxDuration` kill mid-payment the
  intent is already on the chain and reconcilable.
- **The bounds genuinely all run.** `evaluate_item` (`bounds.py:555`) deliberately
  evaluates every bound even after one fails, so a single audit shows every violation
  rather than revealing them one fix at a time.
- **Mandate replay protection is the ledger itself** — `idx_ledger_mandate_nonce`. The
  audit trail *is* the nonce store, so the two cannot drift.
- **The oversell saga is wired into the real path**, not just the demo:
  `kernel/checkout.py:1192` runs the full compensation before raising, so the refund
  exists before the caller sees the error.
- **`POLICY_MODE` lives in the kernel** and `assert_may_move_money()` sits on the same
  line as the side effect it guards. Default `shadow` means a misconfigured deploy moves
  no money.
- **`to_public()` is a whitelist**, constructing a fresh dict from declared fields
  rather than deleting private ones.
- **The public/ledger vocabulary is disciplined** — bound ids name rules
  (`price_floor`) not columns (`floor_price_inr`), free text is stored as
  `need_sha256`, and the audit claim is "tamper-evident" rather than "immutable". Keep
  that discipline in any new surface.
