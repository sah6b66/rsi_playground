"""Unit tests: OpenCodeAdapter (P0 carrier, M2) + DEV_* cred env (M3). Offline only."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from rsi.carrier.adapters import OpenCodeAdapter
from rsi.carrier.adapters.fake import FakeCarrier
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService, build_dev_env
from rsi.core.models import LabHandle, SshAccess


def _lab(lab_id: str = "L1") -> LabHandle:
    return LabHandle(
        lab_id=lab_id,
        host="main",
        backend="fake",
        endpoints={"R1": SshAccess(ip="10.0.0.1", username="admin")},
    )


class TestBuildImage:
    def test_build_image_returns_base(self, tmp_path) -> None:
        adapter = OpenCodeAdapter()
        ref = adapter.build_image("my-sut", tmp_path)
        assert ref == "rsi-opencode-runtime:1.0"


class TestStart:
    def test_start_writes_opencode_json_with_prompt_logger(self, tmp_path) -> None:
        cfg = tmp_path / "sut"
        cfg.mkdir()
        (cfg / "model.json").write_text(
            json.dumps({"provider": "openai", "model": "gpt-5"}), encoding="utf-8"
        )
        adapter = OpenCodeAdapter()
        cid = adapter.start("rsi-opencode-runtime:1.0", cfg, "restricted", None)
        assert cid.startswith("oc-")
        oc = json.loads((cfg / "opencode.json").read_text(encoding="utf-8"))
        plugin = oc["plugins"]["opencode-prompt-logger"]
        assert plugin["file"].startswith("/trace/wiretap/")
        assert plugin["rotateBytes"] is False
        assert plugin["http"] is True
        assert oc["provider"] == "openai"
        assert oc["model"] == "gpt-5"

    def test_start_model_json_missing_uses_defaults(self, tmp_path) -> None:
        cfg = tmp_path / "sut"
        cfg.mkdir()
        adapter = OpenCodeAdapter()
        adapter.start("rsi-opencode-runtime:1.0", cfg, "restricted", None)
        oc = json.loads((cfg / "opencode.json").read_text(encoding="utf-8"))
        assert oc["provider"] == "anthropic"
        assert oc["model"] == "claude-sonnet-4-6"

    def test_start_records_dev_env(self, tmp_path) -> None:
        cfg = tmp_path / "sut"
        cfg.mkdir()
        adapter = OpenCodeAdapter()
        cred = {
            "api_key": "sk-x",
            "devices": {"R1": {"user": "u", "pass": "p", "host": "h", "port": 22}},
        }
        cid = adapter.start("rsi-opencode-runtime:1.0", cfg, "restricted", cred)
        env = adapter.env_for[cid]
        assert env["DEV_R1_USER"] == "u"
        assert env["DEV_R1_PASS"] == "p"
        assert env["DEV_R1_HOST"] == "h"
        assert env["DEV_R1_PORT"] == "22"
        assert env["OPENCODE_API_KEY"] == "sk-x"


class TestSubmitAndTrace:
    def test_submit_task_cmd_shape(self, tmp_path) -> None:
        adapter = OpenCodeAdapter()
        cfg = tmp_path
        cid = adapter.start("rsi-opencode-runtime:1.0", cfg, "restricted", None)
        inst = SimpleNamespace(run_id="run-42", instance_id=cid)
        h1 = adapter.submit_task(inst, "do the thing")
        assert "--format" in h1.cmd and "json" in h1.cmd
        assert "--print-logs" in h1.cmd
        assert "--session" in h1.cmd
        assert h1.cmd[h1.cmd.index("--session") + 1] == "run-42"
        assert h1.stdout is None
        # .prompt attribute object also works
        h2 = adapter.submit_task(inst, SimpleNamespace(prompt="another"))
        assert h2.cmd[-1] == "another"

    def test_collect_result_offline(self, tmp_path) -> None:
        adapter = OpenCodeAdapter()
        cfg = tmp_path
        cid = adapter.start("rsi-opencode-runtime:1.0", cfg, "restricted", None)
        handle = adapter.submit_task(
            SimpleNamespace(run_id="r", instance_id=cid), "t"
        )
        assert adapter.collect_result(handle, 1.0) == ""
        handle.stdout = "done"
        assert adapter.collect_result(handle, 1.0) == "done"

    def test_trace_channels_per_run(self, tmp_path) -> None:
        adapter = OpenCodeAdapter()
        inst = SimpleNamespace(run_id="run-7", instance_id="oc-x")
        ch = adapter.trace_channels(inst)
        assert ch.kind == "ndjson"
        assert ch.paths[0] == "/trace/wiretap/run-7.ndjson"
        assert "/trace/events.ndjson" in ch.paths


class TestBuildDevEnv:
    def test_build_dev_env_devices(self) -> None:
        cred = {"devices": {"R1": {"user": "u", "pass": "p", "host": "h", "port": 22}}}
        env = build_dev_env(cred)
        assert env == {
            "DEV_R1_USER": "u",
            "DEV_R1_PASS": "p",
            "DEV_R1_HOST": "h",
            "DEV_R1_PORT": "22",
        }
        env2 = build_dev_env({"api_key": "sk-1"})
        assert env2 == {"OPENCODE_API_KEY": "sk-1"}
        assert build_dev_env(None) == {}
        assert build_dev_env("junk") == {}
        assert build_dev_env(42) == {}
        # partial device keys: missing keys skipped
        env3 = build_dev_env({"devices": {"R2": {"user": "u2"}}})
        assert env3 == {"DEV_R2_USER": "u2"}


class TestServiceEnvInjection:
    def test_create_instance_injects_env(self, tmp_path) -> None:
        svc = HostService(
            host_pool=HostPool(),
            net_policy=NetPolicy(),
            adapters={"opencode": OpenCodeAdapter(), "fake": FakeCarrier()},
        )
        config_dir = tmp_path / "sut-config"
        config_dir.mkdir(exist_ok=True)
        handle = svc.create_instance(
            image_ref="my-sut",
            config_version_id=str(config_dir),
            lab=_lab(),
            permission="restricted",
            instrument=False,
            host="main",
            cred={
                "api_key": "sk-z",
                "devices": {"R1": {"user": "u", "pass": "p", "host": "h", "port": 22}},
            },
            run_id="run-9",
        )
        assert handle.env["DEV_R1_USER"] == "u"
        assert handle.env["DEV_R1_PASS"] == "p"
        assert handle.env["DEV_R1_HOST"] == "h"
        assert handle.env["DEV_R1_PORT"] == "22"
        assert handle.env["OPENCODE_API_KEY"] == "sk-z"
        assert handle.env["RSI_RUN_ID"] == "run-9"
