"""Offer assembly — turning a proposal into something the store can be held to.

A proposal is what the model wants to sell. An offer is what this store has
committed to: named products, a fixed price, a fixed window, and a signed record
of why. This module is the step between the two, and it is deliberately the only
step. The model never reaches the database, and nothing that reaches the database
has ever taken a model at its word.

The ladder, in order:

1. Resolve every proposed SKU against the selling envelope. A SKU that is not
   there cannot be priced — so an invented base SKU is a refusal, and an invented
   upsell is a dropped line.
2. Turn percentages into whole rupees, rounding toward the merchant.
3. Bound the base item on its own. If the base fails, the offer fails; there is
   nothing left to sell.
4. Bound the base plus the upsells. Per-item bounds prune the lines that failed
   and the cart bounds then run against what survived, so an over-priced extra
   costs the buyer nothing.
5. Gate each surviving option on its own total and its own discount.
6. Sign one receipt, store the offer, and write the trail.

Two options come out. Option A is what the buyer asked for. Option B is that plus
whatever survived, and it is the one marked `recommended` — this store is allowed
to recommend, and the buyer is handed both prices plus the ledger link to check
the recommendation against.

What this module never does is second-guess the kernel on the kernel's behalf.
Nothing here filters a proposal before the bounds see it. A discount past the cap
is priced, submitted, refused, and written down. That is the difference between a
veto that works and a veto nobody has ever tested.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Iterable, Mapping, Sequence

import settings
from kernel import budgets, mode
from kernel import reasons as prose
from kernel import receipt as receipts
from kernel import stock
from kernel.bounds import (
    ROLE_BASE,
    ROLE_UPSELL,
    BoundResult,
    CartEvaluation,
    LineItem,
    evaluate_offer,
)
from kernel.gates import GateDecision, assign_tier
from store import catalog, ids, ledger, offers, sessions
from store.db import get_connection
from store.timestamps import utc_now
from vyapaari import envelope as envelope_module
from vyapaari import proposer
from vyapaari.envelope import SellableSku
from vyapaari.prompt import ProposalRequest
from vyapaari.schema import Proposal, ProposedItem

#: Option ids. A is the thing that was asked for; B is A plus what survived.
OPTION_BASE = "A"
OPTION_BUNDLE = "B"

#: Why a proposed line is not in the offer.
DROPPED_UNKNOWN_SKU = "unknown_sku"
DROPPED_BOUND_REJECTED = "bound_rejected"

#: Who wrote the sentence the buyer reads. Recorded per line, because "the model
#: said this" and "the store says this" are different claims.
PROSE_MODEL = "model"
PROSE_STORE = "store"

#: Refusal codes. Stable strings an agent can branch on without parsing English.
CODE_UNKNOWN_SKU = "unknown_sku"
CODE_POLICY_REFUSED = "policy_refused"
CODE_NO_MATCH = "no_match"


class OfferRefused(RuntimeError):
    """No offer could be made, and the reason is part of the answer.

    Carries an HTTP status because the two ways this fails deserve different
    ones: nothing in the catalog fits the request (404) is the buyer learning the
    store does not stock what they want, while a policy refusal (409) is the
    store declining to sell on terms it will not accept.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        http_status: int = 409,
        bounds: Iterable[int] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.bounds = tuple(bounds)

    def as_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "rejecting_bounds": list(self.bounds),
        }


def need_fingerprint(need: str) -> str:
    """A stable handle for the buyer's words, for the public trail.

    Not a hiding place: a three-word shopping request is guessable from its
    digest, and claiming otherwise would be the kind of security theatre this
    project is supposed to argue against. What it buys is that the buyer's exact
    sentence is not republished verbatim in an endpoint anyone can read, while
    two requests for the same thing still line up in an audit.
    """
    return sha256(" ".join(need.split()).lower().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def unit_discount_inr(list_price_inr: int, discount_pct: Decimal) -> int:
    """Whole rupees off one unit, rounded down.

    Rounded toward the merchant: 3.45% of Rs 4,999 is Rs 172, not Rs 173.
    Rounding a percentage up would price the line a rupee below what the per-SKU
    cap permits, and the kernel would then refuse a discount the model was
    explicitly told it could give.
    """
    if discount_pct <= 0:
        return 0
    return int(Decimal(int(list_price_inr)) * Decimal(discount_pct) / Decimal(100))


def _line_item(item: ProposedItem, sellable: SellableSku, *, role: str) -> LineItem:
    return LineItem(
        sku=sellable.sku,
        qty=item.qty,
        list_price_inr=sellable.list_price_inr,
        offered_price_inr=sellable.list_price_inr
        - unit_discount_inr(sellable.list_price_inr, item.discount_pct),
        role=role,
    )


# ---------------------------------------------------------------------------
# The pieces of an assembled offer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DroppedLine:
    """A line the model proposed that the buyer will never see."""

    sku: str
    reason: str
    detail: str
    bound: int | None = None
    bound_id: str | None = None

    def as_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "sku": self.sku,
            "reason": self.reason,
            "detail": self.detail,
        }
        if self.bound is not None:
            body["bound"] = self.bound
            body["bound_id"] = self.bound_id
        return body


@dataclass(frozen=True)
class LineProse:
    """The sentence shown for one line, and who wrote it."""

    sku: str
    why: str
    source: str
    refusal: str | None = None

    def as_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"sku": self.sku, "source": self.source}
        if self.refusal:
            body["refused"] = self.refusal
        return body


@dataclass(frozen=True)
class _LineSpec:
    """One priced line plus everything needed to describe it."""

    proposed: ProposedItem
    sellable: SellableSku
    upsell_type: str | None = None
    popularity_pct: int | None = None


@dataclass(frozen=True)
class AssembledOption:
    """One thing the buyer may buy, bounded and gated on its own terms."""

    option_id: str
    evaluation: CartEvaluation
    gate: GateDecision
    prose: tuple[LineProse, ...]
    sellables: tuple[SellableSku, ...]
    machine_rationale: Mapping[str, Any]
    recommended: bool = False

    @property
    def items(self) -> tuple[LineItem, ...]:
        return self.evaluation.approved_items

    @property
    def total_inr(self) -> int:
        return self.evaluation.total_inr

    @property
    def human_reason(self) -> str:
        """The option in prose, for a human deciding whether to approve it.

        A join of the per-line sentences rather than a separate summary, so the
        paragraph a human reads and the sentences recorded in the receipt are the
        same words. Two independently generated versions of the same explanation
        would eventually disagree, and the one in the receipt is the one that
        claims to be evidence.
        """
        return " ".join(p.why for p in self.prose)

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(p.why for p in self.prose)

    def as_payload(self) -> dict[str, Any]:
        """The option as stored and as served.

        Carries the five fields the checkout orchestrator reads back per line —
        sku, qty, list price, offered price, role — because that row is the
        amount authority and it must be reconstructible without this module.
        Titles and sentences ride along for the buyer's benefit.

        Both options carry the full set of fields, including the ones the base
        option arguably does not need. An agent should not have to special-case
        which option it is looking at to find out what a purchase would cost.
        """
        titles = {s.sku: s.title for s in self.sellables}
        by_sku = {p.sku: p for p in self.prose}
        return {
            "option_id": self.option_id,
            "items": [
                {
                    "sku": item.sku,
                    "title": titles.get(item.sku, ""),
                    "qty": item.qty,
                    "list_price_inr": item.list_price_inr,
                    "offered_price_inr": item.offered_price_inr,
                    "role": item.role,
                    "why": by_sku[item.sku].why if item.sku in by_sku else "",
                }
                for item in self.items
            ],
            "total_inr": self.evaluation.total_inr,
            "list_total_inr": self.evaluation.list_total_inr,
            "discount_inr": self.evaluation.discount_inr,
            "you_save_inr": self.evaluation.discount_inr,
            "recommended": self.recommended,
            "human_reason": self.human_reason,
            "machine_rationale": dict(self.machine_rationale),
            "gate": self.gate.as_payload(),
            "prose_sources": [p.as_payload() for p in self.prose],
        }


@dataclass(frozen=True)
class Assembly:
    """Every option the bounds allowed, plus what they threw away."""

    options: tuple[AssembledOption, ...]
    dropped: tuple[DroppedLine, ...] = ()
    #: Set when the bundle option existed but a cart-level bound refused it.
    bundle_refusal: str | None = None

    @property
    def recommended(self) -> AssembledOption:
        for option in self.options:
            if option.recommended:
                return option
        return self.options[0]

    @property
    def gate_tier(self) -> int:
        """The strictest tier across the options.

        The offer row holds one tier and the headline option is the recommended
        one, so this is the recommended option's tier in every case that can
        arise today — but taking the maximum states the intent, which is that a
        single summary number must never read looser than any option it summarises.
        """
        return max(option.gate.tier for option in self.options)

    @property
    def reason_refusals(self) -> tuple[dict[str, Any], ...]:
        """Model sentences that were replaced, for the ledger."""
        return tuple(
            {"option_id": option.option_id, **p.as_payload()}
            for option in self.options
            for p in option.prose
            if p.refusal
        )

    def as_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "options": [option.as_payload() for option in self.options],
            "gate_tier": self.gate_tier,
        }
        if self.dropped:
            body["dropped"] = [d.as_payload() for d in self.dropped]
        if self.bundle_refusal:
            body["bundle_refusal"] = self.bundle_refusal
        return body


# ---------------------------------------------------------------------------
# Assembly: kernel-pure, no database, no network
# ---------------------------------------------------------------------------


def _sellable(
    by_sku: Mapping[str, SellableSku],
    private_by_sku: Mapping[str, Mapping[str, Any]],
    sku: str,
) -> SellableSku | None:
    """The envelope entry for `sku`, if it can actually be bounded.

    An envelope entry with no economics row cannot be checked against the floor,
    and a line the kernel cannot bound is a line this store does not sell. Being
    unable to price something safely and refusing to price it are the same
    answer.
    """
    item = by_sku.get(sku)
    if item is None:
        return None
    row = private_by_sku.get(sku)
    if not row or "cost_inr" not in row:
        return None
    return item


def _resolve_prose(
    spec: _LineSpec, private_rows: Sequence[Mapping[str, Any]]
) -> LineProse:
    """The sentence for one line: the model's, or the store's instead.

    The model's prose is checked, never edited. A sentence that has to be
    repaired to be safe was not a sentence worth showing, and a partially
    scrubbed one would still be attributed to the model in the receipt.
    """
    refusal = prose.refusal_reason(spec.proposed.why, private_rows)
    if refusal is None:
        return LineProse(
            sku=spec.sellable.sku,
            why=" ".join(spec.proposed.why.split()),
            source=PROSE_MODEL,
        )
    if spec.upsell_type is None:
        replacement = prose.render_base_reason(title=spec.sellable.title)
    else:
        replacement = prose.render_upsell_reason(
            upsell_type=spec.upsell_type,
            title=spec.sellable.title,
            popularity_pct=spec.popularity_pct,
        )
    return LineProse(
        sku=spec.sellable.sku,
        why=replacement,
        source=PROSE_STORE,
        refusal=refusal,
    )


def _machine_rationale(
    evaluation: CartEvaluation,
    gate: GateDecision,
    specs: Sequence[_LineSpec],
    *,
    budget_inr: int | None,
) -> dict[str, Any]:
    """The same offer, for a reader that computes rather than reads.

    `popularity_pct` is a rounded whole percentage, not the stored attach rate.
    The rounding is the point: how often buyers take an upsell is a sales
    argument and is published deliberately, while the stored figure is a fact
    about this store's data and stays inside the kernel.
    """
    approved = {item.sku for item in evaluation.approved_items}
    included = [s for s in specs if s.sellable.sku in approved]
    body: dict[str, Any] = {
        "discount_pct": str(evaluation.discount_pct),
        # The shortest window across the lines, because that is the one the
        # buyer is actually held to on the cart as a whole.
        "returns_window_days": min(
            (s.sellable.returns_window_days for s in included), default=0
        ),
        "gate_tier": gate.tier,
        "requires_mandate": gate.requires_mandate,
        "requires_human": gate.requires_human,
    }
    if budget_inr is not None:
        body["fits_budget"] = evaluation.total_inr <= budget_inr
        body["budget_headroom_inr"] = budget_inr - evaluation.total_inr
    popularity = [
        s.popularity_pct
        for s in included
        if s.upsell_type is not None and s.popularity_pct
    ]
    if popularity:
        body["popularity_pct"] = max(popularity)
    upsell_types = [s.upsell_type for s in included if s.upsell_type]
    if upsell_types:
        body["upsell_types"] = upsell_types
    return body


def _build_option(
    option_id: str,
    evaluation: CartEvaluation,
    specs: Sequence[_LineSpec],
    *,
    private_rows: Sequence[Mapping[str, Any]],
    budget_inr: int | None,
    recommended: bool,
) -> AssembledOption:
    gate = assign_tier(
        total_inr=evaluation.total_inr,
        discount_pct=evaluation.discount_pct,
        tripped_bounds=evaluation.tripped_bounds,
        mandate_issuer_trusted=None,
        # `agent_first_order` is deliberately not supplied, for the same reason
        # the checkout path withholds it: the only agent identity available here
        # arrived in a request body with nothing vouching for it, so a caller can
        # mint a fresh one per request. Deriving a risk signal from it would stop
        # no adversary while routing every honest agent's first purchase to a
        # human. Feed it once there is an authenticated identity to feed it from.
    )
    by_sku = {s.sellable.sku: s for s in specs}
    approved = [item.sku for item in evaluation.approved_items]
    return AssembledOption(
        option_id=option_id,
        evaluation=evaluation,
        gate=gate,
        prose=tuple(
            _resolve_prose(by_sku[sku], private_rows) for sku in approved
            if sku in by_sku
        ),
        sellables=tuple(by_sku[sku].sellable for sku in approved if sku in by_sku),
        machine_rationale=_machine_rationale(
            evaluation, gate, specs, budget_inr=budget_inr
        ),
        recommended=recommended,
    )


def assemble(
    proposal: Proposal,
    *,
    envelope: Sequence[SellableSku],
    private_by_sku: Mapping[str, Mapping[str, Any]],
    available_by_sku: Mapping[str, int],
    offers_made: int,
    spent_today_inr: int,
    now: datetime | None = None,
    budget_inr: int | None = None,
) -> Assembly:
    """Price a proposal, run it past the bounds, and gate what survives.

    Pure: no database, no network, no clock beyond the one passed in. Everything
    that persists happens in `build_offer`, so this function can be called in a
    test with a hand-written proposal and no environment at all.

    Raises `OfferRefused` when there is nothing to sell.
    """
    moment = now or utc_now()
    by_sku = envelope_module.by_sku(envelope)

    base_sellable = _sellable(by_sku, private_by_sku, proposal.base.sku)
    if base_sellable is None:
        raise OfferRefused(
            f"{proposal.base.sku} is not a SKU this store can sell",
            code=CODE_UNKNOWN_SKU,
        )
    base_spec = _LineSpec(proposed=proposal.base, sellable=base_sellable)
    base_item = _line_item(proposal.base, base_sellable, role=ROLE_BASE)

    attach_pct = {a.sku: a.popularity_pct for a in base_sellable.attach}
    dropped: list[DroppedLine] = []
    upsell_specs: list[_LineSpec] = []
    upsell_items: list[LineItem] = []
    for upsell in proposal.upsells:
        sellable = _sellable(by_sku, private_by_sku, upsell.sku)
        if sellable is None:
            dropped.append(
                DroppedLine(
                    sku=upsell.sku,
                    reason=DROPPED_UNKNOWN_SKU,
                    detail=f"{upsell.sku} is not a SKU this store can sell",
                )
            )
            continue
        upsell_specs.append(
            _LineSpec(
                proposed=upsell,
                sellable=sellable,
                upsell_type=upsell.upsell_type,
                popularity_pct=attach_pct.get(upsell.sku),
            )
        )
        upsell_items.append(_line_item(upsell, sellable, role=ROLE_UPSELL))

    # Scoped to the SKUs actually in play. Scanning every private row in the
    # catalog would be stricter and worse: a three-digit number matching any of
    # fourteen costs by coincidence would suppress a correct sentence, and a
    # check that fires on correct output is a check somebody switches off.
    skus_in_play = [base_item.sku, *(i.sku for i in upsell_items)]
    private_rows = [dict(private_by_sku[sku]) for sku in skus_in_play]

    def evaluate(items: Sequence[LineItem]) -> CartEvaluation:
        return evaluate_offer(
            list(items),
            private_by_sku={i.sku: dict(private_by_sku[i.sku]) for i in items},
            available_by_sku={i.sku: int(available_by_sku.get(i.sku, 0)) for i in items},
            offers_made=offers_made,
            spent_today_inr=spent_today_inr,
            now=moment,
        )

    base_eval = evaluate([base_item])
    if base_eval.offer_failed:
        raise OfferRefused(
            base_eval.failure_detail or "the base item was refused",
            code=CODE_POLICY_REFUSED,
            bounds=base_eval.rejecting_bounds,
        )

    bundle_eval: CartEvaluation | None = None
    bundle_refusal: str | None = None
    if upsell_items:
        candidate = evaluate([base_item, *upsell_items])
        for verdict in candidate.rejected_items:
            if verdict.item.role != ROLE_UPSELL:
                continue
            failed = verdict.failed_bound
            dropped.append(
                DroppedLine(
                    sku=verdict.item.sku,
                    reason=DROPPED_BOUND_REJECTED,
                    detail=failed.detail if failed else "refused by the bounds",
                    bound=failed.bound if failed else None,
                    bound_id=failed.bound_id if failed else None,
                )
            )
        if candidate.offer_failed:
            # The base cleared on its own, so this is a cart-level refusal — the
            # day's discount budget, most likely. Drop the whole bundle rather
            # than trying to shrink it into compliance: guessing which extra to
            # remove is a pricing decision, and pricing is not this layer's.
            bundle_refusal = candidate.failure_detail
        elif len(candidate.approved_items) > 1:
            bundle_eval = candidate

    specs = [base_spec, *upsell_specs]
    options = [
        _build_option(
            OPTION_BASE,
            base_eval,
            specs,
            private_rows=private_rows,
            budget_inr=budget_inr,
            recommended=bundle_eval is None,
        )
    ]
    if bundle_eval is not None:
        options.append(
            _build_option(
                OPTION_BUNDLE,
                bundle_eval,
                specs,
                private_rows=private_rows,
                budget_inr=budget_inr,
                recommended=True,
            )
        )

    return Assembly(
        options=tuple(options),
        dropped=tuple(dropped),
        bundle_refusal=bundle_refusal,
    )


# ---------------------------------------------------------------------------
# The issued offer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedOffer:
    """A stored, signed, expiring offer — the response to an offer request."""

    offer_id: str
    session_id: str
    expires_at: str
    expires_in_seconds: int
    assembly: Assembly
    policy_receipt: Mapping[str, Any]
    proposal_source: str
    proposal_latency_ms: int
    audit_url: str

    @property
    def recommended_option_id(self) -> str:
        return self.assembly.recommended.option_id

    def as_payload(self) -> dict[str, Any]:
        """The offer object, as an agent receives it.

        `policy_receipt` covers the recommended option. Its per-line verdicts
        cover every line in the offer — the base option's lines are a subset of
        the bundle's — and the cart-level bounds of both options are in it, so a
        verifier sees every bound that contributed. Each option still carries its
        own `gate`, because the tier that applies depends on which one is bought
        and a single signed tier could only describe one of them.
        """
        return {
            "offer_id": self.offer_id,
            "session_id": self.session_id,
            "expires_at": self.expires_at,
            "expires_in_seconds": self.expires_in_seconds,
            "recommended_option_id": self.recommended_option_id,
            "receipt_covers_option_id": self.recommended_option_id,
            **self.assembly.as_payload(),
            "policy_receipt": dict(self.policy_receipt),
            "proposal": {
                "source": self.proposal_source,
                "latency_ms": self.proposal_latency_ms,
            },
            "policy_mode": mode.mode_value(),
            "audit_url": self.audit_url,
        }


def audit_url_for(offer_id: str) -> str:
    return f"/audit/{offer_id}"


def build_offer(
    *,
    need: str,
    agent_id: str,
    session_id: str | None = None,
    qty: int = 1,
    base_sku: str | None = None,
    category: str | None = None,
    budget_inr: int | None = None,
    delivery: str | None = None,
    generate: proposer.Generator | None = None,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> IssuedOffer:
    """The offer flow, end to end: ask the model, bound it, sign it, store it.

    The offer id is minted before the model is called, so every entry the flow
    writes — the request, the proposal, the verdicts, the receipt — is reachable
    from one public audit URL. An id assigned only after a successful assembly
    would leave refusals unlinkable, and a refusal is the entry most worth
    finding.

    `generate` is injectable so a test can drive the whole flow, database and
    ledger included, without a network or an API key.
    """
    conn = conn or get_connection()
    moment = now or utc_now()

    session = sessions.ensure(session_id, agent_id=agent_id, conn=conn)
    resolved_session_id = session["session_id"]
    offers_made = sessions.offers_made(resolved_session_id, conn=conn)
    offer_id = ids.offer_id()

    ledger.append(
        "buyer_agent",
        "offer.request",
        {
            "offer_id": offer_id,
            "session_id": resolved_session_id,
            "agent_id": agent_id,
            # The buyer's words are not published. Length and digest are enough
            # to correlate a request with what came back; the sentence itself is
            # the buyer's business and this endpoint is public.
            "need_chars": len(need),
            "need_sha256": need_fingerprint(need),
            "qty": int(qty),
            "base_sku": base_sku,
            "category": category,
            "budget_inr": budget_inr,
            "delivery": delivery,
            "offers_made": offers_made,
        },
        conn=conn,
    )

    public_rows = catalog.cache.all_public()
    private_by_sku: dict[str, dict[str, Any]] = {}
    for row in public_rows:
        private = catalog.cache.private(row["sku"])
        if private is not None:
            private_by_sku[row["sku"]] = dict(private)
    available_by_sku = stock.available_for(
        list(private_by_sku), now=moment, conn=conn
    )
    envelope = envelope_module.build(
        public_rows, private_by_sku, available_by_sku=available_by_sku
    )

    request = ProposalRequest(
        need=need,
        qty=int(qty),
        base_sku=base_sku,
        category=category,
        budget_inr=budget_inr,
        delivery=delivery,
        offers_made=offers_made,
    )
    outcome = proposer.propose(request, envelope, generate=generate)
    ledger.append(
        "vyapaari",
        "proposal.emitted",
        {
            "offer_id": offer_id,
            "session_id": resolved_session_id,
            **outcome.as_payload(),
        },
        conn=conn,
    )

    if outcome.proposal is None:
        refusal = OfferRefused(
            "nothing in the catalog matches this request",
            code=CODE_NO_MATCH,
            http_status=404,
        )
        _ledger_refusal(offer_id, resolved_session_id, refusal, conn=conn)
        raise refusal

    try:
        assembly = assemble(
            outcome.proposal,
            envelope=envelope,
            private_by_sku=private_by_sku,
            available_by_sku=available_by_sku,
            offers_made=offers_made,
            spent_today_inr=budgets.spent(conn=conn),
            now=moment,
            budget_inr=budget_inr,
        )
    except OfferRefused as exc:
        _ledger_refusal(offer_id, resolved_session_id, exc, conn=conn)
        raise

    recommended = assembly.recommended
    ledger.append(
        "policy_kernel",
        "offer.evaluated",
        {
            "offer_id": offer_id,
            "session_id": resolved_session_id,
            "options": [
                {
                    "option_id": option.option_id,
                    **option.evaluation.as_payload(),
                    "gate": option.gate.as_payload(),
                }
                for option in assembly.options
            ],
            "dropped": [d.as_payload() for d in assembly.dropped],
            "prose_replaced": list(assembly.reason_refusals),
            "bundle_refusal": assembly.bundle_refusal,
        },
        conn=conn,
    )

    # Every cart bound from the options that are not the signed one, so the
    # receipt covers each bound that contributed to the offer rather than only
    # the ones the headline option produced.
    extra_bounds: list[BoundResult] = [
        bound
        for option in assembly.options
        if option is not recommended
        for bound in option.evaluation.cart_bounds
    ]
    signed = receipts.issue(
        offer_id=offer_id,
        evaluation=recommended.evaluation,
        gate=recommended.gate,
        reasons=recommended.reasons,
        extra_bounds=extra_bounds,
    )

    stored = offers.create(
        offer_id=offer_id,
        session_id=resolved_session_id,
        base_sku=outcome.proposal.base.sku,
        options=[option.as_payload() for option in assembly.options],
        total_inr=recommended.total_inr,
        gate_tier=assembly.gate_tier,
        policy_receipt=signed.as_payload(),
        policy_mode=mode.mode_value(),
        now=moment,
        conn=conn,
    )
    # Counted after the row exists. Incrementing first would spend the session's
    # quota on an offer that a failed write means the buyer never received.
    sessions.record_offer(resolved_session_id, conn=conn)

    ledger.append(
        "policy_kernel",
        "offer.issued",
        {
            "offer_id": offer_id,
            "session_id": resolved_session_id,
            "receipt_id": signed.receipt_id,
            "recommended_option_id": recommended.option_id,
            "option_ids": [o.option_id for o in assembly.options],
            "total_inr": recommended.total_inr,
            "list_total_inr": recommended.evaluation.list_total_inr,
            "discount_inr": recommended.evaluation.discount_inr,
            "gate_tier": assembly.gate_tier,
            "expires_at": stored["expires_at"],
            "proposal_source": outcome.source,
        },
        reason=recommended.gate.summary,
        conn=conn,
    )

    return IssuedOffer(
        offer_id=offer_id,
        session_id=resolved_session_id,
        expires_at=stored["expires_at"],
        expires_in_seconds=offers.seconds_remaining(stored, now=moment),
        assembly=assembly,
        policy_receipt=signed.as_payload(),
        proposal_source=outcome.source,
        proposal_latency_ms=outcome.latency_ms,
        audit_url=audit_url_for(offer_id),
    )


def _ledger_refusal(
    offer_id: str,
    session_id: str,
    refusal: OfferRefused,
    *,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Record a refusal as carefully as an acceptance.

    A store that logs only its sales can claim anything about what it declined.
    """
    ledger.append(
        "policy_kernel",
        "offer.refused",
        {
            "offer_id": offer_id,
            "session_id": session_id,
            **refusal.as_payload(),
        },
        reason=str(refusal),
        conn=conn,
    )


def latency_budget_ms() -> int:
    """The published budget for this flow, for callers that log against it."""
    return settings.LATENCY_BUDGETS_MS["offer"]
