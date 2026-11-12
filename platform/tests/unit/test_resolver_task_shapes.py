"""resolver._parse_tasks 双形状解析（A1：导入评测集跑批链路修复）."""

import pytest

from rsi.core.errors import ValidationError
from rsi.orchestrator.resolver import _parse_tasks

TASK_RECORD_ROW = {
    "task_id": "fault/leaf-uplink-down-01",
    "title": "上联口误关",
    "prepare": [
        {
            "action": "exec_cli",
            "entries": [{"device": "Leaf-01", "cli_lines": ["interface GE1/0/2", "shutdown"]}],
        }
    ],
    "task": {"prompt": "定位故障。以 <FINAL_JSON>{...}</FINAL_JSON> 输出。"},
    "expected": {"root_cause": {"expected": "上联口被 shutdown"}},
    "metrics": ["root_cause_accuracy"],
}
FLAT_ROW = {"id": "inspection/x-01", "prompt": "巡检", "expected": {}}


def test_taskrecord_shape_parsed():
    records = _parse_tasks([TASK_RECORD_ROW], "es-1")
    assert records[0].task_id == "fault/leaf-uplink-down-01"
    assert "FINAL_JSON" in records[0].task.prompt
    assert records[0].prepare[0].entries[0].device == "Leaf-01"
    assert records[0].expected.root_cause.expected == "上联口被 shutdown"
    assert [m.name for m in records[0].metrics] == ["root_cause_accuracy"]


def test_taskrecord_shape_context_and_constraints():
    row = dict(TASK_RECORD_ROW)
    row["task"] = {
        "prompt": "p",
        "context": {"topology": "spine-leaf"},
        "tool_constraints": ["只允许 SSH"],
    }
    records = _parse_tasks([row], "es-1")
    assert records[0].task.context == {"topology": "spine-leaf"}
    assert records[0].task.tool_constraints == ["只允许 SSH"]


def test_flat_shape_still_parsed():
    records = _parse_tasks([FLAT_ROW], "es-1")
    assert records[0].task_id == "inspection/x-01"
    assert records[0].task.prompt == "巡检"
    assert records[0].metrics == []


def test_bad_row_raises_with_line():
    with pytest.raises(ValidationError) as ei:
        _parse_tasks([{"no_id": 1}], "es-1")
    assert "line 1" in str(ei.value)


def test_taskrecord_shape_via_resolver(tmp_path):
    """:import 存储形状 → resolver 整链路（env+tasks）能出 TaskRecord."""
    import yaml

    from rsi.orchestrator.resolver import build_batch_resolver

    env_yaml = yaml.safe_dump(
        {
            "name": "env-x",
            "backend": "ensp-pro",
            "devices": [{"name": "Leaf-01", "type": "vrp", "config": "configs/L.cfg"}],
            "topology": {"links": []},
            "ssh_access": {
                "Leaf-01": {"ip": "192.168.96.21", "port": 22, "username": "u", "password": "p"}
            },
        }
    )
    evalsets = {"es-9": {"id": "es-9", "env_yaml": env_yaml, "tasks": [TASK_RECORD_ROW]}}
    suts = {"sut-9": {"image_ref": "rsi/opencode-netops:1.0"}}
    resolver = build_batch_resolver(
        session_factory=None, gitstore=None, evalsets_store=evalsets, suts_store=suts
    )
    env, tasks, cfg, image = resolver(
        {"evalset_id": "es-9", "sut_id": "sut-9", "params": {}}
    )
    assert env.name == "env-x" and env.backend == "ensp-pro"
    assert tasks[0].task_id == "fault/leaf-uplink-down-01"
    assert cfg == "" and image == "rsi/opencode-netops:1.0"


class _FakeGitstore:
    """export() 造出整棵树（含 suts/<sut>/ 子目录）——真实 GitStore 形状."""

    def __init__(self, sut_ids=("sut-abc",)):
        self.sut_ids = sut_ids

    def export(self, version_id, dest):
        from pathlib import Path

        dest = Path(dest)
        for sid in self.sut_ids:
            d = dest / "suts" / sid
            d.mkdir(parents=True, exist_ok=True)
            (d / "model.json").write_text("{}", encoding="utf-8")
        other = dest / "README.md"
        other.write_text("x", encoding="utf-8")


def test_resolve_config_dir_points_at_sut_subtree(tmp_path):
    """E2 实测：git export 是整棵树，config 根必须是 suts/<sut_id>/ 子目录
    （否则 adapter 读不到 model.json 回落 anthropic 默认模型）。"""
    from rsi.orchestrator.resolver import _resolve_config_dir

    gs = _FakeGitstore(sut_ids=("sut-abc", "sut-xyz"))
    cfg = _resolve_config_dir(gs, "deadbeef" * 5, sut_id="sut-abc")
    assert cfg.replace("\\", "/").endswith("suts/sut-abc"), cfg
    from pathlib import Path

    assert (Path(cfg) / "model.json").is_file()


def test_resolve_config_dir_without_sut_id_keeps_root(tmp_path):
    from rsi.orchestrator.resolver import _resolve_config_dir

    gs = _FakeGitstore()
    cfg = _resolve_config_dir(gs, "deadbeef" * 5)
    from pathlib import Path

    assert (Path(cfg) / "suts" / "sut-abc").is_dir()  # 未指定 sut：整树根
