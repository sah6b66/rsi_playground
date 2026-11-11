"""PromptLibrary — versioned string-template prompts (DD-02 §4).

Templates are keyed ``"<component>/<name>.v<N>"`` and rendered with
``str.format(**vars)``. Rendering picks the latest version present; when no
template matches, a generic fallback prompt is produced so offline callers
never hard-fail on a missing prompt.

Templates can also come from a template directory (C-6 frozen domain): a file
``<dir>/<name>.v<N>.md`` registers under the component given at load time
(defaults to ``judge``); :meth:`load_root` scans the unified platform tree
``<root>/<component>/<name>.v<N>.md`` (DD-02 §2, ``rsi/llm/prompts/``), so
frozen ``.md`` files take priority over inline entries while the fallback
concatenation stays as last resort.

``hash_of`` implements the DD-08 §5.1/§5.4 prompt-fingerprint:
``sha256("<name>@<version>|<model>|<temperature>")``.
"""

from __future__ import annotations

import hashlib
import string
from pathlib import Path
from typing import Any

from rsi.core.errors import ConfigError
from rsi.log import get_logger

__all__ = ["PromptLibrary"]

logger = get_logger(__name__)


def _placeholders(body: str) -> set[str]:
    """Named placeholders (``{var}``) used by a template body (F3)."""
    return {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(body)
        if field_name
    }


class PromptLibrary:
    """Simple string-template prompt library with version tracking."""

    def __init__(
        self,
        templates: dict[str, str] | None = None,
        *,
        template_dir: Path | str | None = None,
        component: str = "judge",
    ):
        # templates: {"<component>/<name>.v<N>": "template with {vars}"}
        self._templates = dict(templates or {})
        if template_dir is not None:
            self.load_dir(template_dir, component=component)

    def load_dir(self, template_dir: Path | str, *, component: str = "judge") -> int:
        """Register ``<name>.v<N>.md`` files from a single-component directory.

        Returns the number of templates registered. Missing directory is a
        no-op (deployment compatibility: the inline/fallback path still works).
        """
        root = Path(template_dir)
        if not root.is_dir():
            return 0
        loaded = 0
        for path in sorted(root.glob("*.md")):
            if ".v" not in path.stem:
                continue
            try:
                self._templates[f"{component}/{path.stem}"] = path.read_text(
                    encoding="utf-8"
                )
                loaded += 1
            except OSError:
                continue
        return loaded

    def load_root(self, template_root: Path | str) -> int:
        """Register ``<root>/<component>/<name>.v<N>.md`` files (DD-02 §2).

        Each subdirectory name is the component (judge/attribution/
        hypothesis/critic/sft). Returns the number of templates registered;
        a missing root is a no-op.
        """
        root = Path(template_root)
        if not root.is_dir():
            return 0
        loaded = 0
        for comp_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            loaded += self.load_dir(comp_dir, component=comp_dir.name)
        return loaded

    def render(self, component: str, name: str, vars: dict[str, Any]) -> str:
        """Find latest version of component/name, format with vars.

        F3 (audit4, DD-02 §4): a template placeholder the caller does not
        supply is a template/code contract drift — raise ConfigError instead
        of failing later with a KeyError (or silently degrading to an
        indeterminate judgment). Extra vars are tolerated (debug log only).
        """
        best = self._latest_key(component, name)
        if best is None:
            # Fallback: return a generic prompt
            return (
                f"组件: {component}，任务: {name}\n"
                + "\n".join(f"{k}: {v}" for k, v in vars.items())
            )
        required = _placeholders(self._templates[best])
        missing = required - set(vars)
        if missing:
            raise ConfigError(
                f"prompt {best!r} requires vars not supplied: {sorted(missing)}",
                context={"template": best, "missing": sorted(missing)},
            )
        extra = set(vars) - required
        if extra:
            logger.debug("render.extra_vars", template=best, extra=sorted(extra))
        return self._templates[best].format(**vars)

    def latest_version(self, component: str, name: str) -> int:
        """Return latest version number, 0 if none."""
        prefix = f"{component}/{name}.v"
        best = 0
        for k in self._templates:
            if k.startswith(prefix):
                try:
                    v = int(k[len(prefix) :])
                except ValueError:
                    continue
                best = max(best, v)
        return best

    def hash_of(self, name: str, model: str, temperature: float) -> str:
        """Prompt fingerprint: sha256(name@version|model|temperature|body_sha16).

        ``name`` is the full ``"<component>/<name>"`` key; the version is the
        current active (latest) version, 0 when only the fallback exists.

        F2 (audit4): the first 16 hex chars of the ACTIVE template body's
        sha256 are part of the payload, so editing a template's body without
        bumping its version still changes the fingerprint — a cassette
        recorded under the old body can never silently replay for the new
        one (ADR-7: prompt evolution invalidates regression assets).
        """
        if "/" in name:
            component, short = name.split("/", 1)
            version = self.latest_version(component, short)
            best = self._latest_key(component, short)
        else:
            version = 0
            best = None
        body_sha = ""
        if best is not None:
            body = self._templates[best]
            body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        payload = f"{name}@v{version}|{model}|{temperature}|{body_sha}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _latest_key(self, component: str, name: str) -> str | None:
        prefix = f"{component}/{name}.v"
        best: str | None = None
        best_v = -1
        for k in self._templates:
            if not k.startswith(prefix):
                continue
            # e) numeric version compare: lexicographic ordering would rank
            # ".v10" below ".v2" and silently pick a stale prompt.
            try:
                v = int(k[len(prefix):])
            except ValueError:
                continue
            if v > best_v:
                best, best_v = k, v
        return best
