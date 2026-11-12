"""OpenCodeAdapter real 模式（A4）：docker 生命周期离线单测."""
from pathlib import Path

from rsi.carrier.adapters.opencode import OpenCodeAdapter
from rsi.carrier.service import InstanceHandle


class FakeContainers:
    def __init__(self, client):
        self.client = client

    def create(self, image, **kw):
        kw["image"] = image
        c = FakeContainer(self.client, image, kw)
        self.client.created.append(c)
        return c

    def get(self, cid):
        for c in self.client.created:
            if c.id == cid:
                return c
        raise KeyError(cid)


class FakeContainer:
    _seq = 0

    def __init__(self, client, image, kw):
        FakeContainer._seq += 1
        self.client = client
        self.id = f"cid{FakeContainer._seq:04d}"
        self.image = image
        self.kw = kw
        self.started = False
        self.removed = False
        self.execs: list[dict] = []

    def start(self):
        self.started = True

    def stop(self, **kw):
        self.started = False

    def remove(self):
        self.removed = True

    def exec_run(self, cmd, environment=None, workdir=None, demux=False):
        self.execs.append({"cmd": cmd, "env": environment or {}})
        stdout = b'<FINAL_JSON>{"task_complete": true}</FINAL_JSON>'
        return (0, (stdout, b"")) if demux else (0, stdout)


class FakeDockerClient:
    def __init__(self):
        self.created: list[FakeContainer] = []
        self.containers = FakeContainers(self)


class FakeHostPool:
    def __init__(self, client):
        self._c = client

    def client(self, host):
        return self._c


def _adapter(tmp_path):
    return OpenCodeAdapter(
        real=True,
        host_pool=FakeHostPool(FakeDockerClient()),
        trace_root=tmp_path / "trace",
        extra_env={"ANTHROPIC_AUTH_TOKEN": "tk"},
    )


def _instance(adapter, cid):
    return InstanceHandle(
        instance_id="inst-1", sut_id="img", run_id="run-1", host="main",
        container_id=cid, config_dir=Path("."), carrier="opencode",
    )


def test_real_start_creates_container(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.start_instance(
        "rsi/opencode-netops:1.0", tmp_path, "read-only",
        {"devices": {"Spine-01": {"user": "u", "pass": "p", "host": "h", "port": 22}}},
        "main", "run-1",
    )
    (c,) = adapter.host_pool._c.created
    assert c.started and c.kw["image"] == "rsi/opencode-netops:1.0"
    mounts = {m["Target"]: m["Source"] for m in c.kw["mounts"]}
    assert mounts["/agent-config"] == str(tmp_path.resolve())
    assert mounts["/trace"].replace("\\", "/").endswith("trace/run-1")
    assert c.kw["environment"]["DEV_SPINE_01_USER"] == "u"
    assert c.kw["environment"]["ANTHROPIC_AUTH_TOKEN"] == "tk"


def test_real_collect_runs_exec_and_fills_handle(tmp_path):
    adapter = _adapter(tmp_path)
    cid = adapter.start_instance("img", tmp_path, "read-write", None, "main", "run-1")
    inst = _instance(adapter, cid)
    handle = adapter.submit_task(inst, "检查设备。以 <FINAL_JSON>{...}</FINAL_JSON> 输出。")
    out = adapter.collect_result(handle, 30)
    assert out.endswith("</FINAL_JSON>")
    assert handle.exit_code == 0 and handle.stdout == out
    (c,) = adapter.host_pool._c.created
    assert c.execs[0]["cmd"][0] == "opencode"
    assert c.execs[0]["env"]["OPENCODE_WIRETAP_FILE"] == "/trace/wiretap/run-1.ndjson"


def test_real_destroy_removes_container(tmp_path):
    adapter = _adapter(tmp_path)
    cid = adapter.start_instance("img", tmp_path, "read-only", None, "main", "run-1")
    adapter.destroy_instance(_instance(adapter, cid))
    (c,) = adapter.host_pool._c.created
    assert c.removed


def test_offline_default_unchanged():
    adapter = OpenCodeAdapter()
    assert adapter.start_instance("img", Path("."), "ro", None, "main", "r") is None
    assert adapter.destroy_instance(_instance(adapter, "x")) is None
