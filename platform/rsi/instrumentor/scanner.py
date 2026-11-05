"""Scanner: fixed-path scan of a SUT config directory (DD-06 §4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

HOOK_CARRIERS = {"opencode"}


class ScanReport(BaseModel):
    skills: list[dict[str, Any]] = []
    mcp_servers: list[dict[str, Any]] = []
    scripts: list[dict[str, Any]] = []
    knowledge: dict[str, Any] | None = None
    carrier_hooks_supported: bool = False


class Scanner:
    @staticmethod
    def scan(config_dir: Path, carrier: str) -> ScanReport:
        config_dir = Path(config_dir)
        skills = Scanner._scan_skills(config_dir)
        servers = Scanner._scan_mcp(config_dir)
        scripts = Scanner._scan_scripts(config_dir)
        knowledge = Scanner._scan_knowledge(config_dir)
        return ScanReport(
            skills=skills,
            mcp_servers=servers,
            scripts=scripts,
            knowledge=knowledge,
            carrier_hooks_supported=carrier in HOOK_CARRIERS,
        )

    @staticmethod
    def _scan_skills(config_dir: Path) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        skills_dir = config_dir / "skills"
        if not skills_dir.is_dir():
            return out
        for md in sorted(skills_dir.rglob("SKILL.md")):
            out.append(
                {"name": md.parent.name, "path": str(md.relative_to(config_dir))}
            )
        return out

    @staticmethod
    def _scan_mcp(config_dir: Path) -> list[dict[str, Any]]:
        mcp_path = config_dir / "mcp.json"
        if not mcp_path.is_file():
            return []
        try:
            data = json.loads(mcp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        servers = data.get("mcpServers") or data.get("servers") or {}
        out: list[dict[str, Any]] = []
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            out.append(
                {
                    "name": name,
                    "command": cfg.get("command"),
                    "args": cfg.get("args", []),
                    "transport": "stdio" if cfg.get("command") else cfg.get("type", "unknown"),
                }
            )
        return out

    @staticmethod
    def _scan_scripts(config_dir: Path) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        scripts_dir = config_dir / "scripts"
        if not scripts_dir.is_dir():
            return out
        for ext, lang in ((".py", "python"), (".sh", "shell")):
            for f in sorted(scripts_dir.rglob(f"*{ext}")):
                out.append(
                    {
                        "path": str(f.relative_to(config_dir)),
                        "lang": lang,
                        "entry_hint": f.name,
                    }
                )
        return out

    @staticmethod
    def _scan_knowledge(config_dir: Path) -> dict[str, Any] | None:
        kdir = config_dir / "knowledge"
        if not kdir.is_dir():
            return None
        cfg = kdir / "retrieval.json"
        if cfg.is_file():
            try:
                return json.loads(cfg.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {"path": str(cfg)}
        return {"path": str(kdir)}
