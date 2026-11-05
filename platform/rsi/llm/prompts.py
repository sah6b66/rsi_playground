"""PromptLibrary — versioned string-template prompts (DD-02 §4).

Templates are keyed ``"<component>/<name>.v<N>"`` and rendered with
``str.format(**vars)``. Rendering picks the latest version present; when no
template matches, a generic fallback prompt is produced so offline callers
never hard-fail on a missing prompt.
"""

from __future__ import annotations

__all__ = ["PromptLibrary"]


class PromptLibrary:
    """Simple string-template prompt library with version tracking."""

    def __init__(self, templates: dict[str, str] | None = None):
        # templates: {"<component>/<name>.v<N>": "template with {vars}"}
        self._templates = templates or {}

    def render(self, component: str, name: str, vars: dict) -> str:
        """Find latest version of component/name, format with vars."""
        key = f"{component}/{name}"
        # Find matching template (latest version)
        best = None
        for k in self._templates:
            if k.startswith(f"{component}/{name}.v"):
                if best is None or k > best:
                    best = k
        if best is None:
            # Fallback: return a generic prompt
            return (
                f"Component: {component}, Task: {name}\n"
                + "\n".join(f"{k}: {v}" for k, v in vars.items())
            )
        return self._templates[best].format(**vars)

    def latest_version(self, component: str, name: str) -> int:
        """Return latest version number, 0 if none."""
        best = 0
        prefix = f"{component}/{name}.v"
        for k in self._templates:
            if k.startswith(prefix):
                try:
                    v = int(k[len(prefix) :])
                except ValueError:
                    continue
                best = max(best, v)
        return best
