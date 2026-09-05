"""Shared scenario dataclass for all evaluation metrics."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Scenario:
    id: str                                           # e.g. "M1-001"
    name: str                                         # e.g. "Direct lowball ₹1"
    metric: str                                       # e.g. "price_floor"
    input: dict[str, Any]                             # Payload to send
    check: Callable[[Any], tuple[bool, str]]          # Verification function: returns (passed, message)
    is_finding_not_failure: bool = False              # Marks documented architectural findings
    details: str | None = None                        # Optional explanatory note
