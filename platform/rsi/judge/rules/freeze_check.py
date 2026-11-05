"""Frozen-domain verification for the judge rules/prompts (C-6, FR7.6).

``judge/rules/freeze.manifest`` maps relative paths (relative to ``rsi/judge``)
to sha256 digests. :func:`verify` recomputes the digests and reports any
mismatch — a tampered or missing file fails verification. The manifest format
matches :meth:`rsi.versioning.gitstore.GitStore.verify_freeze` so the same
asset can be reused when a staging tree freezes the judge domain.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rsi.log import get_logger

__all__ = ["MANIFEST_PATH", "JUDGE_ROOT", "verify"]

logger = get_logger(__name__)

JUDGE_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = JUDGE_ROOT / "rules" / "freeze.manifest"


def verify(manifest_path: Path | None = None, root: Path | None = None) -> bool:
    """Return True iff every file listed in the manifest matches its sha256."""
    path = manifest_path if manifest_path is not None else MANIFEST_PATH
    base = root if root is not None else JUDGE_ROOT
    if not path.is_file():
        return False
    try:
        manifest: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    for rel, expected in manifest.items():
        f = base / rel
        if not f.is_file():
            logger.warning("freeze_check.missing_file", path=rel)
            return False
        actual = hashlib.sha256(f.read_bytes()).hexdigest()
        if actual != expected:
            logger.warning("freeze_check.hash_mismatch", path=rel, expected=expected, actual=actual)
            return False
    return True


def write_manifest(
    manifest_path: Path | None = None, root: Path | None = None
) -> dict[str, str]:
    """(Re)generate the manifest from the current rule/prompt files."""
    path = manifest_path if manifest_path is not None else MANIFEST_PATH
    base = root if root is not None else JUDGE_ROOT
    manifest: dict[str, str] = {}
    for pattern in ("rules/safety_rules.yaml", "prompts/*.v*.md"):
        for f in sorted(base.glob(pattern)):
            rel = f.relative_to(base).as_posix()
            manifest[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":  # python -m rsi.judge.rules.freeze_check
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "write":
        print(json.dumps(write_manifest(), indent=2, sort_keys=True))
        sys.exit(0)
    ok = verify()
    print("freeze: OK" if ok else "freeze: MISMATCH")
    sys.exit(0 if ok else 1)
