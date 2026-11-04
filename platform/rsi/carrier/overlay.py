"""OverlayBuilder: copy-on-write SUT config overlay (DD-05).

Builds a destroyable overlay directory from an exported config version so
instrumentation (plans, extra skills) never mutates the source tree.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

OVERLAY_META = "overlay.json"


class OverlayBuilder:
    """Create/clean disposable config overlays."""

    @staticmethod
    def build(config_dir: Path, plan: Any | None = None) -> Path:
        """Copy ``config_dir`` to a sibling ``overlay-<uuid>`` and stamp metadata."""
        config_dir = Path(config_dir)
        overlay_dir = config_dir.parent / f"overlay-{uuid.uuid4().hex[:12]}"
        shutil.copytree(config_dir, overlay_dir, dirs_exist_ok=True)
        meta = {
            "source": str(config_dir),
            "destroyable": True,
            "plan_summary": getattr(plan, "model_dump", lambda: plan)()
            if plan is not None
            else None,
        }
        (overlay_dir / OVERLAY_META).write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        return overlay_dir

    @staticmethod
    def cleanup(overlay_dir: Path) -> None:
        """Remove an overlay only if it is marked destroyable."""
        overlay_dir = Path(overlay_dir)
        meta_path = overlay_dir / OVERLAY_META
        if not overlay_dir.exists():
            return
        if not meta_path.exists():
            return  # not an overlay we created — refuse to delete
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("destroyable"):
            shutil.rmtree(overlay_dir)
