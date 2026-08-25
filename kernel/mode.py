"""POLICY_MODE — the kernel-level switch between computing and executing.

The flag lives here, inside the kernel, rather than in the API layer. That
placement is the entire point: shadow mode must be impossible to bypass by
calling a different endpoint. Every path that could move money asks this
module, so there is one gate and no way around it.

In shadow mode everything still runs — all nine bounds are evaluated, gate
tiers are assigned, policy receipts are signed, and every verdict and reason
reaches the ledger — but the Razorpay client is never constructed and no stock
is committed. A shadow ledger entry records the amount that *would* have moved
and carries `would_have_charged: true`, so it can never be mistaken for a real
transaction when auditing.

Callers read through `current_mode()` instead of importing the constant.
`settings.POLICY_MODE` is bound at import time; a module that copied it into
its own namespace would not see a change, which would make the flag untestable
and — worse — make it possible for two modules to disagree about whether money
is allowed to move.
"""

from __future__ import annotations

import settings
from settings import PolicyMode


def current_mode() -> PolicyMode:
    """The live value of the flag, read fresh on every call."""
    return settings.POLICY_MODE


def mode_value() -> str:
    """The string form written to the ledger and every persisted row."""
    return current_mode().value


def is_live() -> bool:
    """True when the kernel is allowed to execute its verdicts."""
    return current_mode() is PolicyMode.LIVE


def is_shadow() -> bool:
    """True when the kernel computes verdicts and calls nothing."""
    return current_mode() is PolicyMode.SHADOW


class ShadowModeViolation(RuntimeError):
    """An external side effect was attempted while in shadow mode.

    Raised rather than ignored. A silently-skipped payment would leave the
    system reporting success for money that never moved, which is worse than
    a loud failure.
    """


def assert_may_move_money(action: str) -> None:
    """Gate every external money call. Raises in shadow mode.

    Called immediately before the Razorpay client is constructed or used, so
    the guard sits on the same line as the side effect it protects.
    """
    if not is_live():
        raise ShadowModeViolation(
            f"POLICY_MODE={mode_value()} forbids {action}: in shadow mode the "
            "kernel computes the full verdict and calls nothing."
        )
