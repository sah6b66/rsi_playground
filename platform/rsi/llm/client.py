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
import inspect
import json
import time
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import BaseModel, ValidationError

from rsi.core.errors import LLMOutputInvalid
from rsi.core.models import LLMResponse, LLMUsage
from rsi.llm.cassette import CassetteMissError
from rsi.llm.salvage import salvage_json
from rsi.log import get_logger

if TYPE_CHECKING:
    from rsi.llm.accounting import Accountant
    from rsi.llm.cassette import CassetteStore
    from rsi.llm.prompts import PromptLibrary

__all__ = ["Component", "LLMClient", "Handler"]

logger = get_logger(__name__)

Component = Literal["attribution", "hypothesis", "critic", "judge", "sft"]

Handler = Callable[[str, str, dict[str, str]], str]

_MAX_SCHEMA_RETRIES = 2  # extra attempts after the initial one (DD-02 §5.1 ④)


def _accepts_temperature(handler: Handler) -> bool:
    """True when the injected handler wants the effective temperature kwarg."""
    try:
        params = inspect.signature(handler).parameters
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False
    return "temperature" in params or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


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
        prompt_library: PromptLibrary | None = None,
        accountant: Accountant | None = None,
        temperature: float = 0.0,
        cassette: "CassetteStore | None" = None,
        cassette_name: str = "default",
    ) -> None:
        self._handler: Handler = handler or _default_handler
        self.model = model
        self.prompt_version = prompt_version
        self.temperature = temperature
        self.prompt_library = prompt_library
        self.accountant = accountant
        self.cassette: dict[str, LLMResponse] = {}
        self._cassette_store = cassette
        self._cassette_name = cassette_name
        self._semaphore = asyncio.Semaphore(max_concurrent_calls)
        self._judge_temp_override_logged = False

    # -- public API ---------------------------------------------------------------

    def _effective_temperature(self, component: Component) -> float:
        """Temperature actually used for a component's calls.

        ADR-23 fairness guard (audit4 P0): judge calls are ALWAYS forced to
        temperature 0 regardless of the configured value (logged once).
        """
        if component == "judge" and self.temperature != 0.0:
            if not self._judge_temp_override_logged:
                logger.debug(
                    "llm.judge_temperature_forced_to_zero",
                    configured=self.temperature,
                )
                self._judge_temp_override_logged = True
            return 0.0
        return self.temperature

    def prompt_hash(self, component: Component, prompt_name: str) -> str:
        """Fingerprint of the active prompt+model+temperature (C-1, DD-08 §5.1).

        ``sha256("<component>/<name>@<version>|<model>|<temperature>|<body_sha16>")``
        where the version/body come from the library when a PromptLibrary is
        attached (F2: body-bound so same-version edits change the hash), else
        the client's static ``prompt_version`` with an empty body segment.
        The temperature is the EFFECTIVE temperature (judge is always 0).
        """
        temperature = self._effective_temperature(component)
        if self.prompt_library is not None:
            return self.prompt_library.hash_of(
                f"{component}/{prompt_name}", self.model, temperature
            )
        payload = (
            f"{component}/{prompt_name}@{self.prompt_version}"
            f"|{self.model}|{temperature}|"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

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
        if self._cassette_store is not None and self._cassette_store.mode == "replay":
            return self._replay(
                key, component, prompt_name, schema, run_id, batch_id
            )
        cached = self.cassette.get(key)
        if cached is not None:
            return cached

        # prompt construction via the versioned template library (DD-02 §4)
        prompt_text: str | None = None
        prompt_version = self.prompt_version
        if self.prompt_library is not None:
            prompt_text = self.prompt_library.render(component, prompt_name, vars)
            v = self.prompt_library.latest_version(component, prompt_name)
            if v > 0:
                prompt_version = f"v{v}"

        raw: str | None = None
        parsed: Any | None = None
        validation_error: str | None = None
        temperature = self._effective_temperature(component)
        pass_temperature = _accepts_temperature(self._handler)
        started = time.perf_counter()
        async with self._semaphore:
            for _attempt in range(_MAX_SCHEMA_RETRIES + 1):
                call_vars = dict(vars)
                if prompt_text is not None:
                    call_vars.setdefault("prompt", prompt_text)
                if validation_error is not None:
                    call_vars["validation_error"] = validation_error
                kwargs: dict[str, Any] = (
                    {"temperature": temperature} if pass_temperature else {}
                )
                raw = await asyncio.to_thread(
                    self._handler, component, prompt_name, call_vars, **kwargs
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
        latency_ms = int((time.perf_counter() - started) * 1000)

        resp = LLMResponse(
            content=raw or "",
            parsed=parsed,
            usage=LLMUsage(),
            model=self.model,
            prompt_version=prompt_version,
        )
        self.cassette[key] = resp

        # C-4: record mode persists the call to the cassette file (DD-02 §4).
        if self._cassette_store is not None and self._cassette_store.mode == "record":
            self._cassette_store.append(
                self._cassette_name,
                {
                    "key": key,
                    "component": component,
                    "prompt_name": prompt_name,
                    "model": self.model,
                    "prompt_version": prompt_version,
                    "content": raw or "",
                    "input_tokens": max(len(prompt_text or "") // 4, 1),
                    "output_tokens": max(len(raw or "") // 4, 1),
                    "latency_ms": latency_ms,
                },
            )

        # usage accounting for budget control (DD-02 §4)
        self._account(
            component,
            prompt_version,
            self.model,
            len(prompt_text or "") // 4,
            len(raw or "") // 4,
            latency_ms,
            run_id,
            batch_id,
        )
        return resp

    # -- internals ------------------------------------------------------------------

    def _account(
        self,
        component: str,
        prompt_version: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        run_id: str | None,
        batch_id: str | None,
    ) -> None:
        """Account one call (real or replayed) through the Accountant."""
        if self.accountant is None:
            return
        self.accountant.record(
            component=component,
            model=model,
            prompt_version=prompt_version,
            input_tokens=max(input_tokens, 1),
            output_tokens=max(output_tokens, 1),
            latency_ms=latency_ms,
            run_id=run_id,
            batch_id=batch_id,
        )

    def _replay(
        self,
        key: str,
        component: Component,
        prompt_name: str,
        schema: type[BaseModel] | None,
        run_id: str | None,
        batch_id: str | None,
    ) -> LLMResponse:
        """Cassette replay path (ADR-7): hit -> respond + account; miss -> raise."""
        record = self._cassette_store.find(self._cassette_name, key)
        if record is None:
            raise CassetteMissError(
                f"cassette {self._cassette_name!r} has no record for "
                f"component {component!r} prompt {prompt_name!r}",
                context={"component": component, "prompt_name": prompt_name},
            )
        parsed: Any | None = None
        if schema is not None:
            parsed, _err = self._validate(str(record.get("content", "")), schema)
        resp = LLMResponse(
            content=str(record.get("content", "")),
            parsed=parsed,
            usage=LLMUsage(
                input_tokens=int(record.get("input_tokens", 0) or 0),
                output_tokens=int(record.get("output_tokens", 0) or 0),
            ),
            model=str(record.get("model", self.model)),
            prompt_version=str(record.get("prompt_version", self.prompt_version)),
        )
        # DD-02 §5.1 note: replayed hits are still accounted so the budget
        # view stays consistent (fixes the "hit without accounting" gap).
        self._account(
            component,
            resp.prompt_version,
            resp.model,
            resp.usage.input_tokens,
            resp.usage.output_tokens,
            int(record.get("latency_ms", 0) or 0),
            run_id,
            batch_id,
        )
        return resp

    # -- internals ------------------------------------------------------------------

    def _key(self, component: str, prompt_name: str, vars: dict[str, str]) -> str:
        """Cassette/cache key: component + prompt_name + prompt fingerprint
        + canonical vars (R2-H3).

        The active prompt fingerprint (``prompt_hash``) is part of the key so
        a changed prompt version (e.g. a new ``.v2`` template) can never
        replay a response recorded under the old prompt, even when the
        request vars are identical.
        """
        canonical = json.dumps(
            {k: vars[k] for k in sorted(vars)}, ensure_ascii=False, sort_keys=True
        )
        payload = (
            f"{component}\x00{prompt_name}\x00"
            f"{self.prompt_hash(component, prompt_name)}\x00{canonical}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate(
        raw: str, schema: type[BaseModel]
    ) -> tuple[Any | None, str | None]:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            # DD-02 §5.3 salvage (audit4 P0): LLMs often wrap JSON in
            # markdown fences or prose. Try the salvage strategies before
            # declaring the output invalid (failure still feeds the retry /
            # LLMOutputInvalid path unchanged).
            obj = salvage_json(raw)
            if obj is None:
                return None, f"schema validation failed: output is not JSON: {exc}"
        try:
            return schema.model_validate(obj), None
        except ValidationError as exc:
            return None, f"schema validation error: {exc}"
