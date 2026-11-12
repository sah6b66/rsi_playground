"""Doctor (startup self-diagnostic) unit tests."""

from __future__ import annotations

import base64
import textwrap
from pathlib import Path

import pytest

from rsi.doctor import CheckResult, Doctor, DoctorReport

VALID_SETTINGS = """
server:
  host: 0.0.0.0
  port: 8000
database:
  url: sqlite:///platform.db
git:
  repo:
    path: git/platform-repo.git
storage:
  traces_dir: traces
  artifacts_dir: artifacts
  recordings_dir: recordings
security:
  jwt_secret_env: RSI_JWT_SECRET
llm:
  providers:
    anthropic:
      api_key_env: ANTHROPIC_API_KEY
carrier:
  hosts:
    main:
      docker: local
      subnets: ["10.100.0.0/16"]
sandbox:
  backends:
    ensp-pro-1:
      kind: ensp-pro
      url: https://192.0.2.1:8443
      subnets: ["10.101.0.0/16"]
"""


def _write_settings(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "settings.yaml"
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


class TestCheckResult:
    def test_model_creation(self):
        r = CheckResult(
            category="runtime",
            name="Python version",
            status="pass",
            message="Python 3.12.7",
        )
        assert r.category == "runtime"
        assert r.fix_hint is None

    def test_status_validation(self):
        with pytest.raises(Exception):
            CheckResult(
                category="runtime", name="x", status="bogus", message="m"
            )


class TestRuntime:
    def test_python_version_ok(self, monkeypatch):
        monkeypatch.setattr("rsi.doctor._py_version", lambda: (3, 12, 7))
        d = Doctor()
        rs = d.check_runtime()
        r = next(x for x in rs if x.name == "Python version")
        assert r.status == "pass"

    def test_python_version_too_old(self, monkeypatch):
        monkeypatch.setattr("rsi.doctor._py_version", lambda: (3, 11, 0))
        d = Doctor()
        rs = d.check_runtime()
        r = next(x for x in rs if x.name == "Python version")
        assert r.status == "fail"

    def test_rsi_home_writable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RSI_HOME", str(tmp_path / "var"))
        d = Doctor()
        rs = d.check_runtime()
        r = next(x for x in rs if "RSI_HOME" in x.name)
        assert r.status == "pass"


class TestConfig:
    def test_valid_settings(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS)
        monkeypatch.setenv("RSI_JWT_SECRET", "a" * 64)
        mk = base64.b64encode(b"\x00" * 32).decode()
        monkeypatch.setenv("RSI_MASTER_KEY", mk)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        fails = [r for r in rs if r.status == "fail"]
        assert not fails, fails

    def test_missing_settings(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        d = Doctor(settings_path=tmp_path / "nope.yaml")
        rs = d.check_config()
        r = next(x for x in rs if x.name == "settings.yaml 存在")
        assert r.status == "fail"
        assert r.fix_hint

    def test_invalid_yaml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, "server: [unclosed\n  port: ::")
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "YAML" in x.name)
        assert r.status == "fail"

    def test_missing_section(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS.replace("sandbox:\n", "# sandbox removed\n", 1))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "必需段" in x.name)
        assert r.status == "fail"
        assert "sandbox" in r.message

    def test_bad_port(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS.replace("port: 8000", "port: 99999"))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "port" in x.name.lower())
        assert r.status == "fail"

    def test_bad_database_url(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS.replace(
            "sqlite:///platform.db", "mysql://x/y"))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "database" in x.name.lower())
        assert r.status == "fail"

    def test_jwt_secret_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS)
        monkeypatch.delenv("RSI_JWT_SECRET", raising=False)
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "JWT" in x.name)
        assert r.status == "fail"

    def test_jwt_secret_short(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS)
        monkeypatch.setenv("RSI_JWT_SECRET", "short")
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "JWT" in x.name)
        assert r.status == "fail"
        assert "32" in r.message

    def test_master_key_invalid(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS)
        monkeypatch.setenv("RSI_JWT_SECRET", "a" * 64)
        monkeypatch.setenv("RSI_MASTER_KEY", "not-base64!!!")
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "Master" in x.name or "MASTER" in x.name)
        assert r.status == "fail"

    def test_no_llm_key_warns(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS)
        monkeypatch.setenv("RSI_JWT_SECRET", "a" * 64)
        mk = base64.b64encode(b"\x00" * 32).decode()
        monkeypatch.setenv("RSI_MASTER_KEY", mk)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "LLM" in x.name)
        assert r.status == "warn"


class TestSandboxBackends:
    def test_valid_kind_url_passes(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS)
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        # No fail/warn mentioning sandbox.backends.<name>.kind/.url/旧字段
        bad = [r for r in rs if r.status != "pass"
               and ("kind" in r.name or ".url" in r.name or "旧字段" in r.name)]
        assert not bad, bad

    def test_url_empty_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS.replace(
            "      url: https://192.0.2.1:8443\n", ""))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if x.name == "sandbox.backends.ensp-pro-1.url")
        assert r.status == "fail"
        assert "url" in r.message

    def test_bad_kind_fails(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS.replace(
            "kind: ensp-pro", "kind: gns3"))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if x.name.endswith(".kind"))
        assert r.status == "fail"

    def test_legacy_ssh_keys_warn(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS.replace(
            "      kind: ensp-pro",
            "      host: 192.0.2.1\n      ssh_user: administrator\n"
            "      kind: ensp-pro"))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "旧字段" in x.name)
        assert r.status == "warn"
        assert "host" in r.message and "ssh_user" in r.message

    def test_fake_backend_without_url_ok(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS.replace(
            "kind: ensp-pro", "kind: fake").replace(
            "      url: https://192.0.2.1:8443\n", ""))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        bad = [r for r in rs if r.status == "fail"
               and ("kind" in r.name or ".url" in r.name)]
        assert not bad, bad


class TestSubnets:
    def test_invalid_subnet(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS.replace(
            "10.101.0.0/16", "10.101.0.0/33"))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "子网" in x.name)
        assert r.status == "fail"

    def test_overlapping_subnets(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS.replace(
            '10.101.0.0/16', '10.100.5.0/24'))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "子网" in x.name)
        assert r.status == "fail"
        assert "重叠" in r.message


class TestSecurity:
    def test_common_jwt_secret_warns(self, monkeypatch):
        monkeypatch.setenv("RSI_JWT_SECRET", "changeme")
        d = Doctor()
        rs = d.check_security()
        r = next(x for x in rs if "默认" in x.name or "常见" in x.name)
        assert r.status == "warn"

    def test_same_as_master_key_warns(self, monkeypatch):
        v = "a" * 64
        monkeypatch.setenv("RSI_JWT_SECRET", v)
        monkeypatch.setenv("RSI_MASTER_KEY", v)
        d = Doctor()
        rs = d.check_security()
        r = next(x for x in rs if "相同" in x.name or "Master" in x.name)
        assert r.status == "warn"

    def test_plaintext_password_warns(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(
            tmp_path,
            VALID_SETTINGS + """
ssh_access:
  host: 1.2.3.4
  password: hunter2
""",
        )
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_security()
        r = next(x for x in rs if "明文密码" in x.name)
        assert r.status == "warn"


class TestReport:
    def test_aggregation(self):
        report = DoctorReport(
            results=[
                CheckResult(category="runtime", name="a", status="pass", message="m"),
                CheckResult(category="runtime", name="b", status="warn", message="m"),
                CheckResult(category="config", name="c", status="fail", message="m"),
            ],
        )
        assert report.passed == 1
        assert report.warned == 1
        assert report.failed == 1
        assert report.has_errors

    def test_format_report(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_settings(tmp_path, VALID_SETTINGS)
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        report = DoctorReport(results=d.check_security())
        text = d.format_report(report)
        assert isinstance(text, str)
        assert "安全检查" in text


class TestInternal:
    def test_modules_importable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RSI_HOME", str(tmp_path))
        d = Doctor()
        rs = d.check_internal()
        r = next(x for x in rs if "模块" in x.name)
        assert r.status in ("pass", "warn")

    def test_api_app_creatable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RSI_HOME", str(tmp_path))
        d = Doctor()
        rs = d.check_internal()
        r = next(x for x in rs if "API" in x.name)
        assert r.status == "pass"


class TestS11DoctorFixes:
    """B3 发现：默认 settings 路径应走 <RSI_HOME>/settings.yaml；空 url 合法."""

    def test_default_path_resolves_rsi_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RSI_HOME", str(tmp_path))
        monkeypatch.delenv("RSI_SETTINGS", raising=False)
        _write_settings(tmp_path, VALID_SETTINGS)
        d = Doctor()  # 不传 settings_path，不 chdir
        assert d.settings_path == tmp_path / "settings.yaml"
        rs = d.check_config()
        r = next(x for x in rs if x.name == "settings.yaml 存在")
        assert r.status == "pass"

    def test_rsi_settings_env_still_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RSI_HOME", str(tmp_path))
        monkeypatch.setenv("RSI_SETTINGS", str(tmp_path / "custom.yaml"))
        d = Doctor()
        assert d.settings_path == tmp_path / "custom.yaml"

    def test_empty_database_url_passes(self, tmp_path):
        _write_settings(tmp_path, VALID_SETTINGS.replace(
            "url: sqlite:///platform.db", "url: \"\""))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "database" in x.name.lower())
        assert r.status == "pass"


class TestS11DoctorRealEnv:
    """B3 真实环境：settings.yaml 内联 LLM key / 远端 tcp docker 主机."""

    def test_llm_key_in_settings_yaml_passes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
        _write_settings(tmp_path, VALID_SETTINGS.replace(
            "api_key_env: ANTHROPIC_API_KEY",
            'api_key: "sk-inline"'))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_config()
        r = next(x for x in rs if "LLM API Key" in x.name)
        assert r.status == "pass"

    def test_docker_local_down_with_remote_host_is_not_warn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Doctor, "_docker_version", staticmethod(lambda: None))
        _write_settings(tmp_path, VALID_SETTINGS.replace(
            "docker: local",
            'docker: "tcp://192.168.96.7:2375"'))
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_external()
        r = next(x for x in rs if x.name == "Docker")
        assert r.status == "pass"
        assert "远端" in r.message or "remote" in r.message.lower()

    def test_tcp_carrier_host_probed(self, tmp_path, monkeypatch):
        _write_settings(tmp_path, VALID_SETTINGS.replace(
            "docker: local",
            'docker: "tcp://192.168.96.7:2375"'))
        probed = {}
        monkeypatch.setattr(
            Doctor, "_tcp_reachable",
            lambda self, host, port: probed.update(host=host, port=port) or True,
        )
        d = Doctor(settings_path=tmp_path / "settings.yaml")
        rs = d.check_external()
        r = next(x for x in rs if x.name.startswith("Carrier 主机"))
        assert r.status == "pass"
        assert probed == {"host": "192.168.96.7", "port": 2375}
