"""EvalsetFetcher — fetch evaluation sets from the online Evalset API (ADR-27).

Client for the externally-managed Evalset API configured at
``settings.evalset_api.url``. Unlike the deprecated ConfigApiCaller, an
empty URL does NOT raise at construction time: the fetcher is always
assembled in ``build_services`` so probes/monitoring can reference it, and
``fetch`` fails loudly (the API endpoint surfaces 409 to the caller).
"""

from __future__ import annotations

import httpx
from pydantic import ValidationError as PydanticValidationError

from rsi.core.errors import EnvError, NotFoundError, ValidationError
from rsi.core.models import ApiEvalsetTask
from rsi.log import get_logger

__all__ = ["EvalsetFetcher"]

BODY_SUMMARY_LEN = 300

logger = get_logger(__name__)


def _tasks_from_payload(
    data: object, name: str, url: str
) -> list[ApiEvalsetTask]:
    """Shared response parsing for fetch()/fetch_sync() (D4)."""
    if not isinstance(data, dict):
        raise EnvError(
            f"evalset fetch failed for {name}: response is not an object",
            context={"name": name, "url": url},
        )
    raw_tasks = data.get("tasks") or []
    if not isinstance(raw_tasks, list):
        raise EnvError(
            f"evalset fetch failed for {name}: 'tasks' is not a list",
            context={"name": name, "url": url},
        )
    tasks: list[ApiEvalsetTask] = []
    for i, task in enumerate(raw_tasks):
        task_id = task.get("task_id", f"#{i}") if isinstance(task, dict) else f"#{i}"
        try:
            tasks.append(ApiEvalsetTask.model_validate(task))
        except PydanticValidationError as exc:
            logger.warning(
                "evalset_fetcher.invalid_task",
                evalset=name,
                task_id=task_id,
                index=i,
            )
            raise ValidationError(
                f"evalset {name!r}: invalid task {task_id!r} "
                f"(index {i}): {exc}",
                context={"name": name, "task_id": task_id, "index": i},
            ) from exc
    return tasks


class EvalsetFetcher:
    """Online Evalset API v1 client — one lazily-created ``httpx.AsyncClient``.

    Same lifecycle pattern as NetworkPrepClient (connection pooling, shared
    by all requests; ``aclose`` resets so a later call recreates one).
    """

    def __init__(self, base_url: str, timeout_s: int = 30) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None

    async def fetch(self, name: str) -> list[ApiEvalsetTask]:
        """GET /evalsets/{name} → parse ``{name, tasks: [...]}`` into models.

        - 404 → NotFoundError; other non-200 / transport errors → EnvError
        - malformed tasks → ValidationError with the offending task_id
        """
        url = f"{self.base_url}/evalsets/{name}"
        try:
            client = self._get_client()
            resp = await client.get(url, timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            raise EnvError(
                f"evalset fetch failed for {name}: {exc}",
                context={"name": name, "url": url, "exc": type(exc).__name__},
            ) from exc
        if resp.status_code == 404:
            raise NotFoundError(
                f"evalset {name!r} not found on online API",
                context={"name": name, "url": url, "status_code": 404},
            )
        if resp.status_code != 200:
            summary = resp.text[:BODY_SUMMARY_LEN]
            raise EnvError(
                f"evalset fetch failed for {name}: HTTP {resp.status_code}: {summary}",
                context={"name": name, "url": url, "status_code": resp.status_code},
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise EnvError(
                f"evalset fetch failed for {name}: invalid JSON: {exc}",
                context={"name": name, "url": url},
            ) from exc
        if not isinstance(data, dict):
            raise EnvError(
                f"evalset fetch failed for {name}: response is not an object",
                context={"name": name, "url": url},
            )
        return _tasks_from_payload(data, name, url)

    def fetch_sync(self, name: str) -> list[ApiEvalsetTask]:
        """Sync variant of fetch() for the (sync) batch resolver (D4).

        Uses a short-lived ``httpx.Client`` per call — the sync resolver path
        runs at most once per batch, so pooling buys nothing. Same error
        contract as the async fetch (404 → NotFoundError etc.).
        """
        url = f"{self.base_url}/evalsets/{name}"
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.get(url)
        except httpx.HTTPError as exc:
            raise EnvError(
                f"evalset fetch failed for {name}: {exc}",
                context={"name": name, "url": url, "exc": type(exc).__name__},
            ) from exc
        if resp.status_code == 404:
            raise NotFoundError(
                f"evalset {name!r} not found on online API",
                context={"name": name, "url": url, "status_code": 404},
            )
        if resp.status_code != 200:
            summary = resp.text[:BODY_SUMMARY_LEN]
            raise EnvError(
                f"evalset fetch failed for {name}: HTTP {resp.status_code}: {summary}",
                context={"name": name, "url": url, "status_code": resp.status_code},
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise EnvError(
                f"evalset fetch failed for {name}: invalid JSON: {exc}",
                context={"name": name, "url": url},
            ) from exc
        return _tasks_from_payload(data, name, url)

    async def aclose(self) -> None:
        """Close the owned client; reset so a later call recreates one."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazily create one client and reuse it (connection pooling)."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client
