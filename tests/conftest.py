"""Shared pytest fixtures.

Every test runs against Postgres with TRUNCATE isolation — ledger is
append-only so a test never pollutes the next.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Ensure DATABASE_URL is loaded from .env before settings is imported.
# In CI the env var is already set via the workflow `env:` block; locally
# developers rely on .env. Settings reads os.environ at import time, so this
# must run before `store.db` is imported.
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(REPO_ROOT / ".env", override=False)
except Exception:
    try:
        from scripts._env import load_env_file  # type: ignore

        load_env_file()
    except Exception:
        pass

# Also ensure postgres-related env is present for settings import.
if not os.environ.get("DATABASE_URL"):
    try:
        from scripts._env import parse_env_file  # type: ignore

        _env_vals = parse_env_file(REPO_ROOT / ".env")
        for _k, _v in _env_vals.items():
            if _k == "DATABASE_URL" and _v:
                os.environ.setdefault(_k, _v)
                break
    except Exception:
        pass

from store import catalog as catalog_module  # noqa: E402
from store import db as db_module  # noqa: E402


# ── opt-in live API tests ──────────────────────────────────────────────────────
#
# The default suite is hermetic by design: it needs no credentials, no network,
# and cannot be made flaky by someone else's gateway. That is worth keeping, so
# the tests that talk to the real Razorpay sandbox are deselected unless asked
# for explicitly rather than skipped-if-absent — an opt-out default would mean a
# clean CI run silently proves less than it appears to.


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live-api",
        action="store_true",
        default=False,
        help="run tests marked live_api against the real Razorpay test-mode API",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--live-api"):
        return
    skip = pytest.mark.skip(reason="needs --live-api (real Razorpay test-mode call)")
    for item in items:
        if "live_api" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def real_credentials() -> dict[str, str]:
    """The genuine values from `.env`, read straight off disk.

    Read from the file rather than from `os.environ` on purpose: the autouse
    `test_secrets` fixture below replaces several of these with deterministic
    stand-ins, so a live test that needs the true webhook secret cannot get it
    from the environment any more. It puts back the one value it needs.
    """
    from scripts._env import parse_env_file

    return parse_env_file()


@pytest.fixture
def live_razorpay(real_credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch):
    """A RazorpayClient bound to the real test-mode credentials."""
    from kernel.payments import RazorpayClient

    key_id = real_credentials.get("RAZORPAY_KEY_ID", "")
    key_secret = real_credentials.get("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        pytest.skip("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET absent from .env")

    # Also place them in the environment so code paths that construct their own
    # client (the checkout orchestrator's default factory) find them.
    monkeypatch.setenv("RAZORPAY_KEY_ID", key_id)
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", key_secret)
    return RazorpayClient(key_id=key_id, key_secret=key_secret)


@pytest.fixture(scope="session")
def gemini_api_key(real_credentials: dict[str, str]) -> str:
    """The real Gemini key, from `.env` or from the ambient environment.

    Both sources, because a key exported into the shell is as real as one written
    to a file and a test that only looked at the file would skip for no reason.
    Skips rather than fails when there is none: no key means the live-model tests
    cannot run, and pretending otherwise would be worse than saying so.
    """
    import os

    key = real_credentials.get("GEMINI_API_KEY", "") or os.environ.get(
        "GEMINI_API_KEY", ""
    )
    if not key:
        pytest.skip(
            "GEMINI_API_KEY absent from .env and the environment — the live-model "
            "tests need a real key"
        )
    return key


@pytest.fixture
def live_gemini(gemini_api_key: str, monkeypatch: pytest.MonkeyPatch):
    """A GeminiClient on the real API, with the key in the environment too.

    In the environment as well as on the client because `vyapaari.gemini.
    is_configured()` reads it there, and that predicate is what decides whether
    the offer path attempts a model call at all. A test that only handed the key
    to a client would silently exercise the fallback.
    """
    from vyapaari.gemini import GeminiClient, is_configured

    monkeypatch.setenv("GEMINI_API_KEY", gemini_api_key)
    if not is_configured():
        pytest.skip("google-genai is not installed")
    return GeminiClient(api_key=gemini_api_key)


@pytest.fixture
def counting_gemini(live_gemini):
    """The real client, wrapped so a test can see and reshape what it returned.

    Two things this makes possible. It counts calls, which is how the retry path
    is proved to have made two genuine round trips rather than one. And it takes a
    `transform`, so a test can corrupt a real model response on its way back —
    which is how the kernel's veto is tested against misbehaviour a well-behaved
    model will not reliably produce on demand. The call is real either way; only
    what comes back is rewritten.
    """

    class CountingGemini:
        def __init__(self, inner):
            self._inner = inner
            self.model = inner.model
            self.calls: list[dict[str, str]] = []
            self.responses: list[str] = []
            self.transform = None

        def generate(self, *, system: str, user: str, response_schema) -> str:
            raw = self._inner.generate(
                system=system, user=user, response_schema=response_schema
            )
            self.calls.append({"system": system, "user": user})
            self.responses.append(raw)
            if self.transform is None:
                return raw
            return self.transform(raw, len(self.calls))

    return CountingGemini(live_gemini)



@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Iterator:
    """A fresh database with the schema and the 14 seeded SKUs. Postgres-only."""
    from store.db import TABLES

    db_module.reset_connection()
    conn = db_module.get_connection()
    db_module.init_db(conn)
    # TRUNCATE for isolation — transaction() already commits, so no extra COMMIT.
    # Extra COMMIT (conn.execute("COMMIT") mixed SQL with psycopg2 state) was
    # the hang on Postgres and is removed.
    with db_module.transaction(conn):
        for table in reversed(TABLES):
            try:
                conn.execute(f"TRUNCATE {table} CASCADE")
            except Exception:
                try:
                    conn.execute(f"DELETE FROM {table}")
                except Exception:
                    pass
    catalog_module.seed_database(conn=conn)
    catalog_module.cache.load(conn)
    try:
        yield conn
    finally:
        db_module.reset_connection()


@pytest.fixture
def client(db):
    """TestClient over the real app factory, bound to the throwaway database."""
    from fastapi.testclient import TestClient

    from api.app import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


# ── secrets ────────────────────────────────────────────────────────────────────

#: Deterministic stand-ins so signing and verification are reproducible across
#: runs. Razorpay's API keys are deliberately absent: no test may reach the live
#: transport, and a test that needs a client builds one with explicit test keys.
TEST_SECRETS = {
    "POLICY_RECEIPT_HMAC_SECRET": "test-hmac-secret-do-not-use-anywhere-real",
    "RAZORPAY_WEBHOOK_SECRET": "test-webhook-secret-0123456789",
    "MANDATE_SIGNING_SEED": "11" * 32,
    "DEMO_KEY": "test-demo-key-0123456789",
}


@pytest.fixture(autouse=True)
def test_secrets(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Give every test a fixed set of signing secrets.

    Autouse because a receipt signed with a random per-process key cannot be
    asserted against, and because a test that silently signed with nothing would
    pass for the wrong reason. A test that needs a secret absent deletes it with
    `monkeypatch.delenv`.
    """
    for name, value in TEST_SECRETS.items():
        monkeypatch.setenv(name, value)
    return dict(TEST_SECRETS)


# ── a stub Razorpay ────────────────────────────────────────────────────────────


class FakeRazorpay:
    """Records calls instead of making them.

    Every method returns the same shape `kernel.payments` normalises to, so the
    orchestrator cannot tell the difference — which is the point. `calls` is the
    assertion surface: idempotency is proved by its length, not by a mock library's
    call_count.
    """

    def __init__(
        self,
        *,
        key_id: str = "rzp_test_fake",
        capture_status: str = "captured",
        payment_amount_inr: int | None = None,
        already_captured: bool = False,
        payment_order_id: str | None = None,
    ):
        self.key_id = key_id
        self.calls: list[tuple[str, tuple, dict]] = []
        self._capture_status = capture_status
        self._counter = 0
        # What `fetch_payment` reports. Settle reads the amount off the stored
        # order and compares, so a test that wants the mismatch branch sets this.
        self.payment_amount_inr = payment_amount_inr
        self.already_captured = already_captured
        # Which gateway order `fetch_payment` claims the payment was made against.
        # Defaults to the last order this stub created, which is what a real
        # browser payment against that order would report. A test that wants the
        # order-mismatch branch passes an unrelated id.
        self.payment_order_id = payment_order_id
        self.last_order_id: str | None = None

    def _next(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_fake{self._counter:04d}"

    def _record(self, name: str, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def create_order(self, amount_inr: int, *, receipt: str, notes=None, currency="INR"):
        self._record("create_order", amount_inr, receipt=receipt, notes=notes)
        order_id = self._next("order")
        self.last_order_id = order_id
        return {
            "id": order_id,
            "amount_inr": amount_inr,
            "currency": currency,
            "receipt": receipt,
            "status": "created",
        }

    def capture_payment(self, payment_id: str, amount_inr: int, *, currency="INR"):
        self._record("capture_payment", payment_id, amount_inr)
        return {
            "id": payment_id,
            "amount_inr": amount_inr,
            "currency": currency,
            "status": self._capture_status,
            "captured": self._capture_status == "captured",
            "method": "card",
        }

    def fetch_payment(self, payment_id: str):
        """An authorized, not-yet-captured payment, as the real client normalises it.

        `captured`, `amount_inr`, and `order_id` are all present because `settle`
        branches on the first, reconciles the second against the stored order, and
        checks the third binds the payment to that order. A stub that omitted any
        of them would let a settle bug pass here and fail against the real gateway
        — `order_id` was in fact missing, and its absence meant nothing could test
        that a payment for an unrelated order is refused.
        """
        self._record("fetch_payment", payment_id)
        return {
            "id": payment_id,
            "order_id": self.payment_order_id or self.last_order_id,
            "status": "captured" if self.already_captured else "authorized",
            "amount_inr": self.payment_amount_inr,
            "currency": "INR",
            "captured": self.already_captured,
            "method": "card",
        }

    def fetch_order(self, order_id: str):
        self._record("fetch_order", order_id)
        return {"id": order_id, "status": "created", "amount_inr": 0}

    def fetch_order_payments(self, order_id: str):
        self._record("fetch_order_payments", order_id)
        return []

    def refund_payment(self, payment_id: str, **kwargs):
        self._record("refund_payment", payment_id, **kwargs)
        return {"id": self._next("rfnd"), "payment_id": payment_id, "status": "processed"}


class ForbiddenRazorpay(FakeRazorpay):
    """A client that fails the test if anything touches it.

    Shadow mode's whole claim is that no Razorpay call happens. Asserting on an
    empty call list would pass if the code never got that far; raising proves the
    path ran and still did not call out.
    """

    def _record(self, name: str, *args, **kwargs) -> None:
        raise AssertionError(
            f"Razorpay.{name} was called while POLICY_MODE=shadow. Shadow mode "
            "must compute the verdict and call nothing."
        )


@pytest.fixture
def fake_razorpay() -> FakeRazorpay:
    return FakeRazorpay()


@pytest.fixture
def forbidden_razorpay() -> ForbiddenRazorpay:
    return ForbiddenRazorpay()


# ── offers ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def make_offer(db):  # type: ignore[no-untyped-def]
    """Store a bounded, gated, signed offer and return it.

    Delegates to the seeding module the demo uses, so a test and a live demo
    exercise the same offer-construction path. Returns the summary dict, which
    carries the offer id, the tier, and the totals.
    """
    from scripts import seed_offer

    def _make(key: str = "tier0", **overrides):
        scenario = seed_offer.SCENARIOS_BY_KEY[key]
        if overrides:
            scenario = replace(scenario, **overrides)
        return seed_offer.seed(scenario, conn=db)

    return _make


# ── policy mode ────────────────────────────────────────────────────────────────


@pytest.fixture
def live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flip the kernel to live mode for the duration of one test.

    Patched on `settings` rather than on `kernel.mode`, because `mode.current_mode`
    reads the attribute fresh on every call and that indirection is the property
    being relied on. Nothing here reaches a real gateway — the tests that use this
    pass a FakeRazorpay — but it does let stock commit and budget accrue, which is
    the half of the money path shadow mode skips.
    """
    import settings as settings_module
    from settings import PolicyMode

    monkeypatch.setattr(settings_module, "POLICY_MODE", PolicyMode.LIVE)


# ── mandates ───────────────────────────────────────────────────────────────────


@pytest.fixture
def trusted_issuer(db) -> str:  # type: ignore[no-untyped-def]
    """Register the demo issuer in the process registry, as startup does.

    Without this every mandate verifies as UNKNOWN_ISSUER, which escalates rather
    than refusing — so a test that forgot it would see plausible-looking Tier 2
    holds instead of the acceptance or refusal it was checking.
    """
    from mandate.issuers import DEMO_ISSUER_ID, bootstrap_demo_issuer

    bootstrap_demo_issuer()
    return DEMO_ISSUER_ID


@pytest.fixture
def mandate_for(trusted_issuer: str):
    """Issue a signed mandate that covers a cart, with overridable claims.

    Defaults are deliberately generous — limits above any seeded scenario and the
    full category scope — so a test that is not about mandate limits does not have
    to think about them. A test that IS about a specific rejection narrows exactly
    the one claim it cares about and leaves the rest valid, which is what makes the
    failing check unambiguous.
    """
    from mandate import signer

    def _issue(
        *,
        agent_id: str = "agent-test",
        categories=(
            "audio_accessories",
            "cables",
            "charging_accessories",
            "earbud_accessories",
        ),
        max_amount_inr: int = 50_000,
        max_single_txn_inr: int = 50_000,
        **overrides,
    ) -> str:
        return signer.issue(
            subject="user-test",
            agent_id=agent_id,
            categories=categories,
            max_amount_inr=max_amount_inr,
            max_single_txn_inr=max_single_txn_inr,
            **overrides,
        )

    return _issue
