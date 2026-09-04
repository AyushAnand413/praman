"""Configuration for the Aether Audio agent-commerce stack.

Two rules this module exists to enforce, both frozen on day 1:

1. Secrets come from the environment only — never from a file in the repo,
   never hardcoded, and never interpolated into an LLM prompt. They are
   wrapped in `Secret` so an accidental log line, f-string, or traceback
   prints a mask instead of the value. Reading the real value requires an
   explicit `.reveal()`.

2. The nine bounds are named constants declared here, once. They are frozen
   at the start of the build: the kernel reads them, and nothing computes or
   mutates them at runtime. Changing a bound is a code change with a diff,
   not a config tweak.

POLICY_MODE governs whether the policy engine executes live actions or runs in
shadow mode, computing every verdict without any external side effect.
"""

from __future__ import annotations

import os
from decimal import Decimal
from enum import Enum
from pathlib import Path

# Local dev: auto-load .env if present so `python -m uvicorn api.app:app` works
# without `python -m dotenv run --`. Production (Vercel/CI) already has env set,
# so this is a no-op there (override=False). Path is explicit so the reloader
# child process finds it regardless of cwd.
try:
    from dotenv import load_dotenv  # type: ignore

    _env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=_env_path, override=False)
    # fallback to default search if not found at repo root
    if not os.environ.get("DATABASE_URL"):
        load_dotenv(override=False)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CATALOG_PATH = Path(os.environ.get("CATALOG_PATH") or BASE_DIR / "catalog.json")

# Postgres-only — local, CI and Vercel all require DATABASE_URL.
# Example: postgresql://user:pass@host:5432/db?sslmode=require
DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ---------------------------------------------------------------------------
# POLICY_MODE
# ---------------------------------------------------------------------------


class PolicyMode(str, Enum):
    """shadow: kernel computes the full verdict and calls nothing.
    live:   kernel computes the full verdict, then executes it.
    """

    SHADOW = "shadow"
    LIVE = "live"


def _resolve_policy_mode() -> PolicyMode:
    raw = (os.environ.get("POLICY_MODE") or PolicyMode.SHADOW.value).strip().lower()
    try:
        return PolicyMode(raw)
    except ValueError as exc:  # fail loud — an ambiguous mode is a money bug
        valid = ", ".join(m.value for m in PolicyMode)
        raise RuntimeError(
            f"POLICY_MODE={raw!r} is not valid. Expected one of: {valid}"
        ) from exc


# Default is shadow: the safe direction to fail. A misconfigured deploy moves
# no money rather than moving money nobody authorised.
POLICY_MODE: PolicyMode = _resolve_policy_mode()


# ---------------------------------------------------------------------------
# The nine bounds. Frozen. Read-only for every caller.
# ---------------------------------------------------------------------------

# 1 — runaway generosity on a single item. Per-SKU caps in product_private may
# be stricter; the kernel takes the minimum of the two, never the maximum.
MAX_DISCOUNT_PCT_PER_SKU = 12

# 2 — discount stacking across a cart.
MAX_CART_DISCOUNT_PCT = 15

# 3 — selling below viable margin. floor = cost_inr x this multiplier, and a
# SKU's explicit floor_price_inr overrides it when it is higher.
FLOOR_PRICE_COST_MULTIPLIER = Decimal("1.20")

# 4 — discounting the store to death overnight. Resets daily.
DAILY_DISCOUNT_BUDGET_INR = 10_000

# 5 — nagging the buyer agent.
MAX_OFFERS_PER_SESSION = 2

# 6 — large autonomous spend. Above this, a human gates the transaction.
MAX_TXN_WITHOUT_HUMAN_INR = 6_000

# 7 — selling something that does not exist. Mandatory, not tunable.
MIN_STOCK_QTY = 1

# 8 — honouring stale prices.
OFFER_TTL_SECONDS = 300

# 9 — double-charging. Mandatory, not tunable.
IDEMPOTENCY_KEY_REQUIRED = True

# 10 — an upsell must be related to the item it accompanies. Relatedness is
# evidence-based: learned pairings from completed orders, seeded companions,
# or a declared attach candidate / tier-up path. A discount can be legal in
# every other respect and still be refused for pairing a laptop with cat food.
RELATEDNESS_MIN_SAMPLES = 5

# How fast learned evidence ages. Counts decay exponentially with this
# half-life when a pairing is touched, so yesterday's habit fades instead of
# haunting the catalog forever.
PAIRING_HALF_LIFE_DAYS = 45

# ---------------------------------------------------------------------------
# The agentic proposer: exploration with tools, under a hard budget
# ---------------------------------------------------------------------------

# Master switch. False reverts Vyapaari to the one-shot proposer exactly as it
# shipped — same prompts, same fallback ladder. A kill-switch on the clever
# path is what makes shipping the clever path survivable.
PROPOSER_TOOLS_ENABLED = (
    os.environ.get("PROPOSER_TOOLS_ENABLED", "true").strip().lower()
    not in ("0", "false", "no")
)

# The agent may call at most this many tools before it must answer. A bounded
# loop is what makes an agent shippable next to a 3-second latency hint:
# without a cap, "let me check one more thing" is unbounded by construction.
AGENT_MAX_TOOL_CALLS = int(os.environ.get("AGENT_MAX_TOOL_CALLS", "4"))

# Wall-clock ceiling on the whole exploration, seconds. Sits inside the offer
# latency budget so that even a maximal exploration still leaves time for the
# kernel evaluation and the response.
AGENT_WALL_CLOCK_SECONDS = float(os.environ.get("AGENT_WALL_CLOCK_SECONDS", "2.5"))

#: Bound number -> the identifier written into the ledger when it fires.
#: A rejection with no bound id is a silent rejection, which is a bug.
#:
#: These identifiers name the *rule*, never the private column the rule reads.
#: They travel into the public ledger and into the buyer's policy receipt, and
#: no private column name is allowed to appear in a response body — so bound 1
#: is not called `max_discount_pct_per_sku` and bound 3 is not called
#: `floor_price_inr`, however naturally those names would read here.
BOUND_IDS: dict[int, str] = {
    1: "discount_cap_per_sku",
    2: "max_cart_discount_pct",
    3: "price_floor",
    4: "daily_discount_budget_inr",
    5: "max_offers_per_session",
    6: "max_txn_without_human_inr",
    7: "stock_qty_positive",
    8: "offer_ttl_seconds",
    9: "idempotency_key_required",
    10: "relatedness_required",
}


# ---------------------------------------------------------------------------
# Other day-1 constants
# ---------------------------------------------------------------------------

# Ledger. Genesis row's prev_hash. Canonical JSON = sorted keys, no whitespace
# variance; the separators live here so the hash function and the verifier can
# never drift apart.
LEDGER_GENESIS_PREV_HASH = "0" * 64
CANONICAL_JSON_SEPARATORS = (",", ":")

# Stock holds.
STOCK_HOLD_TTL_SECONDS = 120

# Most SKUs one catalog query will return. This is a ceiling, not paging: it
# keeps a one-word query from returning the whole shop as though every item
# matched. It lives here rather than beside the endpoint because it has to rise
# as the catalog grows, and a limit you have to edit code to raise is a limit
# that silently truncates results the day a merchant imports a real catalog.
MAX_CATALOG_RESULTS = int(os.environ.get("MAX_CATALOG_RESULTS", "10"))

# How long an unpaid two-step checkout keeps its reserved discount budget before
# the sweep takes it back. Deliberately much longer than the hold TTL above: a
# lapsed hold does NOT mean the buyer left, because filling in a card form takes
# longer than 120 seconds routinely, and sweeping on hold expiry alone would fail
# orders out from under buyers who are still paying. Long enough to outlast any
# gateway checkout session, short enough that a day's budget cannot be held
# hostage by abandoned carts.
CHECKOUT_ABANDONED_AFTER_SECONDS = 1_800

# Internal service-level budgets. Per endpoint, not one global target.
LATENCY_BUDGETS_MS: dict[str, int] = {
    "discovery": 50,
    "catalog": 200,
    "audit": 200,
    "offer": 3_000,
    "checkout": 4_000,
}

# Published in /.well-known/agent-commerce.json so a buyer agent budgets its
# own timeouts instead of timing out and retrying — a retry storm is an
# idempotency problem, i.e. a double-charge problem.
#
# Standing rule: a published hint is NEVER tighter than the real budget. Hence
# checkout is 5000 against a 2-4s budget. Do not "tune" these down.
LATENCY_HINTS_MS: dict[str, int] = {
    "catalog": 200,
    "offer": 3_000,
    "checkout": 5_000,
}

CATALOG_SKU_COUNT = 14

# The discovery contract.
CAPABILITIES = (
    "catalog.query",
    "offer.request",
    "checkout.intent",
    "order.status",
)
MANDATE_REQUIRED_ABOVE_INR = 2_000
MANDATE_AUTH_SCHEME = "ed25519-signed-jwt"
DEFAULT_RETURNS_WINDOW_DAYS = 7

# ---------------------------------------------------------------------------
# Default MEC values — fallback when no merchant-specific MEC is configured.
# These preserve exact backward compatibility with the frozen bounds above.
# ---------------------------------------------------------------------------

DEFAULT_MEC_HARD_CONSTRAINTS = {
    "min_margin_pct": 20,
    "max_discount_pct_per_sku": MAX_DISCOUNT_PCT_PER_SKU,
    "max_cart_discount_pct": MAX_CART_DISCOUNT_PCT,
    "max_txn_without_human_inr": MAX_TXN_WITHOUT_HUMAN_INR,
    "min_stock_qty": MIN_STOCK_QTY,
    "offer_ttl_seconds": OFFER_TTL_SECONDS,
    "daily_discount_budget_inr": DAILY_DISCOUNT_BUDGET_INR,
    "max_offers_per_session": MAX_OFFERS_PER_SESSION,
    "approval_thresholds": {
        "auto_max_inr": MANDATE_REQUIRED_ABOVE_INR,
        "mandate_max_inr": MAX_TXN_WITHOUT_HUMAN_INR,
    },
}

DEFAULT_MEC_OBJECTIVES = {
    "margin_weight": "0.25",
    "conversion_weight": "0.25",
    "aov_weight": "0.25",
    "inventory_velocity_weight": "0.25",
}

DEFAULT_MEC_NEGOTIATION = {
    "price": True,
    "quantity": True,
    "bundles": True,
    "substitutes": True,
    "shipping": False,
    "delivery_date": False,
}

# The closed set of ledger actors. An event from outside this set is a bug,
# not a new actor.
LEDGER_ACTORS = (
    "buyer_agent",
    "vyapaari",
    "policy_kernel",
    "razorpay",
    "merchant",
    "system",
)


# ---------------------------------------------------------------------------
# Secrets: env vars only
# ---------------------------------------------------------------------------


class Secret:
    """A string that refuses to print itself.

    `str()`, `repr()`, and f-strings all yield a mask, so a secret cannot
    reach a log, a traceback, or an LLM prompt by accident. Use `.reveal()` at
    the exact call site that needs the value.
    """

    __slots__ = ("_value", "_name")

    def __init__(self, value: str, name: str) -> None:
        self._value = value
        self._name = name

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return f"<Secret {self._name} len={len(self._value)}>"

    __str__ = __repr__


#: Every env var that holds a credential. Nothing here is ever serialised,
#: logged, or placed in a prompt.
SECRET_ENV_VARS = (
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
    "POLICY_RECEIPT_HMAC_SECRET",
    "MANDATE_SIGNING_SEED",
    "GEMINI_API_KEY",
    "DEMO_KEY",
    "SHOPIFY_ADMIN_ACCESS_TOKEN",
)


class MissingSecret(RuntimeError):
    """Raised when a required credential is absent from the environment."""


def secret(name: str, *, required: bool = True) -> Secret:
    """Read a credential from the environment.

    Lazy on purpose: a caller that needs only the Razorpay keys must not be
    forced to supply GEMINI_API_KEY just to import this module. Each subsystem
    asks for what it needs, when it needs it.
    """
    if name not in SECRET_ENV_VARS:
        raise KeyError(f"{name} is not a declared secret; add it to SECRET_ENV_VARS")
    value = os.environ.get(name, "")
    if not value and required:
        raise MissingSecret(
            f"{name} is not set. Secrets come from the environment only — "
            f"export it or put it in a local .env that git ignores."
        )
    return Secret(value, name)


def assert_no_secrets_in_prompt(text: str) -> None:
    """Guard the vyapaari -> LLM boundary: no secret may ever enter a prompt.

    Called by the prompt builder before dispatch. Compares against live env
    values, so it catches a secret that arrived through any path, not just a
    literal in the source.
    """
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name, "")
        if len(value) >= 8 and value in text:
            raise RuntimeError(
                f"Refusing to send prompt: it contains the value of {name}."
            )


# Non-secret operational config.
DASHBOARD_ORIGIN = os.environ.get("DASHBOARD_ORIGIN", "http://localhost:3000")
MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "Aether Audio")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
RAZORPAY_API_BASE = "https://api.razorpay.com/v1"

# ---------------------------------------------------------------------------
# Shopify connector (integrations/shopify.py)
# ---------------------------------------------------------------------------

# The dev-store domain, e.g. "my-store.myshopify.com". Not a secret; the admin
# token below is.
SHOPIFY_STORE_DOMAIN = os.environ.get("SHOPIFY_STORE_DOMAIN", "")
SHOPIFY_ADMIN_API_BASE_TEMPLATE = "https://{domain}/admin/api/2024-10"

# Shopify's REST product payload does not carry unit cost, but our envelope
# drops any SKU without economics rather than inventing a headroom for it. So
# the importer derives cost from price at this assumed margin and stores the
# derivation in the private attrs, where a merchant can correct it. Stated
# honestly here because it is an assumption, not data.
SHOPIFY_ASSUMED_MARGIN_PCT = int(os.environ.get("SHOPIFY_ASSUMED_MARGIN_PCT", "40"))

# How many products per page during sync.
SHOPIFY_SYNC_PAGE_LIMIT = 100

# ---------------------------------------------------------------------------
# Multi-store tenancy
# ---------------------------------------------------------------------------

# Comma-separated slugs of every store this deployment hosts, e.g.
# "voltmart,gadgethub". Empty means single-store mode and every row lands
# under DEFAULT_STORE_ID. Requests name their store via X-Store-Id; anything
# not in this list resolves to the first entry (or the default) rather than to
# a wildcard.
PRAMAN_STORES = tuple(
    s.strip()
    for s in os.environ.get("PRAMAN_STORES", "").split(",")
    if s.strip()
)

# Which learning cluster each store belongs to, as JSON:
#   PRAMAN_STORE_CLUSTER_MAP='{"voltmart": "electronics", "gadgethub": "electronics"}'
# Stores in one cluster share ANONYMOUS CATEGORY-LEVEL pairing priors ("in
# electronics stores generally, chargers follow phones") — never SKU lists,
# order details, or identities. A store's own observed data always overrides
# its cluster's prior once it has enough samples.
PRAMAN_STORE_CLUSTER_MAP_JSON = os.environ.get("PRAMAN_STORE_CLUSTER_MAP", "{}")

# Cluster priors are suggestions until a store's OWN evidence crosses this
# many baskets per base category.
CLUSTER_PRIOR_MIN_OWN_SAMPLES = 5

# The model that writes the sales pitch. A name, not a credential — it belongs
# here rather than beside GEMINI_API_KEY so it can be logged and put in a ledger
# entry, which is how a later audit tells which model produced an offer.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

# Wall-clock ceiling on one model call. Deliberately below the offer latency
# budget above, because the offer path may make two calls (the original and the
# one retry) and still has the whole kernel evaluation to do afterwards. A model
# that has not answered by then is slower than the deterministic fallback, so
# waiting longer costs the buyer time and buys nothing.
GEMINI_TIMEOUT_SECONDS = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "30"))

# Low but not zero. Some variety in the sales prose is the point of using a model
# at all; the numbers it returns are re-checked by the kernel either way, so
# sampling temperature is a copywriting knob and not a safety one.
GEMINI_TEMPERATURE = float(os.environ.get("GEMINI_TEMPERATURE", "0.4"))

# Reasoning tokens the model may spend before answering. Zero: picking a product
# and two add-ons out of a 14-SKU catalog is a retrieval task, and the seconds
# that extended thinking costs come straight out of the offer budget above. The
# arithmetic that would justify thinking is the kernel's, and the kernel does not
# guess. Set to -1 to omit the field entirely, for a model that rejects it.
GEMINI_THINKING_BUDGET = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))


def validate_startup(*, require_llm: bool = False, require_payments: bool = True) -> None:
    """Fail fast at boot rather than mid-transaction.

    Callers that have no LLM wired up yet pass require_llm=False.
    """
    missing: list[str] = []
    needed: list[str] = ["POLICY_RECEIPT_HMAC_SECRET"]
    if require_payments:
        needed += ["RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"]
    if require_llm:
        needed += ["GEMINI_API_KEY"]
    for name in needed:
        if not os.environ.get(name):
            missing.append(name)
    if missing:
        raise MissingSecret(
            "Missing required environment variables: " + ", ".join(missing)
        )
