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

# Frozen-domain glob patterns (mirrors write_manifest): every matching file
# must be listed in the manifest, and every listed file must hash clean.
_FROZEN_GLOBS = ("rules/safety_rules.yaml", "prompts/*.v*.md")


def _normalized_sha256(path: Path) -> str:
    """sha256 over the file bytes with CRLF line endings normalized to LF.

    Windows checkouts (core.autocrlf=true) materialize the frozen files with
    CRLF while the manifest was computed from the LF-committed bytes — a
    byte-exact hash would then mismatch on every file (audit3 E1/N1). Both
    :func:`verify` and :func:`write_manifest` use this same normalization so
    the two sides can never drift apart.
    """
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def verify(manifest_path: Path | None = None, root: Path | None = None) -> bool:
    """Return True iff every file listed in the manifest matches its sha256."""
    base = root if root is not None else JUDGE_ROOT
    if manifest_path is not None:
        path = manifest_path
    elif root is not None:
        path = root / "rules" / "freeze.manifest"
    else:
        path = MANIFEST_PATH
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
        actual = _normalized_sha256(f)
        if actual != expected:
            logger.warning("freeze_check.hash_mismatch", path=rel, expected=expected, actual=actual)
            return False
    # R2-H2: a file that matches the frozen glob patterns but is NOT listed
    # in the manifest also fails verification — otherwise an attacker could
    # add a brand-new ``.v2`` prompt file (changing the active prompt while
    # every manifested file still hashes clean) and freeze would PASS.
    for pattern in _FROZEN_GLOBS:
        for f in sorted(base.glob(pattern)):
            rel = f.relative_to(base).as_posix()
            if rel not in manifest:
                logger.warning("freeze_check.unlisted_file", path=rel)
                return False
    return True


def write_manifest(
    manifest_path: Path | None = None, root: Path | None = None
) -> dict[str, str]:
    """(Re)generate the manifest from the current rule/prompt files.

    When a custom ``root`` is given and no explicit ``manifest_path``, the
    manifest is written under ``<root>/rules/freeze.manifest`` — never back
    onto the repository's own manifest.
    """
    base = root if root is not None else JUDGE_ROOT
    if manifest_path is not None:
        path = manifest_path
    elif root is not None:
        path = root / "rules" / "freeze.manifest"
    else:
        path = MANIFEST_PATH
    manifest: dict[str, str] = {}
    for pattern in _FROZEN_GLOBS:
        for f in sorted(base.glob(pattern)):
            rel = f.relative_to(base).as_posix()
            manifest[rel] = _normalized_sha256(f)
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
