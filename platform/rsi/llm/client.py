"""LLMClient — offline mock (DD-02 §4/§5.1 subset, Plan 3 Task 3).

Simplified single-process version of the full DD-02 client:

* injectable sync ``handler(component, prompt_name, vars) -> str`` stands in
  for the provider call (real anthropic/openai_compat wiring lands in a later
  slice);
* cassette is an in-memory dict keyed by
  ``sha256(component + prompt_name + canonical vars)`` — a hit returns the
  cached :class:`LLMResponse` without invoking the handler;
* concurrency is bounded by ``asyncio.Semaphore(max_concurrent_calls)``
  (default 4, v2.14);
* when a ``schema`` is supplied the raw text is ``json.loads`` + pydantic
  validated; on failure the validation error is fed back to the handler via
  ``vars["validation_error"]`` and retried at most 2 extra times. Exhausting
  retries raises :class:`LLMOutputInvalid` (ADR-1/7).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError

from rsi.core.errors import LLMOutputInvalid
from rsi.core.models import LLMResponse, LLMUsage

__all__ = ["Component", "LLMClient", "Handler"]

Component = Literal["attribution", "hypothesis", "critic", "judge", "sft"]

Handler = Callable[[str, str, dict[str, str]], str]

_MAX_SCHEMA_RETRIES = 2  # extra attempts after the initial one (DD-02 §5.1 ④)


def _default_handler(component: str, prompt_name: str, vars: dict[str, str]) -> str:
    raise NotImplementedError(
        "LLMClient requires an injected handler for offline mocking"
    )


class LLMClient:
    """Offline mock client with cassette replay and schema-validated output."""

    def __init__(
        self,
        handler: Handler | None = None,
        *,
        max_concurrent_calls: int = 4,
        model: str = "mock",
        prompt_version: str = "v1",
    ) -> None:
        self._handler: Handler = handler or _default_handler
        self.model = model
        self.prompt_version = prompt_version
        self.cassette: dict[str, LLMResponse] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent_calls)

    # -- public API ---------------------------------------------------------------

    async def complete(
        self,
        component: Component,
        prompt_name: str,
        vars: dict[str, str],
        schema: type[BaseModel] | None = None,
        run_id: str | None = None,
        batch_id: str | None = None,
    ) -> LLMResponse:
        """Render-free completion flow (cassette -> handler -> validate)."""
        key = self._key(component, prompt_name, vars)
        cached = self.cassette.get(key)
        if cached is not None:
            return cached

        raw: str | None = None
        parsed: Any | None = None
        validation_error: str | None = None
        async with self._semaphore:
            for _attempt in range(_MAX_SCHEMA_RETRIES + 1):
                call_vars = dict(vars)
                if validation_error is not None:
                    call_vars["validation_error"] = validation_error
                raw = await asyncio.to_thread(
                    self._handler, component, prompt_name, call_vars
                )
                if schema is None:
                    break
                parsed, validation_error = self._validate(raw, schema)
                if validation_error is None:
                    break
            if schema is not None and validation_error is not None:
                raise LLMOutputInvalid(
                    f"component {component!r} prompt {prompt_name!r}: schema "
                    f"validation failed after {_MAX_SCHEMA_RETRIES + 1} attempts",
                    context={
                        "component": component,
                        "prompt_name": prompt_name,
                        "last_error": validation_error,
                        "last_output": raw,
                    },
                )

        resp = LLMResponse(
            content=raw or "",
            parsed=parsed,
            usage=LLMUsage(),
            model=self.model,
            prompt_version=self.prompt_version,
        )
        self.cassette[key] = resp
        return resp

    # -- internals ------------------------------------------------------------------

    @staticmethod
    def _key(component: str, prompt_name: str, vars: dict[str, str]) -> str:
        canonical = json.dumps(
            {k: vars[k] for k in sorted(vars)}, ensure_ascii=False, sort_keys=True
        )
        payload = f"{component}\x00{prompt_name}\x00{canonical}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate(
        raw: str, schema: type[BaseModel]
    ) -> tuple[Any | None, str | None]:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"schema validation failed: output is not JSON: {exc}"
        try:
            return schema.model_validate(obj), None
        except ValidationError as exc:
            return None, f"schema validation error: {exc}"
