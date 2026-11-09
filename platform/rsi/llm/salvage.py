"""Salvage parsing for LLM structured output (DD-02 §5.3, audit4 P0).

Providers routinely wrap JSON in markdown fences or lead/trail prose.
Before failing a schema-validated call, try in order:

1. direct ``json.loads`` (done by the caller);
2. extract a `````json ... ````` fenced block;
3. balanced-brace scan for the first complete JSON object.

All strategies are pure functions with zero provider dependencies.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["extract_fenced_json", "scan_first_json_object", "salvage_json"]

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def extract_fenced_json(raw: str) -> Any | None:
    """Strategy 2: parse the first ```json fenced block, if any."""
    for match in _FENCE_RE.finditer(raw):
        candidate = match.group(1).strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def scan_first_json_object(raw: str) -> Any | None:
    """Strategy 3: scan for the first balanced ``{...}`` JSON object."""
    start = raw.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start : i + 1])
                    except json.JSONDecodeError:
                        break  # unbalanced-looking but invalid: try next '{'
        start = raw.find("{", start + 1)
    return None


def salvage_json(raw: str) -> Any | None:
    """Apply the salvage strategies (fence -> balanced scan); None on failure."""
    if not raw:
        return None
    obj = extract_fenced_json(raw)
    if obj is not None:
        return obj
    return scan_first_json_object(raw)
