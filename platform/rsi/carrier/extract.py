"""FinalJsonExtractor: SUT stdout -> TaskResult (DD-05 / Q6).

Extraction strategy:
1. LAST ``<FINAL_JSON>...</FINAL_JSON>`` tag, JSON-decoded.
2. Otherwise the LAST balanced JSON object in the raw stdout.
3. Any failure yields ``TaskResult(task_complete=False, parse_error=True)``
   (class-A sample per §3.6).
"""

from __future__ import annotations

import json
import re
from typing import Any

from rsi.core.models import ActionRecord, TaskResult

_FINAL_JSON_RE = re.compile(r"<FINAL_JSON>(.*?)</FINAL_JSON>", re.DOTALL)


def _from_dict(data: dict[str, Any]) -> TaskResult:
    """Map a parsed JSON object onto TaskResult; raise on bad shape."""
    actions = [ActionRecord(**a) for a in data.get("actions") or []]
    return TaskResult(
        task_complete=bool(data.get("task_complete", False)),
        root_cause=data.get("root_cause"),
        actions=actions,
        confidence=data.get("confidence"),
        parse_error=False,
    )


def _last_tag_payload(stdout: str) -> dict[str, Any] | None:
    matches = _FINAL_JSON_RE.findall(stdout)
    if not matches:
        return None
    data = json.loads(matches[-1])
    if not isinstance(data, dict):
        raise ValueError("FINAL_JSON payload is not an object")
    return data


def _last_balanced_object(stdout: str) -> dict[str, Any] | None:
    """Scan backwards for a balanced ``{...}`` that JSON-parses to a dict."""
    end = stdout.rfind("}")
    if end == -1:
        return None
    start = stdout.rfind("{", 0, end + 1)
    while start != -1:
        candidate = stdout[start : end + 1]
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            start = stdout.rfind("{", 0, start)
            continue
        if isinstance(data, dict):
            return data
        start = stdout.rfind("{", 0, start)
    return None


def extract_task_result(stdout: str) -> TaskResult:
    """Extract the SUT's final answer from raw stdout."""
    try:
        data = _last_tag_payload(stdout)
        if data is None:
            data = _last_balanced_object(stdout)
        if data is None:
            raise ValueError("no JSON object found in stdout")
        return _from_dict(data)
    except Exception:
        return TaskResult(task_complete=False, parse_error=True)
