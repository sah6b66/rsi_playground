"""CassetteStore — NDJSON record/replay persistence for LLM calls (DD-02 §4, ADR-7).

A cassette is one ``<root>/<name>.ndjson`` file holding one JSON record per
line. Modes:

* ``record`` — real handler calls are appended to the cassette file;
* ``replay`` — lookups only; a miss raises :class:`CassetteMissError`
  (ADR-7 fail semantics — never silently fall back to a live call);
* ``off`` — no cassette interaction (the pre-existing in-memory behaviour).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from rsi.core.errors import RsiError

__all__ = ["CassetteMissError", "CassetteStore"]

CassetteMode = Literal["record", "replay", "off"]


class CassetteMissError(RsiError):
    """Replay cassette does not contain a matching record (ADR-7)."""


class CassetteStore:
    """File-backed cassette store; one NDJSON file per cassette name."""

    def __init__(self, root: Path | str, mode: CassetteMode = "off") -> None:
        self.root = Path(root)
        self.mode: CassetteMode = mode

    def _path(self, name: str) -> Path:
        return self.root / f"{name}.ndjson"

    def load(self, name: str) -> list[dict[str, Any]]:
        """Read all records of cassette ``name``.

        Raises :class:`CassetteMissError` when the cassette file does not
        exist (also in record mode — loading a not-yet-recorded cassette is
        a miss, not an empty list).
        """
        path = self._path(name)
        if not path.is_file():
            raise CassetteMissError(f"cassette not found: {path}")
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
        return records

    def append(self, name: str, record: dict[str, Any]) -> None:
        """Append one record to ``<root>/<name>.ndjson`` (record mode)."""
        self.root.mkdir(parents=True, exist_ok=True)
        with self._path(name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def keys(self, name: str) -> set[str]:
        """Matching keys (request digest hashes) present in cassette ``name``."""
        return {r["key"] for r in self.load(name) if isinstance(r.get("key"), str)}

    def find(self, name: str, key: str) -> dict[str, Any] | None:
        """Return the first record matching ``key``, or None."""
        for record in self.load(name):
            if record.get("key") == key:
                return record
        return None
