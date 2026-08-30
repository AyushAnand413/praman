"""The A/B harness — measured revenue lift, not claimed revenue lift.

Two arms, same store, same personas, same seeds:

    control    the buyer takes the base item only, as an agent always will
               when nothing is offered or nothing is accepted
    treatment  the buyer sees the full offer and chooses as its persona
               chooses

Both arms walk the identical rail through real HTTP — discovery, catalog,
offer, checkout — against the running app. Nothing here reads merchant state
directly; the buyer agent knows what any outside agent knows. Sessions run
with POLICY_MODE=shadow, so the whole experiment moves no money and leaves no
test-mode noise behind: every verdict, bound, and receipt still happens.

Choice policy is where the arms differ, and only there:

    control    the option with the fewest items — the base item alone
    treatment  the persona's own rule (recommended / budget / cheapest)

That asymmetry IS the experiment: does a structured, bounded, verifiable offer
move an autonomous buyer off the base item? The report says yes or no with
numbers, including the uncomfortable ones — conversion can fall, and the
report shows it when it does.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from harness.grahak import Grahak, Persona, StoreRefused, WalletRefused
from harness.grahak import Offer, Transport

#: A session "completed" when the store accepted the purchase under one of
#: these statuses. shadow_complete is the normal outcome in a shadow-mode run;
#: confirmed belongs to a live-mode run.
COMPLETED_STATUSES = frozenset({"confirmed", "shadow_complete", "replayed"})


@dataclass(frozen=True)
class SessionResult:
    """What happened in one simulated shopping trip."""

    arm: str
    persona: str
    index: int
    completed: bool
    order_id: str | None
    basket_inr: int
    upsells_shown: int
    upsells_taken: int
    discount_inr: int
    error: str

    @property
    def took_upsell(self) -> bool:
        return self.upsells_taken > 0


TransportFactory = Callable[[], Transport]


def _base_only_option(offer: Offer) -> dict[str, Any]:
    """The smallest option: what a buyer takes when it ignores merchandising."""
    options = list(offer.options)
    if not options:
        raise StoreRefused(status=0, code="no_options", message="offer had no options")
    return min(
        options,
        key=lambda o: (len(o.get("items", [])), int(o["total_inr"])),
    )


def _savings_of(option: dict[str, Any]) -> int:
    """Rupees off list across the option's lines — the discount actually given."""
    total = 0
    for item in option.get("items", []):
        qty = int(item.get("qty", 1))
        total += (int(item["list_price_inr"]) - int(item["offered_price_inr"])) * qty
    return max(0, total)


def run_session(
    persona: Persona,
    arm: str,
    *,
    transport_factory: TransportFactory,
    index: int,
) -> SessionResult:
    """One buyer, one need, one purchase attempt, over the real rail."""
    agent = Grahak(
        transport_factory(),
        wallet=persona.wallet(),
        agent_id=f"grahak_ab_{arm}_{index}_{secrets.token_hex(3)}",
    )
    empty = SessionResult(
        arm=arm,
        persona=persona.name,
        index=index,
        completed=False,
        order_id=None,
        basket_inr=0,
        upsells_shown=0,
        upsells_taken=0,
        discount_inr=0,
        error="",
    )
    try:
        if agent.discovery is None:
            agent.discover()
        agent.browse(persona.need, budget_inr=persona.budget_inr, category=persona.category)
        offer = agent.request_offer(
            persona.need,
            qty=persona.qty,
            budget_inr=persona.budget_inr,
            category=persona.category,
            delivery=persona.delivery,
        )

        # The only place the arms differ.
        if arm == "control":
            option = _base_only_option(offer)
        else:
            option = persona.choose(offer)

        items = list(option.get("items", []))
        purchase = agent.buy(offer, str(option["option_id"]))
        completed = purchase.status in COMPLETED_STATUSES
        return SessionResult(
            arm=arm,
            persona=persona.name,
            index=index,
            completed=completed,
            order_id=purchase.order_id,
            basket_inr=purchase.amount_inr if completed else 0,
            upsells_shown=max(0, len(items) - 1),
            upsells_taken=max(0, len(items) - 1) if completed else 0,
            discount_inr=_savings_of(option) if completed else 0,
            error="" if completed else f"incomplete:{purchase.status}",
        )
    except (StoreRefused, WalletRefused) as exc:
        # A refusal is a data point, not a crash: bounds firing, a wallet
        # declining, stock gone. The report counts them per arm.
        return SessionResult(**{**empty.__dict__, "error": str(exc)[:200]})
    except Exception as exc:  # noqa: BLE001 - a runner of 400 sessions cannot
        # let one broken trip abort the experiment; it records and moves on.
        return SessionResult(**{**empty.__dict__, "error": f"{type(exc).__name__}: {exc}"[:200]})


def run_ab(
    *,
    sessions_per_arm: int = 200,
    transport_factory: TransportFactory,
    personas: Sequence[Persona] | None = None,
    arms: tuple[str, ...] = ("control", "treatment"),
) -> list[SessionResult]:
    """Run the experiment. Personas rotate so both arms see the same mix.

    Sessions run sequentially to keep results deterministic and avoid
    overloading the server.
    """
    from harness.grahak import PERSONAS

    people = tuple(personas or PERSONAS)
    results: list[SessionResult] = []
    # Sequential execution keeps ordering predictable and avoids concurrency issues.
    for arm in arms:
        for i in range(sessions_per_arm):
            results.append(
                run_session(
                    people[i % len(people)],
                    arm,
                    transport_factory=transport_factory,
                    index=i,
                )
            )
    return results


def summarize(results: Iterable[SessionResult]) -> dict[str, dict[str, Any]]:
    """Per-arm numbers, computed from stored outcomes rather than claims.

    `margin_per_rupee_discounted` needs private cost data, which this module
    deliberately never touches; the script layer joins costs in when it has
    database access. What lands here is what the buyer side observed.
    """
    out: dict[str, dict[str, Any]] = {}
    for arm in sorted({r.arm for r in results}):
        rows_r = [r for r in results if r.arm == arm]
        completed = [r for r in rows_r if r.completed]
        revenue = sum(r.basket_inr for r in completed)
        out[arm] = {
            "sessions": len(rows_r),
            "orders": len(completed),
            "conversion": round(len(completed) / len(rows_r), 4) if rows_r else 0.0,
            "revenue_inr": revenue,
            "aov_inr": (revenue // len(completed)) if completed else 0,
            "attach_rate": (
                round(sum(1 for r in completed if r.took_upsell) / len(completed), 4)
                if completed
                else 0.0
            ),
            "upsells_shown": sum(r.upsells_shown for r in rows_r),
            "upsells_taken": sum(r.upsells_taken for r in completed),
            "discount_given_inr": sum(r.discount_inr for r in completed),
            "refused_or_failed": sum(1 for r in rows_r if not r.completed),
        }
    return out
