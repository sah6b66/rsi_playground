"""Startup self-diagnostic ("doctor") for the RSI Playground.

Validates runtime environment, configuration, external dependencies,
internal components and basic security posture. Designed to run even
when settings are broken — every issue is reported, never raised.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import ipaddress
import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, computed_field

# ── Models ────────────────────────────────────────────────────────────

Category = Literal["runtime", "config", "external", "internal", "security"]


class CheckResult(BaseModel):
    category: Category
    name: str
    status: Literal["pass", "warn", "fail"]
    message: str
    fix_hint: str | None = None


class DoctorReport(BaseModel):
    results: list[CheckResult] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=datetime.now)
    hostname: str = Field(default_factory=socket.gethostname)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "pass")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def warned(self) -> int:
        return sum(1 for r in self.results if r.status == "warn")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "fail")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_errors(self) -> bool:
        return any(r.status == "fail" for r in self.results)


def _py_version() -> tuple[int, int, int]:
    return sys.version_info[:3]  # type: ignore[return-value]


REQUIRED_PACKAGES = [
    "pydantic", "sqlmodel", "git", "structlog", "jwt", "argon2",
    "cryptography", "httpx", "docker", "fastapi",
]

RSI_MODULES = [
    "rsi.core", "rsi.db", "rsi.security", "rsi.versioning", "rsi.sandbox",
    "rsi.carrier", "rsi.tracing", "rsi.llm", "rsi.judge", "rsi.decision",
    "rsi.optimizer", "rsi.orchestrator", "rsi.instrumentor", "rsi.api",
]

REQUIRED_SECTIONS = [
    "server", "database", "git", "storage", "security",
    "llm", "carrier", "sandbox",
]

JWT_MIN_BYTES = 32
COMMON_SECRETS = {"test", "secret", "changeme", "password", "changeme123"}

CATEGORY_TITLES: list[tuple[str, str]] = [
    ("runtime", "[运行时环境]"),
    ("config", "[配置文件]"),
    ("external", "[外部依赖]"),
    ("internal", "[内部组件]"),
    ("security", "[安全检查]"),
]


class Doctor:
    """Self-diagnostic engine. All checks are non-fatal by design."""

    def __init__(self, settings_path: Path | None = None) -> None:
        self.settings_path = (
            settings_path
            or Path(os.environ.get("RSI_SETTINGS", "settings.yaml"))
        )
        self._raw_settings: dict[str, Any] | None = None
        self._settings_load_error: str | None = None

    # ── Public API ───────────────────────────────────────────────────

    async def run_all(self) -> DoctorReport:
        results: list[CheckResult] = []
        for check in (
            self.check_runtime,
            self.check_config,
            self.check_external,
            self.check_internal,
            self.check_security,
            self.check_freeze,
        ):
            try:
                results.extend(check())
            except Exception as exc:  # doctor must never crash
                results.append(
                    CheckResult(
                        category="internal",
                        name=check.__name__,
                        status="fail",
                        message=f"检查器自身异常: {exc}",
                        fix_hint="请将此错误上报给平台开发者",
                    )
                )
        return DoctorReport(results=results)

    # ── Category 1: Runtime ──────────────────────────────────────────

    def check_runtime(self) -> list[CheckResult]:
        results: list[CheckResult] = []

        v = _py_version()
        if v >= (3, 12):
            results.append(CheckResult(
                category="runtime", name="Python version", status="pass",
                message=f"Python {v[0]}.{v[1]}.{v[2]}"))
        else:
            results.append(CheckResult(
                category="runtime", name="Python version", status="fail",
                message=f"Python {v[0]}.{v[1]}.{v[2]} < 3.12",
                fix_hint="安装 Python 3.12+ (uv python install 3.12)"))

        ok, missing = [], []
        for pkg in REQUIRED_PACKAGES:
            try:
                importlib.import_module(pkg)
                ok.append(pkg)
            except Exception:
                missing.append(pkg)
        if missing:
            results.append(CheckResult(
                category="runtime", name="依赖包", status="fail",
                message=f"缺少依赖包: {', '.join(missing)} "
                        f"({len(ok)}/{len(REQUIRED_PACKAGES)} 可用)",
                fix_hint="uv sync"))
        else:
            results.append(CheckResult(
                category="runtime", name="依赖包", status="pass",
                message=f"依赖包完整 ({len(ok)}/{len(REQUIRED_PACKAGES)})"))

        home = Path(os.environ.get("RSI_HOME", "./var"))
        try:
            home.mkdir(parents=True, exist_ok=True)
            probe = home / ".doctor-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            results.append(CheckResult(
                category="runtime", name="RSI_HOME 可写", status="pass",
                message=f"RSI_HOME 可写: {home}"))
        except OSError as exc:
            results.append(CheckResult(
                category="runtime", name="RSI_HOME 可写", status="fail",
                message=f"RSI_HOME 不可写: {home} ({exc})",
                fix_hint=f"设置 RSI_HOME 到可写目录，或授权 {home}"))
        return results

    # ── Settings loading helper ──────────────────────────────────────

    def _load_settings(self) -> dict[str, Any]:
        if self._raw_settings is None:
            try:
                text = self.settings_path.read_text(encoding="utf-8")
                self._raw_settings = yaml.safe_load(text) or {}
                if not isinstance(self._raw_settings, dict):
                    self._raw_settings = {}
                    self._settings_load_error = "YAML 根节点不是映射"
            except FileNotFoundError:
                self._raw_settings = {}
            except yaml.YAMLError as exc:
                self._raw_settings = {}
                self._settings_load_error = str(exc).splitlines()[0]
        return self._raw_settings

    # ── Category 2: Config ───────────────────────────────────────────

    def check_config(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        cfg = self._load_settings()

        # file existence
        if self.settings_path.exists():
            results.append(CheckResult(
                category="config", name="settings.yaml 存在", status="pass",
                message=f"settings.yaml 存在: {self.settings_path}"))
            example = self.settings_path.parent / "settings.yaml.example"
            if example.exists():
                pass  # nothing extra
        else:
            example = self.settings_path.parent / "settings.yaml.example"
            hint = (
                f"复制 {example} 为 settings.yaml 并按需修改"
                if example.exists()
                else "参考 examples/settings.yaml.example 创建 settings.yaml"
            )
            results.append(CheckResult(
                category="config", name="settings.yaml 存在", status="fail",
                message=f"未找到 {self.settings_path}",
                fix_hint=hint))

        # YAML syntax
        if self._settings_load_error is not None:
            results.append(CheckResult(
                category="config", name="YAML 语法", status="fail",
                message=f"YAML 解析失败: {self._settings_load_error}",
                fix_hint="使用 yaml linter 或 python -c 'import yaml;"
                         "yaml.safe_load(open(\"settings.yaml\"))' 定位错误"))
        elif not self.settings_path.exists():
            results.append(CheckResult(
                category="config", name="YAML 语法", status="fail",
                message="无法检查：文件不存在"))
        else:
            results.append(CheckResult(
                category="config", name="YAML 语法", status="pass",
                message="YAML 语法正确"))

        if self._settings_load_error is not None:
            return results  # nothing further can be checked

        # required sections
        missing_sections = [s for s in REQUIRED_SECTIONS if s not in cfg]
        if missing_sections:
            results.append(CheckResult(
                category="config", name="必需段", status="fail",
                message=f"缺少必需段: {', '.join(missing_sections)}",
                fix_hint="在 settings.yaml 中补齐缺失段，参考 "
                         "examples/settings.yaml.example"))
        else:
            results.append(CheckResult(
                category="config", name="必需段", status="pass",
                message=f"必需段齐全 ({len(REQUIRED_SECTIONS)}/{len(REQUIRED_SECTIONS)})"))

        # server.port
        port = (cfg.get("server") or {}).get("port", 8000)
        if isinstance(port, int) and 1 <= port <= 65534:
            results.append(CheckResult(
                category="config", name="server.port", status="pass",
                message=f"server.port = {port}"))
        else:
            results.append(CheckResult(
                category="config", name="server.port", status="fail",
                message=f"server.port = {port!r} 不在 1-65534 范围",
                fix_hint="设置 server.port 为 1-65534 之间的整数"))

        # database.url
        db_url = (cfg.get("database") or {}).get("url", "")
        if str(db_url).startswith(("sqlite://", "postgresql://")):
            results.append(CheckResult(
                category="config", name="database.url", status="pass",
                message=f"database.url 前缀合法"))
        else:
            results.append(CheckResult(
                category="config", name="database.url", status="fail",
                message=f"database.url = {db_url!r}，仅支持 sqlite:// 或 postgresql://",
                fix_hint="P0 使用 sqlite:///platform.db；P1 使用 postgresql://…"))

        # git.repo.path parent writable
        home = Path(os.environ.get("RSI_HOME", "./var"))
        repo_path = Path((cfg.get("git") or {}).get("repo", {})
                         .get("path", "git/platform-repo.git"))
        repo_path = repo_path if repo_path.is_absolute() else home / repo_path
        try:
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            probe = repo_path.parent / ".doctor-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            results.append(CheckResult(
                category="config", name="git.repo 父目录", status="pass",
                message=f"git.repo 父目录可写: {repo_path.parent}"))
        except OSError as exc:
            results.append(CheckResult(
                category="config", name="git.repo 父目录", status="fail",
                message=f"git.repo 父目录不可写: {repo_path.parent} ({exc})",
                fix_hint=f"授权写权限或修改 git.repo.path"))

        # carrier.hosts
        hosts = (cfg.get("carrier") or {}).get("hosts") or {}
        if hosts:
            results.append(CheckResult(
                category="config", name="carrier.hosts", status="pass",
                message=f"carrier.hosts 定义 {len(hosts)} 个主机"))
        else:
            results.append(CheckResult(
                category="config", name="carrier.hosts", status="fail",
                message="carrier.hosts 未定义任何主机",
                fix_hint="至少定义一个 carrier 主机 (docker: local)"))

        # sandbox.backends
        backends = (cfg.get("sandbox") or {}).get("backends") or {}
        if not backends:
            results.append(CheckResult(
                category="config", name="sandbox.backends", status="fail",
                message="sandbox.backends 未定义任何后端",
                fix_hint="至少定义一个仿真后端 (如 ensp-pro-1)"))
        elif list(backends) == ["fake"]:
            results.append(CheckResult(
                category="config", name="sandbox.backends", status="warn",
                message="仅有 fake 后端（不影响 P0 演进，但无法接入真实仿真）",
                fix_hint="接入真实后端时补充 ensp-pro-1 / pnetlab 配置"))
        else:
            results.append(CheckResult(
                category="config", name="sandbox.backends", status="pass",
                message=f"sandbox.backends 定义 {len(backends)} 个后端"))

        # subnets validity + overlap
        results.extend(self._check_subnets(cfg))

        # env vars
        results.extend(self._check_secret_envs())
        return results

    def _check_subnets(self, cfg: dict[str, Any]) -> list[CheckResult]:
        entries: list[tuple[str, str]] = []  # (subnet, owner)
        for name, h in ((cfg.get("carrier") or {}).get("hosts") or {}).items():
            for s in (h or {}).get("subnets") or []:
                entries.append((str(s), f"carrier.hosts.{name}"))
        for name, b in ((cfg.get("sandbox") or {}).get("backends") or {}).items():
            for s in (b or {}).get("subnets") or []:
                entries.append((str(s), f"sandbox.backends.{name}"))

        parsed: list[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str, str]] = []
        invalid: list[str] = []
        for raw, owner in entries:
            try:
                net = ipaddress.ip_network(raw, strict=False)
                parsed.append((net, raw, owner))
            except ValueError:
                invalid.append(f"{owner}: {raw}")
        if invalid:
            return [CheckResult(
                category="config", name="子网格式", status="fail",
                message=f"无效子网: {'; '.join(invalid)}",
                fix_hint="使用 CIDR 格式 x.x.x.x/y (如 10.100.0.0/16)")]

        overlaps: list[str] = []
        for i in range(len(parsed)):
            for j in range(i + 1, len(parsed)):
                a, b = parsed[i][0], parsed[j][0]
                if a.version == b.version and a.overlaps(b):
                    overlaps.append(
                        f"{parsed[i][2]} {parsed[i][1]} ↔ "
                        f"{parsed[j][2]} {parsed[j][1]}")
        if overlaps:
            return [CheckResult(
                category="config", name="子网重叠", status="fail",
                message=f"子网重叠: {'; '.join(overlaps)}",
                fix_hint="为每个主机/后端分配不重叠的 /16 或更细子网")]

        if entries:
            return [CheckResult(
                category="config", name="子网规划", status="pass",
                message=f"{len(entries)} 个子网格式合法且互不重叠")]
        return [CheckResult(
            category="config", name="子网规划", status="pass",
            message="无子网需要检查")]

    def _check_secret_envs(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        jwt = os.environ.get("RSI_JWT_SECRET", "")
        if not jwt:
            results.append(CheckResult(
                category="config", name="RSI_JWT_SECRET", status="fail",
                message="RSI_JWT_SECRET 未设置",
                fix_hint="export RSI_JWT_SECRET=$(openssl rand -hex 32)"))
        elif len(jwt.encode()) < JWT_MIN_BYTES:
            results.append(CheckResult(
                category="config", name="RSI_JWT_SECRET", status="fail",
                message=f"RSI_JWT_SECRET 长度不足 "
                        f"({len(jwt.encode())} 字节 < {JWT_MIN_BYTES})",
                fix_hint="export RSI_JWT_SECRET=$(openssl rand -hex 32)"))
        else:
            results.append(CheckResult(
                category="config", name="RSI_JWT_SECRET", status="pass",
                message=f"RSI_JWT_SECRET 长度 {len(jwt.encode())} 字节"))

        mk = os.environ.get("RSI_MASTER_KEY", "")
        if not mk:
            results.append(CheckResult(
                category="config", name="RSI_MASTER_KEY", status="fail",
                message="RSI_MASTER_KEY 未设置",
                fix_hint="export RSI_MASTER_KEY=$(openssl rand -base64 32)"))
        else:
            try:
                decoded = base64.b64decode(mk, validate=True)
                if len(decoded) != 32:
                    raise ValueError(f"解码后 {len(decoded)} 字节 != 32")
                results.append(CheckResult(
                    category="config", name="RSI_MASTER_KEY", status="pass",
                    message="RSI_MASTER_KEY 为合法 base64 32 字节"))
            except Exception:
                results.append(CheckResult(
                    category="config", name="RSI_MASTER_KEY", status="fail",
                    message="RSI_MASTER_KEY 不是合法的 base64 32 字节",
                    fix_hint="export RSI_MASTER_KEY=$(openssl rand -base64 32)"))

        if not (os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("OPENAI_COMPAT_API_KEY")):
            results.append(CheckResult(
                category="config", name="LLM API Key", status="warn",
                message="未设置 ANTHROPIC_API_KEY 或 OPENAI_COMPAT_API_KEY",
                fix_hint="设置其一以启用 LLM 评测 (judge/critic)"))
        else:
            results.append(CheckResult(
                category="config", name="LLM API Key", status="pass",
                message="至少一个 LLM API Key 已设置"))
        return results

    # ── Category 3: External ─────────────────────────────────────────

    def check_external(self) -> list[CheckResult]:
        results: list[CheckResult] = []

        # Git
        git_ver = self._git_version()
        if git_ver is None:
            results.append(CheckResult(
                category="external", name="Git", status="fail",
                message="未找到 git 可执行文件",
                fix_hint="安装 Git >= 2.30 (https://git-scm.com)"))
        else:
            major, minor, _ = git_ver
            if (major, minor) >= (2, 30):
                results.append(CheckResult(
                    category="external", name="Git", status="pass",
                    message=f"Git {major}.{minor}.{_}"))
            else:
                results.append(CheckResult(
                    category="external", name="Git", status="fail",
                    message=f"Git {major}.{minor}.{_} < 2.30",
                    fix_hint="升级 Git 到 2.30+"))

        # git identity
        name = self._git_config("user.name")
        email = self._git_config("user.email")
        if name and email:
            results.append(CheckResult(
                category="external", name="Git 身份", status="pass",
                message=f"git user: {name} <{email}>"))
        else:
            results.append(CheckResult(
                category="external", name="Git 身份", status="warn",
                message="git user.name / user.email 未配置",
                fix_hint="git config --global user.name …; "
                         "git config --global user.email …"))

        # Docker
        docker_ver = self._docker_version()
        if docker_ver is None:
            results.append(CheckResult(
                category="external", name="Docker", status="warn",
                message="Docker 不可达",
                fix_hint="启动 Docker Desktop 并等待就绪"))
        else:
            results.append(CheckResult(
                category="external", name="Docker", status="pass",
                message=f"Docker {docker_ver}"))

        # sandbox backend SSH reachability
        cfg = self._load_settings()
        for bname, b in ((cfg.get("sandbox") or {}).get("backends") or {}).items():
            host = (b or {}).get("host") or ""
            if not host:
                continue
            port = int((b or {}).get("ssh_port", 22))
            if self._tcp_reachable(host, port):
                results.append(CheckResult(
                    category="external", name=f"仿真后端 {bname}", status="pass",
                    message=f"仿真后端 {bname} ({host}:{port}) 可达"))
            else:
                results.append(CheckResult(
                    category="external", name=f"仿真后端 {bname}", status="warn",
                    message=f"仿真后端 {bname} ({host}:{port}) 不可达",
                    fix_hint="检查仿真主机是否启动，SSH 是否开放"))

        # remote carrier hosts (docker starts with ssh://)
        for hname, h in ((cfg.get("carrier") or {}).get("hosts") or {}).items():
            docker = str((h or {}).get("docker", "local"))
            if docker.startswith("ssh://"):
                host_port = docker[len("ssh://"):].split(":", 1)
                host = host_port[0]
                port = int(host_port[1]) if len(host_port) > 1 else 22
                if self._tcp_reachable(host, port):
                    results.append(CheckResult(
                        category="external", name=f"Carrier 主机 {hname}",
                        status="pass",
                        message=f"carrier.hosts.{hname} SSH 可达"))
                else:
                    results.append(CheckResult(
                        category="external", name=f"Carrier 主机 {hname}",
                        status="warn",
                        message=f"carrier.hosts.{hname} ({host}:{port}) SSH 不可达",
                        fix_hint="检查远程 Docker 主机 SSH 是否开放"))
        return results

    @staticmethod
    def _git_version() -> tuple[int, int, int] | None:
        try:
            out = subprocess.run(
                ["git", "--version"], capture_output=True, text=True,
                timeout=5, shell=False,
            )
            parts = out.stdout.strip().split()[-1].split(".")
            return int(parts[0]), int(parts[1]), int(parts[2])
        except Exception:
            return None

    @staticmethod
    def _git_config(key: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "config", "--global", "--get", key],
                capture_output=True, text=True, timeout=5,
            )
            v = out.stdout.strip()
            return v or None
        except Exception:
            return None

    @staticmethod
    def _docker_version() -> str | None:
        try:
            import docker
            client = docker.from_env(timeout=3)
            return client.version()["ApiVersion"]
        except Exception:
            return None

    @staticmethod
    def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    # ── Category 4: Internal ─────────────────────────────────────────

    def check_internal(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        failed: list[str] = []
        for mod in RSI_MODULES:
            try:
                importlib.import_module(mod)
            except Exception:
                failed.append(mod)
        if failed:
            results.append(CheckResult(
                category="internal", name="模块导入", status="fail",
                message=f"无法导入 {len(failed)} 个模块: {', '.join(failed)}",
                fix_hint="检查安装完整性 (uv sync) 或修复导入错误"))
        else:
            results.append(CheckResult(
                category="internal", name="模块导入", status="pass",
                message=f"{len(RSI_MODULES)} 个模块全部可导入"))

        home = Path(os.environ.get("RSI_HOME", "./var"))
        cfg = self._load_settings()

        # git bare repo
        repo_path = Path((cfg.get("git") or {}).get("repo", {})
                         .get("path", "git/platform-repo.git"))
        repo_path = repo_path if repo_path.is_absolute() else home / repo_path
        if repo_path.exists():
            results.append(CheckResult(
                category="internal", name="Git 裸仓", status="pass",
                message=f"Git 裸仓已存在: {repo_path}"))
        else:
            try:
                repo_path.parent.mkdir(parents=True, exist_ok=True)
                results.append(CheckResult(
                    category="internal", name="Git 裸仓", status="pass",
                    message=f"Git 裸仓可创建: {repo_path}"))
            except OSError as exc:
                results.append(CheckResult(
                    category="internal", name="Git 裸仓", status="fail",
                    message=f"Git 裸仓不可创建: {repo_path} ({exc})",
                    fix_hint="检查目录权限或修改 git.repo.path"))

        # database file directory
        db_url = str((cfg.get("database") or {}).get("url", "")) or \
            f"sqlite:///{home / 'platform.db'}"
        dir_ok = self._db_dir_writable(db_url, home)
        results.append(dir_ok)

        # traces/artifacts/logs directories
        for label, key, default in (
            ("traces", "traces_dir", "traces"),
            ("artifacts", "artifacts_dir", "artifacts"),
            ("logs", "dir", "logs"),
        ):
            section = cfg.get("storage") if label != "logs" else cfg.get("logging")
            p = Path((section or {}).get(key, default))
            p = p if p.is_absolute() else home / p
            try:
                p.mkdir(parents=True, exist_ok=True)
                results.append(CheckResult(
                    category="internal", name=f"{label} 目录", status="pass",
                    message=f"{label} 目录就绪: {p}"))
            except OSError as exc:
                results.append(CheckResult(
                    category="internal", name=f"{label} 目录", status="fail",
                    message=f"{label} 目录不可创建: {p} ({exc})",
                    fix_hint="检查目录权限或修改 storage/logging 配置"))

        # API app creatable
        try:
            from rsi.api.app import create_app
            create_app({})
            results.append(CheckResult(
                category="internal", name="API 应用", status="pass",
                message="API 应用可创建 (OpenAPI schema 可生成)"))
        except Exception as exc:
            results.append(CheckResult(
                category="internal", name="API 应用", status="fail",
                message=f"create_app() 失败: {exc}",
                fix_hint="检查 fastapi 路由注册与依赖"))
        return results

    @staticmethod
    def _db_dir_writable(db_url: str, home: Path) -> CheckResult:
        if db_url.startswith("postgresql://"):
            return CheckResult(
                category="internal", name="数据库目录", status="pass",
                message="PostgreSQL 数据库 (目录检查跳过)")
        if db_url.startswith("sqlite:///"):
            fpath = db_url[len("sqlite:///"):]
            if not fpath:  # sqlite:// → in-memory or default
                return CheckResult(
                    category="internal", name="数据库目录", status="pass",
                    message="SQLite 默认路径")
            p = Path(fpath)
            if not p.is_absolute():
                p = home / fpath
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                probe = p.parent / ".doctor-db-write"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                return CheckResult(
                    category="internal", name="数据库目录", status="pass",
                    message=f"数据库目录可写: {p.parent}")
            except OSError as exc:
                return CheckResult(
                    category="internal", name="数据库目录", status="fail",
                    message=f"数据库目录不可写: {p.parent} ({exc})",
                    fix_hint="检查目录权限或修改 database.url")
        return CheckResult(
            category="internal", name="数据库目录", status="warn",
            message=f"未知 database.url，跳过检查: {db_url!r}")

    # ── Category 5: Security ─────────────────────────────────────────

    def check_freeze(self) -> list[CheckResult]:
        """R2-H2: judge frozen-domain verification finally has a consumer.

        When ``judge/rules/freeze.manifest`` exists, every manifested file
        must hash clean AND every frozen-glob file must be listed (see
        ``rsi.judge.rules.freeze_check.verify``). No manifest configured ->
        informational pass (freeze simply not enabled in this deployment).
        """
        try:
            from rsi.judge.rules import freeze_check
        except Exception as exc:  # pragma: no cover - import guard
            return [CheckResult(
                category="security", name="judge 冻结校验", status="warn",
                message=f"无法加载 freeze_check 模块: {exc}")]
        if not freeze_check.MANIFEST_PATH.is_file():
            return [CheckResult(
                category="security", name="judge 冻结校验", status="pass",
                message="未配置 freeze.manifest，跳过冻结校验")]
        ok = freeze_check.verify()
        if ok:
            return [CheckResult(
                category="security", name="judge 冻结校验", status="pass",
                message=f"冻结域校验通过（manifest={freeze_check.MANIFEST_PATH.name}）")]
        return [CheckResult(
            category="security", name="judge 冻结校验", status="fail",
            message="冻结域校验失败：judge 规则/提示词被篡改或存在未列册文件",
            fix_hint="python -m rsi.judge.rules.freeze_check 查看差异；"
                     "确认改动合规后 python -m rsi.judge.rules.freeze_check write 重列册")]


    def check_security(self) -> list[CheckResult]:
        results: list[CheckResult] = []
        jwt = os.environ.get("RSI_JWT_SECRET", "")

        low = jwt.lower()
        if jwt and any(low == c or low == c * len(jwt) for c in COMMON_SECRETS):
            results.append(CheckResult(
                category="security", name="JWT 密钥常见默认值", status="warn",
                message="JWT 密钥疑似常见/默认值",
                fix_hint="export RSI_JWT_SECRET=$(openssl rand -hex 32)"))
        elif jwt and len(jwt.encode()) >= JWT_MIN_BYTES:
            results.append(CheckResult(
                category="security", name="JWT 密钥长度", status="pass",
                message=f"JWT 密钥长度 {len(jwt.encode())} 字节"))
        else:
            results.append(CheckResult(
                category="security", name="JWT 密钥长度", status="fail",
                message="JWT 密钥缺失或过短 (< 32 字节)",
                fix_hint="export RSI_JWT_SECRET=$(openssl rand -hex 32)"))

        mk = os.environ.get("RSI_MASTER_KEY", "")
        if jwt and mk and jwt == mk:
            results.append(CheckResult(
                category="security", name="密钥相同", status="warn",
                message="Master Key 与 JWT 密钥相同",
                fix_hint="为两者分别生成独立随机值"))
        else:
            results.append(CheckResult(
                category="security", name="密钥相同", status="pass",
                message="JWT 密钥与 Master Key 相互独立"))

        try:
            decoded = base64.b64decode(mk, validate=True)
            if len(decoded) == 32:
                results.append(CheckResult(
                    category="security", name="Master Key", status="pass",
                    message="Master Key 有效"))
            else:
                results.append(CheckResult(
                    category="security", name="Master Key", status="fail",
                    message=f"Master Key 解码后 {len(decoded)} 字节 != 32",
                    fix_hint="export RSI_MASTER_KEY=$(openssl rand -base64 32)"))
        except Exception:
            results.append(CheckResult(
                category="security", name="Master Key", status="fail",
                message="Master Key 缺失或非法 base64",
                fix_hint="export RSI_MASTER_KEY=$(openssl rand -base64 32)"))

        # plaintext password scan
        hits = self._scan_plaintext_passwords()
        if hits:
            results.append(CheckResult(
                category="security", name="明文密码", status="warn",
                message=f"settings.yaml 中有明文密码: {', '.join(hits)}",
                fix_hint="建议使用 password_ref 引用 CredentialStore"))
        else:
            results.append(CheckResult(
                category="security", name="明文密码", status="pass",
                message="settings.yaml 未发现明文密码"))
        return results

    def _scan_plaintext_passwords(self) -> list[str]:
        if not self.settings_path.exists():
            return []
        try:
            data = yaml.safe_load(self.settings_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        hits: list[str] = []

        def walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str) and "password" in str(k).lower() \
                            and not str(k).endswith("_ref") and v:
                        hits.append(f"{path}.{k}")
                    else:
                        walk(v, f"{path}.{k}" if path else str(k))
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    walk(item, f"{path}[{i}]")

        walk(data, "")
        return hits

    def print_report(self, report: DoctorReport) -> None:
        """Print the formatted report, safe on non-UTF8 consoles (GBK etc.)."""
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(self.format_report(report))

    # ── Formatting ───────────────────────────────────────────────────

    def format_report(self, report: DoctorReport) -> str:
        line = "=" * 60
        out = [
            line,
            "  RSI Playground Doctor — 自检报告",
            line,
            f"  时间: {report.generated_at:%Y-%m-%d %H:%M:%S}",
            f"  主机: {report.hostname}",
            line,
            "",
        ]
        icons = {"pass": "✓", "warn": "⚠", "fail": "✗"}
        for cat, title in CATEGORY_TITLES:
            items = [r for r in report.results if r.category == cat]
            if not items:
                continue
            out.append(title)
            for r in items:
                out.append(f"  {icons[r.status]} {r.message}")
                if r.fix_hint and r.status != "pass":
                    out.append(f"    → 修复: {r.fix_hint}")
            out.append("")
        status_line = (
            "✗ 存在阻塞项，需修复后启动"
            if report.has_errors
            else ("⚠ 存在警告，可启动但建议修复" if report.warned else
                  "✓ 一切正常")
        )
        out += [
            line,
            f"  结果: {report.passed} 通过 / {report.warned} 警告 / "
            f"{report.failed} 错误",
            f"  状态: {status_line}",
            line,
        ]
        return "\n".join(out)


# ── CLI ──────────────────────────────────────────────────────────────

async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    doctor = Doctor()
    report = await doctor.run_all()
    print(doctor.format_report(report))
    sys.exit(0 if not report.has_errors else 1)


if __name__ == "__main__":
    asyncio.run(main())
