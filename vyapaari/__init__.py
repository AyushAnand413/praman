"""Vyapaari — the LLM-facing sales layer.

⚠️ BOUNDARY: nothing in this package may import `kernel.payments`, hold
payment credentials, or write to the database. It proposes; the kernel decides;
the store records. The separation is enforced at the filesystem level by
`tests/test_import_boundary.py`, not by convention.

Secrets never enter a prompt built here — call
`settings.assert_no_secrets_in_prompt` before dispatch.

The pipeline, in the order data moves through it:

* `envelope` — joins the public catalog row to the private economics row and
  emits a third thing carrying limits without costs. The only view of the
  catalog a prompt ever sees.
* `prompt` — assembles the text. Stable prefix first for cache hits, buyer's
  words fenced as data.
* `gemini` — the transport. Text in, text out, `LLMUnavailable` on failure.
* `schema` — the one shape a response may take, validated on the way in.
* `proposer` — one call, one retry on a schema failure, then a deterministic
  base-item-only proposal.

What comes out is a *proposal*: a request to sell, with SKUs and percentages
attached. It is not an offer. Nothing here has checked whether the SKUs exist or
whether the discounts are permitted, and nothing here will — that is the policy
kernel's veto, and pre-filtering it away would leave the veto untested.
"""

from vyapaari.envelope import Attach, SellableSku
from vyapaari.gemini import GeminiClient, LLMUnavailable, is_configured
from vyapaari.prompt import Prompt, ProposalRequest
from vyapaari.proposer import (
    SOURCE_FALLBACK,
    SOURCE_LLM,
    SOURCE_LLM_RETRY,
    ProposalOutcome,
    propose,
)
from vyapaari.schema import (
    MAX_UPSELLS,
    UPSELL_TYPES,
    Proposal,
    ProposedItem,
    ProposedUpsell,
    SchemaError,
)

__all__ = [
    "Attach",
    "GeminiClient",
    "LLMUnavailable",
    "MAX_UPSELLS",
    "Proposal",
    "ProposalOutcome",
    "ProposalRequest",
    "ProposedItem",
    "ProposedUpsell",
    "Prompt",
    "SOURCE_FALLBACK",
    "SOURCE_LLM",
    "SOURCE_LLM_RETRY",
    "SchemaError",
    "SellableSku",
    "UPSELL_TYPES",
    "is_configured",
    "propose",
]
