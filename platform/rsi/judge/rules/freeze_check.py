"""Frozen-domain verification for the judge rules/prompts (C-6, FR7.6).

``judge/rules/freeze.manifest`` maps relative paths to sha256 digests. Keys
prefixed ``prompts/`` resolve against ``rsi/llm/prompts/judge`` (the unified
platform template tree, DD-02 §2); all other keys resolve against ``rsi/judge``
(e.g. ``rules/safety_rules.yaml``). :func:`verify` recomputes the digests and
reports any mismatch — a tampered or missing file fails verification. The
manifest format matches :meth:`rsi.versioning.gitstore.GitStore.verify_freeze`
so the same asset can be reused when a staging tree freezes the judge domain.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rsi.log import get_logger

__all__ = ["MANIFEST_PATH", "JUDGE_ROOT", "PROMPTS_ROOT", "verify"]

logger = get_logger(__name__)

JUDGE_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = JUDGE_ROOT / "rules" / "freeze.manifest"
PROMPTS_ROOT = Path(__file__).resolve().parents[2] / "llm" / "prompts" / "judge"

# Manifest keys prefixed with this resolve against PROMPTS_ROOT; everything
# else resolves against JUDGE_ROOT (e.g. rules/safety_rules.yaml).
_PROMPTS_PREFIX = "prompts/"


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


def verify(
    manifest_path: Path | None = None,
    rules_root: Path | None = None,
    prompts_root: Path | None = None,
) -> bool:
    """Return True iff every file listed in the manifest matches its sha256.

    ``rules_root`` redirects the judge tree (default ``rsi/judge``);
    ``prompts_root`` redirects the frozen judge templates (default
    ``rsi/llm/prompts/judge``). Manifest keys prefixed ``prompts/`` resolve
    against the prompts root, everything else against the rules root.
    """
    rules_base = rules_root if rules_root is not None else JUDGE_ROOT
    prompts_base = prompts_root if prompts_root is not None else PROMPTS_ROOT
    if manifest_path is not None:
        path = manifest_path
    elif rules_root is not None:
        path = rules_root / "rules" / "freeze.manifest"
    else:
        path = MANIFEST_PATH
    if not path.is_file():
        return False
    try:
        manifest: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    for rel, expected in manifest.items():
        base, rel_path = _resolve(rel, rules_base, prompts_base)
        f = base / rel_path
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
    for base, pattern, prefix in _resolved_globs(rules_base, prompts_base):
        for f in sorted(base.glob(pattern)):
            rel = prefix + f.relative_to(base).as_posix()
            if rel not in manifest:
                logger.warning("freeze_check.unlisted_file", path=rel)
                return False
    return True


def write_manifest(
    manifest_path: Path | None = None,
    rules_root: Path | None = None,
    prompts_root: Path | None = None,
) -> dict[str, str]:
    """(Re)generate the manifest from the current rule/prompt files.

    When a custom ``rules_root`` is given and no explicit ``manifest_path``,
    the manifest is written under ``<rules_root>/rules/freeze.manifest`` —
    never back onto the repository's own manifest.
    """
    rules_base = rules_root if rules_root is not None else JUDGE_ROOT
    prompts_base = prompts_root if prompts_root is not None else PROMPTS_ROOT
    if manifest_path is not None:
        path = manifest_path
    elif rules_root is not None:
        path = rules_root / "rules" / "freeze.manifest"
    else:
        path = MANIFEST_PATH
    manifest: dict[str, str] = {}
    for base, pattern, prefix in _resolved_globs(rules_base, prompts_base):
        for f in sorted(base.glob(pattern)):
            rel = prefix + f.relative_to(base).as_posix()
            manifest[rel] = _normalized_sha256(f)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _resolve(
    rel: str, rules_base: Path, prompts_base: Path
) -> tuple[Path, str]:
    """Map a manifest key to (root, path relative to that root)."""
    if rel.startswith(_PROMPTS_PREFIX):
        return prompts_base, rel[len(_PROMPTS_PREFIX):]
    return rules_base, rel


def _resolved_globs(
    rules_base: Path, prompts_base: Path
) -> tuple[tuple[Path, str, str], tuple[Path, str, str]]:
    return (
        (rules_base, "rules/safety_rules.yaml", ""),
        (prompts_base, "*.v*.md", _PROMPTS_PREFIX),
    )


if __name__ == "__main__":  # python -m rsi.judge.rules.freeze_check
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "write":
        print(json.dumps(write_manifest(), indent=2, sort_keys=True))
        sys.exit(0)
    ok = verify()
    print("freeze: OK" if ok else "freeze: MISMATCH")
    sys.exit(0 if ok else 1)
