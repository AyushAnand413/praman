"""Grahak — the buyer's agent, and the wallet that authorises it.

This is the counterparty, not part of the store. It holds the buyer's spending
authority, reads the store's published manifest to learn what will be asked of
it, and walks the rail the store advertises: discovery, catalog, offer, checkout,
and a poll for anything held.

Two separations are deliberate.

**The wallet is not the agent.** `Wallet` holds the buyer's limits and is the only
side that signs; `Grahak` holds no key and cannot mint authority for itself. It
asks, and the wallet may refuse. That mirrors, on the buyer's side of the wire,
the same split the store keeps between the model that proposes and the kernel
that decides.

**Nothing here imports the merchant's constants.** The mandate threshold, the
endpoint paths, and the latency hints are read from
`/.well-known/agent-commerce.json`, because a real buyer agent has no access to
`settings.py`. Whether to attach a mandate is decided from the `gate` the store
publishes on each option, so if the store tightens its policy this agent follows
without being edited.

The agent never states a price. It names an offer and an option; the amount comes
from the store's own stored row. There is no field here to put a price in, and
that is the property being demonstrated.

`transport` is anything with httpx's `get`/`post` — a live `httpx.Client` against
a running server, or a `TestClient` over the ASGI app. Both are real requests
through the real routes.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Iterable, Protocol, Sequence

from mandate import signer
from mandate.issuers import DEMO_ISSUER_ID

#: Where a store that speaks this protocol publishes what it can do.
MANIFEST_PATH = "/.well-known/agent-commerce.json"

#: Paths used when the manifest has not been fetched, or does not name one. The
#: manifest is authoritative; these keep a caller that skipped discovery working
#: rather than silently doing nothing.
DEFAULT_PATHS = {
    "catalog": "/agent/v1/catalog",
    "offer": "/agent/v1/offer",
    "checkout": "/agent/v1/checkout",
    "order_status": "/agent/v1/order/{id}",
}


# ---------------------------------------------------------------------------
# What the store can say back
# ---------------------------------------------------------------------------


class StoreRefused(RuntimeError):
    """The store declined, and said why.

    Carries the code and, for a policy refusal, the bound numbers behind it —
    which is the difference between an agent that can adjust its request and one
    that can only retry the same thing harder.
    """

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        bounds: Sequence[int] = (),
        retry_after_seconds: int | None = None,
        path: str = "",
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.bounds = tuple(bounds)
        self.retry_after_seconds = retry_after_seconds
        self.path = path
        detail = f"{status} {code}: {message}"
        if self.bounds:
            detail += f" (bounds {', '.join(str(b) for b in self.bounds)})"
        super().__init__(detail)


class WalletRefused(RuntimeError):
    """The buyer's own wallet declined to authorise the purchase.

    A refusal from this side never reaches the store. The buyer's limits and the
    store's are independent, both apply, and the tighter one wins — which is the
    reason for having two.
    """


class Transport(Protocol):
    """The part of an HTTP client this agent uses."""

    def get(self, url: str, **kwargs: Any) -> Any: ...

    def post(self, url: str, **kwargs: Any) -> Any: ...


def _body_of(response: Any) -> Any:
    """The decoded body, or an empty dict if there wasn't one."""
    try:
        return response.json()
    except Exception:
        return {}


def _fill(template: str, value: str) -> str:
    """Substitute a published path template's single placeholder.

    The manifest writes the order-status path as a template, and which word sits
    inside the braces is the store's choice, not this agent's. Replacing whatever
    is there means a store that renames the placeholder does not break its
    buyers.
    """
    start = template.find("{")
    end = template.find("}", start + 1)
    if start == -1 or end == -1:
        return f"{template.rstrip('/')}/{value}"
    return template[:start] + value + template[end + 1 :]


def _refusal(status: int, body: Any, path: str) -> StoreRefused:
    """Turn an error response into something an agent can branch on."""
    detail = body.get("detail") if isinstance(body, dict) else None

    if isinstance(detail, dict):
        raw_bounds = detail.get("rejecting_bounds") or ()
        bounds: list[int] = []
        for value in raw_bounds:
            try:
                bounds.append(int(value))
            except (TypeError, ValueError):
                continue
        retry = detail.get("retry_after_seconds")
        return StoreRefused(
            status=status,
            code=str(detail.get("code") or "error"),
            message=str(detail.get("message") or "") or f"{status} from {path}",
            bounds=bounds,
            retry_after_seconds=int(retry) if isinstance(retry, int) else None,
            path=path,
        )

    if isinstance(detail, list):
        # The request schema rejected a field. Naming it is the whole value of
        # this branch: an agent that sent an amount should be told the field does
        # not exist, not handed a bare 422.
        fields = ", ".join(
            ".".join(str(part) for part in entry.get("loc", ()))
            for entry in detail
            if isinstance(entry, dict)
        )
        return StoreRefused(
            status=status,
            code="invalid_request",
            message=f"request rejected on {fields}" if fields else "request rejected",
            path=path,
        )

    return StoreRefused(
        status=status,
        code="error",
        message=str(body)[:300] if body else f"{status} from {path}",
        path=path,
    )


# ---------------------------------------------------------------------------
# The buyer's authority
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Wallet:
    """The buyer's spending authority, and the only thing that signs.

    The key material is not held here: `mandate.signer` reads it through
    `mandate.keys`, so this object can be logged or copied without carrying a
    secret. What it does hold is the limits a human set on the agent, and it
    checks them before signing rather than hoping the store will.

    `allowed_categories` of `None` means "whatever this store sells" — the scope
    is still derived from the actual cart, never widened to a wildcard, because a
    mandate that quietly authorises everything is the failure the mandate layer
    exists to prevent.
    """

    owner: str = "buyer-demo"
    max_amount_inr: int = 25_000
    max_single_txn_inr: int = 15_000
    ttl_seconds: int = 900
    allowed_categories: tuple[str, ...] | None = None
    issuer: str = DEMO_ISSUER_ID
    signing_key: Any = None

    def authorise(
        self,
        *,
        agent_id: str,
        amount_inr: int,
        categories: Iterable[str],
    ) -> str:
        """Sign a single-use mandate for exactly this cart, or refuse.

        Scoped to the categories actually being bought and capped at this
        purchase's own amount, so a token that leaks buys nothing else. The store
        applies its own limits afterwards; these are the buyer's.
        """
        scoped = tuple(sorted({c for c in categories if c}))
        if not scoped:
            raise WalletRefused(
                "cannot scope a mandate: the cart's categories are unknown"
            )
        if amount_inr > self.max_single_txn_inr:
            raise WalletRefused(
                f"{amount_inr} INR exceeds this wallet's single-transaction "
                f"limit of {self.max_single_txn_inr} INR"
            )
        if amount_inr > self.max_amount_inr:
            raise WalletRefused(
                f"{amount_inr} INR exceeds this wallet's total limit of "
                f"{self.max_amount_inr} INR"
            )
        if self.allowed_categories is not None:
            outside = [c for c in scoped if c not in self.allowed_categories]
            if outside:
                raise WalletRefused(
                    f"this wallet does not cover category/ies {outside}"
                )
        return signer.issue(
            subject=self.owner,
            agent_id=agent_id,
            categories=scoped,
            # Capped at the cart, not at the wallet's ceiling: the narrowest
            # authority that still completes this purchase.
            max_amount_inr=min(self.max_amount_inr, max(amount_inr, 1)),
            max_single_txn_inr=min(self.max_single_txn_inr, max(amount_inr, 1)),
            issuer=self.issuer,
            ttl_seconds=self.ttl_seconds,
            signing_key=self.signing_key,
        )


# ---------------------------------------------------------------------------
# What the agent gets back
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Discovery:
    """The store's manifest, as the agent understood it."""

    manifest: dict[str, Any]
    latency_ms: int

    @property
    def endpoints(self) -> dict[str, Any]:
        endpoints = self.manifest.get("endpoints")
        return endpoints if isinstance(endpoints, dict) else {}

    @property
    def mandate_required_above_inr(self) -> int | None:
        auth = self.manifest.get("auth")
        mandate = auth.get("mandate") if isinstance(auth, dict) else None
        value = mandate.get("required_above_inr") if isinstance(mandate, dict) else None
        return int(value) if isinstance(value, int) else None

    @property
    def latency_hints_ms(self) -> dict[str, int]:
        hints = self.manifest.get("latency_hints_ms")
        return dict(hints) if isinstance(hints, dict) else {}

    def path(self, name: str) -> str:
        """The path the store publishes for `name`, or the conventional one."""
        value = self.endpoints.get(name)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for key in ("path", "url", "href"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    return candidate
        return DEFAULT_PATHS.get(name, f"/agent/v1/{name}")


@dataclass(frozen=True)
class Offer:
    """An offer as received, with the buyer's own measurement of how long it took.

    `latency_ms` is timed on this side of the wire on purpose. The store reports
    its own number too, and a buyer that only ever read that one would be taking
    the seller's word for the thing it is meant to be checking.
    """

    payload: dict[str, Any]
    latency_ms: int

    @property
    def offer_id(self) -> str:
        return str(self.payload["offer_id"])

    @property
    def session_id(self) -> str:
        return str(self.payload["session_id"])

    @property
    def options(self) -> list[dict[str, Any]]:
        return list(self.payload.get("options", []))

    @property
    def expires_in_seconds(self) -> int:
        return int(self.payload.get("expires_in_seconds", 0))

    @property
    def audit_url(self) -> str:
        return str(self.payload.get("audit_url", ""))

    @property
    def proposal_source(self) -> str:
        proposal = self.payload.get("proposal")
        return str(proposal.get("source", "")) if isinstance(proposal, dict) else ""

    @property
    def recommended(self) -> dict[str, Any]:
        wanted = self.payload.get("recommended_option_id")
        for option in self.options:
            if option.get("option_id") == wanted:
                return option
        for option in self.options:
            if option.get("recommended"):
                return option
        return self.options[0]

    def option(self, option_id: str) -> dict[str, Any]:
        for option in self.options:
            if option.get("option_id") == option_id:
                return option
        raise KeyError(f"this offer has no option {option_id!r}")

    def cheapest(self) -> dict[str, Any]:
        return min(self.options, key=lambda o: int(o["total_inr"]))

    def within(self, budget_inr: int | None) -> dict[str, Any] | None:
        """The best option at or under `budget_inr`, or None if none is.

        Best means most items for the money rather than merely cheapest: an
        upsell that fits the budget is still the buyer getting more of what it
        came for.
        """
        if budget_inr is None:
            return self.recommended
        affordable = [o for o in self.options if int(o["total_inr"]) <= budget_inr]
        if not affordable:
            return None
        return max(affordable, key=lambda o: (len(o.get("items", [])), int(o["total_inr"])))

    def skus(self, option_id: str) -> tuple[str, ...]:
        return tuple(str(item["sku"]) for item in self.option(option_id).get("items", []))


@dataclass(frozen=True)
class Purchase:
    """The outcome of a checkout, held or paid or awaiting payment."""

    order_id: str
    status: str
    state: str
    amount_inr: int
    gate_tier: int
    policy_mode: str
    idempotency_key: str
    mandate_used: bool
    payload: dict[str, Any]

    @property
    def audit_url(self) -> str:
        return str(self.payload.get("audit_url", ""))

    @property
    def poll_url(self) -> str | None:
        value = self.payload.get("poll_url")
        return str(value) if value else None

    @property
    def razorpay(self) -> dict[str, Any]:
        value = self.payload.get("razorpay")
        return dict(value) if isinstance(value, dict) else {}

    @property
    def held_for_human(self) -> bool:
        return self.status == "pending_approval" or bool(self.payload.get("approval_id"))

    @property
    def awaiting_payment(self) -> bool:
        return self.status == "awaiting_payment"

    @property
    def would_have_charged(self) -> bool:
        return bool(self.payload.get("would_have_charged"))


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Persona:
    """One kind of buyer, as a request shape rather than a personality.

    The store sees only what is in these fields, so that is all a persona is: a
    need in the buyer's own words, how many, what it can spend, and how it picks
    between the options it is offered. `prefers` is the buyer's own policy —
    `cheapest` takes the lowest total, `recommended` takes the store's pick,
    `budget` takes the most it can get for its stated budget.
    """

    name: str
    need: str
    qty: int = 1
    budget_inr: int | None = None
    category: str | None = None
    delivery: str | None = None
    prefers: str = "recommended"
    wallet_limit_inr: int = 25_000

    def choose(self, offer: Offer) -> dict[str, Any]:
        """Pick an option, the way this buyer would."""
        if self.prefers == "cheapest":
            return offer.cheapest()
        if self.prefers == "budget":
            chosen = offer.within(self.budget_inr)
            return chosen if chosen is not None else offer.cheapest()
        return offer.recommended

    def wallet(self) -> Wallet:
        return Wallet(
            owner=f"buyer-{self.name}",
            max_amount_inr=self.wallet_limit_inr,
            max_single_txn_inr=self.wallet_limit_inr,
        )


#: Eight buyers who want different things and say so differently. Between them
#: they exercise a single item and a bulk order, a hard budget and none at all, a
#: named SKU and a vague description, an accessory and a flagship — which is the
#: spread the offer path has to hold up across.
PERSONAS: tuple[Persona, ...] = (
    Persona(
        name="budget_tight",
        need=(
            "I need working earbuds for commuting and I cannot go over two "
            "thousand rupees. Cheapest thing that actually works is fine."
        ),
        budget_inr=2_000,
        prefers="cheapest",
        wallet_limit_inr=3_000,
    ),
    Persona(
        name="feature_led",
        need=(
            "Looking for over-ear headphones with active noise cancellation for "
            "open-plan office work. ANC matters more than price."
        ),
        prefers="recommended",
    ),
    Persona(
        name="gift_buyer",
        need=(
            "Buying headphones as a birthday gift for my brother. It has to be "
            "returnable in case he does not like them, and it should feel like a "
            "proper present."
        ),
        budget_inr=6_000,
        prefers="budget",
    ),
    Persona(
        name="bulk",
        need=(
            "We are kitting out a support team and need eight identical headsets "
            "with a microphone. Same model for everyone, please."
        ),
        qty=8,
        prefers="cheapest",
        wallet_limit_inr=40_000,
    ),
    Persona(
        name="brand_agnostic",
        need=(
            "I do not care what it is called. I want something that plays audio "
            "in my ears without wires and lasts a full working day."
        ),
        prefers="recommended",
    ),
    Persona(
        name="upgrade_seeker",
        need=(
            "I already have your entry-level pair and I want to move up to the "
            "studio model. Tell me what the better one gets me."
        ),
        prefers="recommended",
        wallet_limit_inr=30_000,
    ),
    Persona(
        name="replacement_part",
        need=(
            "I lost one of the silicone tips for my earbuds. I just need the "
            "replacement tips, nothing else."
        ),
        category="earbud_accessories",
        prefers="cheapest",
        wallet_limit_inr=2_000,
    ),
    Persona(
        name="deadline_driven",
        need=(
            "I am presenting on Thursday and my headset died. Need a wired "
            "headset that will arrive before then, and I need to know it will."
        ),
        delivery="express",
        budget_inr=4_000,
        prefers="budget",
    ),
)

PERSONAS_BY_NAME: dict[str, Persona] = {p.name: p for p in PERSONAS}


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class Grahak:
    """A buyer agent that shops the way the store says to.

    Stateful only in the ways a real agent would be: it remembers the session the
    store gave it so its offers are counted against one session, it remembers the
    categories it learned while browsing so it can scope a mandate to the cart,
    and it keeps the idempotency key it used for each purchase so a retry reuses
    it rather than minting a second one.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        wallet: Wallet | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.transport = transport
        self.wallet = wallet or Wallet()
        self.agent_id = agent_id or f"grahak-{secrets.token_hex(4)}"
        self.session_id = session_id
        self.discovery: Discovery | None = None
        self.offers_requested = 0
        self._categories: dict[str, str] = {}

    # -- transport ---------------------------------------------------------

    def _get(self, path: str) -> tuple[Any, int]:
        started = time.perf_counter()
        response = self.transport.get(path)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body = _body_of(response)
        if response.status_code >= 400:
            raise _refusal(response.status_code, body, path)
        return body, elapsed_ms

    def _post(
        self, path: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None
    ) -> tuple[Any, int]:
        started = time.perf_counter()
        response = self.transport.post(path, json=payload, headers=headers)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body = _body_of(response)
        if response.status_code >= 400:
            raise _refusal(response.status_code, body, path)
        return body, elapsed_ms

    def _path(self, name: str) -> str:
        if self.discovery is not None:
            return self.discovery.path(name)
        return DEFAULT_PATHS.get(name, f"/agent/v1/{name}")

    # -- the rail ----------------------------------------------------------

    def discover(self) -> Discovery:
        """Read the manifest and keep it. Everything after this uses its paths."""
        manifest, elapsed_ms = self._get(MANIFEST_PATH)
        self.discovery = Discovery(manifest=dict(manifest), latency_ms=elapsed_ms)
        return self.discovery

    def browse(
        self,
        need: str = "",
        *,
        budget_inr: int | None = None,
        category: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Search the catalog. Free, needs no authority, and teaches categories."""
        payload: dict[str, Any] = {"need": need, "agent_id": self.agent_id}
        if budget_inr is not None:
            payload["budget_inr"] = budget_inr
        if category is not None:
            payload["category"] = category
        if limit is not None:
            payload["limit"] = limit
        body, _ = self._post(self._path("catalog"), payload)
        results = list(body.get("results", []))
        for row in results:
            sku, row_category = row.get("sku"), row.get("category")
            if sku and row_category:
                self._categories[str(sku)] = str(row_category)
        return results

    def request_offer(
        self,
        need: str,
        *,
        qty: int = 1,
        base_sku: str | None = None,
        category: str | None = None,
        budget_inr: int | None = None,
        delivery: str | None = None,
    ) -> Offer:
        """Ask for a price. The store decides the terms; this states the need."""
        payload: dict[str, Any] = {"need": need, "agent_id": self.agent_id, "qty": qty}
        if self.session_id:
            payload["session_id"] = self.session_id
        for key, value in (
            ("base_sku", base_sku),
            ("category", category),
            ("budget_inr", budget_inr),
            ("delivery", delivery),
        ):
            if value is not None:
                payload[key] = value

        body, elapsed_ms = self._post(self._path("offer"), payload)
        self.offers_requested += 1
        offer = Offer(payload=dict(body), latency_ms=elapsed_ms)
        self.session_id = offer.session_id
        return offer

    def buy(
        self,
        offer: Offer,
        option_id: str | None = None,
        *,
        idempotency_key: str | None = None,
        mandate: str | None = None,
    ) -> Purchase:
        """Accept one option.

        No amount is sent — the store reads the price from the offer it stored.
        A mandate is attached when the option's own gate says one is required,
        which means the store's threshold is honoured without this agent knowing
        what the threshold is.
        """
        option = offer.option(option_id) if option_id else offer.recommended
        chosen_id = str(option["option_id"])
        amount_inr = int(option["total_inr"])
        key = idempotency_key or f"grahak-{secrets.token_hex(12)}"

        gate = option.get("gate") or {}
        needs_mandate = bool(gate.get("requires_mandate"))
        token = mandate
        if token is None and needs_mandate:
            token = self.wallet.authorise(
                agent_id=self.agent_id,
                amount_inr=amount_inr,
                categories=self._categories_for(offer.skus(chosen_id)),
            )

        payload: dict[str, Any] = {
            "offer_id": offer.offer_id,
            "option_id": chosen_id,
            "agent_id": self.agent_id,
        }
        if token:
            payload["mandate"] = token

        body, _ = self._post(
            self._path("checkout"), payload, headers={"Idempotency-Key": key}
        )
        return Purchase(
            order_id=str(body["order_id"]),
            status=str(body["status"]),
            state=str(body.get("state", "")),
            amount_inr=int(body["amount_inr"]),
            gate_tier=int(body.get("gate_tier", 0)),
            policy_mode=str(body.get("policy_mode", "")),
            idempotency_key=key,
            mandate_used=bool(token),
            payload=dict(body),
        )

    def check(self, order_id: str) -> dict[str, Any]:
        """Poll an order. Polling never advances anything."""
        body, _ = self._get(_fill(self._path("order_status"), order_id))
        return dict(body)

    # -- composed ----------------------------------------------------------

    def shop(
        self,
        need: str,
        *,
        qty: int = 1,
        budget_inr: int | None = None,
        category: str | None = None,
        delivery: str | None = None,
        choose=None,
    ) -> tuple[Offer, Purchase]:
        """The whole rail: discover, browse, ask, buy.

        Browsing before asking is not decoration. It is how the agent learns the
        categories it needs to scope a mandate to the cart it is actually buying,
        which is the difference between a narrow authority and a blank cheque.
        """
        if self.discovery is None:
            self.discover()
        self.browse(need, budget_inr=budget_inr, category=category)
        offer = self.request_offer(
            need, qty=qty, budget_inr=budget_inr, category=category, delivery=delivery
        )
        option = choose(offer) if choose else offer.recommended
        purchase = self.buy(offer, str(option["option_id"]))
        return offer, purchase

    def shop_as(self, persona: Persona) -> tuple[Offer, Purchase]:
        """Shop as one persona, using its wallet and its way of choosing."""
        self.wallet = persona.wallet()
        return self.shop(
            persona.need,
            qty=persona.qty,
            budget_inr=persona.budget_inr,
            category=persona.category,
            delivery=persona.delivery,
            choose=persona.choose,
        )

    # -- internals ---------------------------------------------------------

    def _categories_for(self, skus: Sequence[str]) -> tuple[str, ...]:
        """Categories for these SKUs, browsing again for any it has not seen.

        An upsell can name a SKU the agent never searched for. Looking it up is
        cheap and correct; assuming a wildcard scope would not be.
        """
        missing = [sku for sku in skus if sku not in self._categories]
        for sku in missing:
            self.browse(sku)
        unknown = [sku for sku in skus if sku not in self._categories]
        if unknown:
            raise WalletRefused(
                f"cannot scope a mandate: the store did not say what category "
                f"{unknown} belongs to"
            )
        return tuple(self._categories[sku] for sku in skus)


def wallet_for(persona: Persona) -> Wallet:
    """The wallet a persona shops with."""
    return persona.wallet()
