"""PromptLibrary — versioned string-template prompts (DD-02 §4).

Templates are keyed ``"<component>/<name>.v<N>"`` and rendered with
``str.format(**vars)``. Rendering picks the latest version present; when no
template matches, a generic fallback prompt is produced so offline callers
never hard-fail on a missing prompt.

Templates can also come from a template directory (C-6 frozen domain): a file
``<dir>/<name>.v<N>.md`` registers under the component given at load time
(defaults to ``judge``), so frozen ``.md`` files take priority over inline
entries while the fallback concatenation stays as last resort.

``hash_of`` implements the DD-08 §5.1/§5.4 prompt-fingerprint:
``sha256("<name>@<version>|<model>|<temperature>")``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

__all__ = ["PromptLibrary"]


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
        """Register ``<name>.v<N>.md`` files from a directory.

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

    def render(self, component: str, name: str, vars: dict[str, Any]) -> str:
        """Find latest version of component/name, format with vars."""
        best = self._latest_key(component, name)
        if best is None:
            # Fallback: return a generic prompt
            return (
                f"Component: {component}, Task: {name}\n"
                + "\n".join(f"{k}: {v}" for k, v in vars.items())
            )
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
        """Prompt fingerprint: sha256(name@version|model|temperature).

        ``name`` is the full ``"<component>/<name>"`` key; the version is the
        current active (latest) version, 0 when only the fallback exists.
        """
        if "/" in name:
            component, short = name.split("/", 1)
            version = self.latest_version(component, short)
        else:
            version = 0
        payload = f"{name}@v{version}|{model}|{temperature}"
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
