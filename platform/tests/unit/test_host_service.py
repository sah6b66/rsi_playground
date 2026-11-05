"""Unit tests: HostService with FakeCarrier (Task 6, P0 offline)."""

from __future__ import annotations

from rsi.carrier.adapters.fake import FakeCarrier
from rsi.carrier.hosts import HostPool
from rsi.carrier.network import NetPolicy
from rsi.carrier.service import HostService, InstanceHandle
from rsi.core.models import LabHandle, SshAccess, TaskSpec


def _lab(lab_id: str = "L1") -> LabHandle:
    return LabHandle(
        lab_id=lab_id,
        host="main",
        backend="fake",
        endpoints={"R1": SshAccess(ip="10.0.0.1", username="admin")},
    )


def _service(tmp_path, responses: dict[str, str] | None = None) -> HostService:
    svc = HostService(
        host_pool=HostPool(),
        net_policy=NetPolicy(),
        adapters={"fake": FakeCarrier(responses or {})},
    )
    svc._tmp_path = tmp_path  # for placeholder config paths in tests
    return svc


def _make_instance(svc: HostService, tmp_path, run_id: str = "run-1") -> InstanceHandle:
    config_dir = tmp_path / "sut-config"
    config_dir.mkdir(exist_ok=True)
    return svc.create_instance(
        image_ref="my-sut",
        config_version_id=str(config_dir),
        lab=_lab(),
        permission="restricted",
        instrument=True,
        host="main",
        cred=None,
        run_id=run_id,
    )


GOOD = '{"task_complete": true, "root_cause": "bfd timer mismatch", "confidence": 0.9}'
PROMPT = "diagnose the bgp session flap and reply with FINAL_JSON"
OUT = f"agent output...\n<FINAL_JSON>{GOOD}</FINAL_JSON>\n"


class TestLifecycle:
    def test_create_submit_collect_destroy(self, tmp_path) -> None:
        svc = _service(tmp_path, {PROMPT: OUT})
        inst = _make_instance(svc, tmp_path)

        assert inst.container_id == inst.instance_id
        assert inst.status == "running"
        assert inst.run_id == "run-1"

        handle = svc.submit_task(inst, TaskSpec(prompt=PROMPT))
        assert handle.instance_id == inst.instance_id
        assert PROMPT in handle.cmd

        result = svc.collect(handle)
        assert result.task_complete is True
        assert result.root_cause == "bfd timer mismatch"
        assert result.parse_error is False
        assert result.instance_crashed is False

        svc.destroy(inst)
        assert svc._instances == {}
        assert not svc.net_policy.is_attached(inst.container_id, "lab-L1")

    def test_instrument_builds_overlay(self, tmp_path) -> None:
        svc = _service(tmp_path)
        inst = _make_instance(svc, tmp_path)
        assert "overlay-" in str(inst.config_dir)
        assert inst.config_dir.exists()
        svc.destroy(inst)

    def test_no_instrument_uses_config_dir(self, tmp_path) -> None:
        svc = _service(tmp_path)
        config_dir = tmp_path / "sut-plain"
        config_dir.mkdir(exist_ok=True)
        inst = svc.create_instance(
            image_ref="sut",
            config_version_id=str(config_dir),
            lab=_lab(),
            permission="restricted",
            instrument=False,
            host="main",
            cred=None,
            run_id="run-2",
        )
        assert str(inst.config_dir) == str(config_dir)
        svc.destroy(inst)


class TestResultExtraction:
    def test_parse_error_on_invalid_output(self, tmp_path) -> None:
        svc = _service(tmp_path, {PROMPT: "total garbage, no json here"})
        inst = _make_instance(svc, tmp_path)
        handle = svc.submit_task(inst, TaskSpec(prompt=PROMPT))
        result = svc.collect(handle)
        assert result.task_complete is False
        assert result.parse_error is True
        svc.destroy(inst)

    def test_instance_crashed_when_adapter_raises(self, tmp_path) -> None:
        class ExplodingCarrier(FakeCarrier):
            def collect_result(self, handle, timeout_s: float) -> str:
                raise RuntimeError("container exited non-zero")

        svc = HostService(
            host_pool=HostPool(),
            net_policy=NetPolicy(),
            adapters={"fake": ExplodingCarrier({})},
        )
        inst = _make_instance(svc, tmp_path)
        handle = svc.submit_task(inst, TaskSpec(prompt=PROMPT))
        result = svc.collect(handle)
        assert result.task_complete is False
        assert result.instance_crashed is True
        svc.destroy(inst)


class TestNetworkIsolation:
    def test_attached_to_lab_and_trace_not_mgmt(self, tmp_path) -> None:
        svc = _service(tmp_path)
        inst = _make_instance(svc, tmp_path)
        assert svc.net_policy.is_attached(inst.container_id, "lab-L1")
        assert svc.net_policy.is_attached(inst.container_id, "trace")
        assert not svc.net_policy.is_attached(inst.container_id, "mgmt")
        svc.destroy(inst)

    def test_labs_isolated(self, tmp_path) -> None:
        svc = _service(tmp_path)
        a = svc.create_instance("sut", "cv", _lab("A"), "restricted", False, "main", None, "r1")
        b = svc.create_instance("sut", "cv", _lab("B"), "restricted", False, "main", None, "r2")
        assert svc.net_policy.is_attached(a.container_id, "lab-A")
        assert not svc.net_policy.is_attached(a.container_id, "lab-B")
        assert svc.net_policy.is_attached(b.container_id, "lab-B")
        assert not svc.net_policy.is_attached(b.container_id, "lab-A")
        # both share trace
        assert svc.net_policy.is_attached(a.container_id, "trace")
        assert svc.net_policy.is_attached(b.container_id, "trace")


class TestTracking:
    def test_multiple_instances_independent(self, tmp_path) -> None:
        svc = _service(tmp_path, {PROMPT: OUT})
        i1 = _make_instance(svc, tmp_path, run_id="run-a")
        i2 = _make_instance(svc, tmp_path, run_id="run-b")
        assert i1.instance_id != i2.instance_id

        h1 = svc.submit_task(i1, TaskSpec(prompt=PROMPT))
        h2 = svc.submit_task(i2, TaskSpec(prompt=PROMPT))
        assert h1.instance_id == i1.instance_id
        assert h2.instance_id == i2.instance_id

        svc.destroy(i1)
        assert i2.instance_id in svc._instances
        assert i1.instance_id not in svc._instances
        assert svc.net_policy.is_attached(i2.container_id, "lab-L1")
        svc.destroy(i2)

    def test_destroy_stale_cleans_host(self, tmp_path) -> None:
        svc = _service(tmp_path)
        _make_instance(svc, tmp_path)
        _make_instance(svc, tmp_path)
        n = svc.destroy_stale("main")
        assert n == 2
        assert svc._instances == {}
        assert svc.destroy_stale("main") == 0


class TestCrossHost:
    """HostService over a configured multi-host pool (R1-R3)."""

    @staticmethod
    def _multi_service(tmp_path, responses=None):
        from unittest.mock import MagicMock

        from rsi.carrier.scheduler import InstanceScheduler

        pool = HostPool(
            {
                "main": {"docker": "local"},
                "remote-1": {"docker": "ssh://mock@host1"},
            }
        )
        pool.client = MagicMock(return_value=MagicMock(ping=lambda: True))  # type: ignore[method-assign]
        svc = HostService(
            host_pool=pool,
            net_policy=NetPolicy(host_pool=pool),
            scheduler=InstanceScheduler(pool),
            adapters={"fake": FakeCarrier(responses or {})},
        )
        svc._tmp_path = tmp_path
        return svc

    def test_create_auto_selects_host(self, tmp_path) -> None:
        svc = self._multi_service(tmp_path)
        inst = svc.create_instance(
            "sut", "cv", _lab(), "restricted", False, None, None, "r1"
        )
        assert inst.host == "main"
        assert svc.host_pool._instance_counts["main"] == 1

    def test_create_auto_spreads_to_least_loaded(self, tmp_path) -> None:
        svc = self._multi_service(tmp_path)
        svc.host_pool.increment("main")
        inst = svc.create_instance(
            "sut", "cv", _lab(), "restricted", False, None, None, "r1"
        )
        assert inst.host == "remote-1"

    def test_create_explicit_host(self, tmp_path) -> None:
        svc = self._multi_service(tmp_path)
        inst = svc.create_instance(
            "sut", "cv", _lab(), "restricted", False, "remote-1", None, "r1"
        )
        assert inst.host == "remote-1"
        assert svc.net_policy.is_attached_on("remote-1", inst.container_id, "lab-L1")
        assert svc.net_policy.is_attached_on("remote-1", inst.container_id, "trace")
        assert not svc.net_policy.is_attached_on("main", inst.container_id, "lab-L1")

    def test_destroy_decrements_count(self, tmp_path) -> None:
        svc = self._multi_service(tmp_path)
        inst = svc.create_instance(
            "sut", "cv", _lab(), "restricted", False, None, None, "r1"
        )
        assert svc.host_pool._instance_counts["main"] == 1
        svc.destroy(inst)
        assert svc.host_pool._instance_counts["main"] == 0
        assert not svc.net_policy.is_attached(inst.container_id, "lab-L1")

    def test_instances_tracked_independently_per_host(self, tmp_path) -> None:
        svc = self._multi_service(tmp_path)
        a = svc.create_instance("sut", "cv", _lab("A"), "restricted", False, "main", None, "r1")
        b = svc.create_instance(
            "sut", "cv", _lab("B"), "restricted", False, "remote-1", None, "r2"
        )
        assert {a.host, b.host} == {"main", "remote-1"}
        assert svc.host_pool._instance_counts == {"main": 1, "remote-1": 1}
        # same lab id on different hosts = separate per-host networks
        svc.create_instance("sut", "cv", _lab("A"), "restricted", False, "remote-1", None, "r3")
        assert svc.host_pool._instance_counts["remote-1"] == 2
        assert svc.destroy_stale("remote-1") == 2
        assert svc.host_pool._instance_counts["remote-1"] == 0
        assert svc.host_pool._instance_counts["main"] == 1
