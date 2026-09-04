"""The Gemini transport. Text in, text out, nothing else.

This module knows how to call a model and how to fail. It does not know what a
proposal is, cannot parse one, and holds no opinion about whether a discount is
allowed — that keeps the one place in the system that talks to a third party as
small as it can be.

Two failure classes, kept distinct because the caller responds to them
differently:

* `LLMUnavailable` — the call could not be made or did not come back: no API key,
  the SDK is not installed, a network error, a timeout, a refusal from the
  provider. Retrying a dead transport spends the offer's latency budget to learn
  what we already know, so the caller goes straight to the deterministic
  fallback.
* An empty or malformed body is *not* raised here. It comes back as text and
  fails in the schema parser, which is the path that earns the one retry.

Usage and token counts are deliberately not returned. What an audit needs is the
latency, the model name, and whether the offer came from the model or the
fallback — all of which the proposer records. Token accounting belongs to the
provider's own console, and threading it through here would widen this seam for
a number nothing in the system reasons about.
"""

from __future__ import annotations

import json
import os
from typing import Any

import settings


class LLMUnavailable(RuntimeError):
    """The model could not be reached. Not a bad answer — no answer."""


def is_configured() -> bool:
    """Whether a live call is possible at all: SDK installed and key present.

    Used to decide whether to attempt a call, and by tests to skip the live-model
    cases without pretending they passed.
    """
    if not os.environ.get("GEMINI_API_KEY"):
        return False
    try:
        import google.genai  # noqa: F401
    except ImportError:
        return False
    return True


class GeminiClient:
    """A thin wrapper over `google-genai`'s synchronous client.

    Constructed with no arguments in production; every parameter exists so a test
    can pin the model or shorten the timeout without touching the environment.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        temperature: float | None = None,
        thinking_budget: int | None = None,
    ) -> None:
        self.model = model or settings.GEMINI_MODEL
        self.timeout_seconds = (
            settings.GEMINI_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        self.temperature = (
            settings.GEMINI_TEMPERATURE if temperature is None else temperature
        )
        self.thinking_budget = (
            settings.GEMINI_THINKING_BUDGET
            if thinking_budget is None
            else thinking_budget
        )
        # Held as a plain string rather than a `settings.Secret`, because the SDK
        # needs the value and wrapping it here would only add a `.reveal()` call
        # at the one line that unwraps it. It is never logged or serialised: the
        # attribute is private and nothing in this module prints itself.
        self._api_key = api_key
        self._client: Any = None

    def __repr__(self) -> str:
        # Never include the API key in repr to avoid leaking secrets in logs.
        return f"<GeminiClient model={self.model!r}>"

    # -- construction ------------------------------------------------------

    def _resolve_key(self) -> str:
        if self._api_key:
            return self._api_key
        key = settings.secret("GEMINI_API_KEY", required=False)
        if not key:
            raise LLMUnavailable(
                "GEMINI_API_KEY is not set. The offer path falls back to a "
                "base-item-only offer without it."
            )
        return key.reveal()

    def _ensure_client(self) -> Any:
        """Build the SDK client on first use.

        Lazy for two reasons: importing `google.genai` costs real time at process
        start, and the whole test suite must be able to import this package on a
        machine with no SDK and no key.
        """
        if self._client is not None:
            return self._client
        try:
            from google import genai
        except ImportError as exc:
            raise LLMUnavailable(
                "google-genai is not installed. Install it to enable "
                "model-generated offers."
            ) from exc
        self._client = genai.Client(api_key=self._resolve_key())
        return self._client

    def _config(self, *, system: str, response_schema: dict[str, Any]) -> Any:
        from google.genai import types

        kwargs: dict[str, Any] = {
            "system_instruction": system,
            # Both, not either. The mime type makes the decoder emit JSON; the
            # schema makes it emit *this* JSON. Asking for the mime type alone
            # leaves the field names to the model's discretion.
            "response_mime_type": "application/json",
            "response_schema": response_schema,
            "temperature": self.temperature,
            # Milliseconds, per the SDK's own unit for this field.
            "http_options": types.HttpOptions(
                timeout=int(self.timeout_seconds * 1000)
            ),
        }
        if self.thinking_budget and self.thinking_budget > 0:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=self.thinking_budget
            )
        return types.GenerateContentConfig(**kwargs)

    # -- the call ----------------------------------------------------------

    def generate(
        self, *, system: str, user: str, response_schema: dict[str, Any]
    ) -> str:
        """One model call. Returns the raw response text.

        An empty string is a legitimate return value here and becomes a schema
        error one layer up, which is the failure the retry exists for. Anything
        that prevented the call from completing raises `LLMUnavailable` instead.
        """
        client = self._ensure_client()
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=user,
                config=self._config(system=system, response_schema=response_schema),
            )
        except LLMUnavailable:
            raise
        except Exception as exc:
            # Broad on purpose. The SDK raises its own error hierarchy for API
            # faults and lets httpx's timeout and connection errors through
            # underneath it; enumerating both couples this module to two
            # dependencies' private taxonomies, and every one of them means the
            # same thing to the caller — no answer, use the fallback. The
            # exception type is named in the message so the cause is not lost.
            raise LLMUnavailable(
                f"model call failed ({type(exc).__name__}): {exc}"
            ) from exc

        text = getattr(response, "text", None)
        return text or ""


def is_groq_configured() -> bool:
    """Whether Groq API key is present in environment."""
    return bool(os.environ.get("GROQ_API_KEY"))


class GroqClient:
    """High-speed LLM client powered by Groq."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
        temperature: float = 0.3,
    ) -> None:
        self.model = model or getattr(settings, "GROQ_MODEL", "openai/gpt-oss-120b")
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self._api_key = api_key or os.environ.get("GROQ_API_KEY")
        self._client: Any = None

    def __repr__(self) -> str:
        return f"<GroqClient model={self.model!r}>"

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise LLMUnavailable(
                "GROQ_API_KEY is not set in environment."
            )
        try:
            from groq import Groq
        except ImportError as exc:
            raise LLMUnavailable(
                "groq package is not installed."
            ) from exc

        self._client = Groq(api_key=self._api_key, timeout=self.timeout_seconds)
        return self._client

    def generate(
        self, *, system: str, user: str, response_schema: dict[str, Any]
    ) -> str:
        """Execute one model completion call returning raw JSON text."""
        client = self._ensure_client()
        schema_instruction = (
            f"\n\nCRITICAL: You MUST reply with ONLY a single valid JSON object strictly matching this schema:\n"
            f"{json.dumps(response_schema)}\n"
            f"Do not wrap in markdown quotes. Do not include any explanations."
        )
        full_system = system + schema_instruction

        try:
            chat = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": full_system},
                    {"role": "user", "content": user},
                ],
                model=self.model,
                response_format={"type": "json_object"},
                temperature=self.temperature,
            )
            return chat.choices[0].message.content or ""
        except Exception as exc:
            raise LLMUnavailable(
                f"Groq API call failed ({type(exc).__name__}): {exc}"
            ) from exc

