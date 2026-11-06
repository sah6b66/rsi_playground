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

from rsi.log import get_logger

OVERLAY_META = "overlay.json"

logger = get_logger(__name__)


class OverlayBuilder:
    """Create/clean disposable config overlays."""

    @staticmethod
    def build(config_dir: Path, plan: Any | None = None) -> Path:
        """Copy ``config_dir`` to a sibling ``overlay-<uuid>`` and stamp metadata."""
        logger.debug("build.entry", config_dir=str(config_dir), has_plan=plan is not None)
        config_dir = Path(config_dir).resolve()
        overlay_dir = config_dir.parent / f"overlay-{uuid.uuid4().hex[:12]}"
        # A degenerate source (e.g. cwd from an empty config_version_id) would
        # place the overlay inside itself and recurse copytree forever.
        if overlay_dir == config_dir or overlay_dir.is_relative_to(config_dir):
            raise ValueError(
                f"overlay destination {overlay_dir} would nest inside its "
                f"source {config_dir}; refusing recursive copy"
            )
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
        logger.debug("build.exit", overlay_dir=str(overlay_dir))
        return overlay_dir

    @staticmethod
    def cleanup(overlay_dir: Path) -> None:
        """Remove an overlay only if it is marked destroyable."""
        logger.debug("cleanup.entry", overlay_dir=str(overlay_dir))
        overlay_dir = Path(overlay_dir)
        meta_path = overlay_dir / OVERLAY_META
        if not overlay_dir.exists():
            logger.debug("cleanup.exit", action="noop", reason="missing_dir")
            return
        if not meta_path.exists():
            logger.debug("cleanup.exit", action="noop", reason="not_overlay")  # not an overlay we created — refuse to delete
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("destroyable"):
            shutil.rmtree(overlay_dir)
            logger.debug("cleanup.exit", action="removed")
        else:
            logger.debug("cleanup.exit", action="kept", reason="not_destroyable")
