"""D4: BatchRunner prep_mode routing — API path via NetworkPrepClient (ADR-27).

API mode must: skip the whole SandboxService lab lifecycle (build_lab /
wait_ready / snapshot / prepare / restore_task / destroy / health_check),
prepare+restore the network environment through NetworkPrepClient, and never
inject DEV_* credentials (cred=None into create_instance). CLI mode is the
untouched legacy pipeline.
"""

from __future__ import annotations

import pytest

from rsi.core.errors import EnvError
from rsi.core.models import (
    ApiEvalsetTask,
    ExpectedAnswer,
    NetworkEnvRef,
    RootCauseExpectation,
    TaskSpec,
)
from rsi.orchestrator.batch import api_task_to_task_record
from rsi.sandbox.netprep import PrepResult, RestoreResult
from tests.unit.test_batch_runner import OUT, PROMPT, Rig, _task, _verdict


class StubNetprep:
    """Records prepare/restore calls; optionally fails named envs."""

    def __init__(self, fail_envs: set[str] | None = None) -> None:
        self.prepares: list[tuple] = []
        self.restores: list[tuple] = []
        self._n = 0
        self.fail_envs = fail_envs or set()

    async def prepare(self, env_name: str, params: dict, session_id: str = ""):
        self.prepares.append((env_name, params, session_id))
        if env_name in self.fail_envs:
            raise EnvError(f"netprep unavailable: {env_name}")
        self._n += 1
        return PrepResult(env_id=f"env-{self._n}")

    async def restore(self, env_name: str, env_id: str, params: dict):
        self.restores.append((env_name, env_id, params))
        return RestoreResult()


class SandboxSpy:
    """Wraps a SandboxService and counts lifecycle-method calls."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: dict[str, int] = {}

    def _tick(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    async def build_lab(self, env):
        self._tick("build_lab")
        return await self._inner.build_lab(env)

    async def wait_ready(self, lab, timeout_s):
        self._tick("wait_ready")
        return await self._inner.wait_ready(lab, timeout_s)

    async def snapshot(self, lab, name):
        self._tick("snapshot")
        return await self._inner.snapshot(lab, name)

    async def prepare(self, lab, task_id, actions):
        self._tick("prepare")
        return await self._inner.prepare(lab, task_id, actions)

    async def restore_task(self, lab, applied):
        self._tick("restore_task")
        return await self._inner.restore_task(lab, applied)

    async def restore(self, lab, snap):
        self._tick("restore")
        return await self._inner.restore(lab, snap)

    async def reset(self, lab):
        self._tick("reset")
        return await self._inner.reset(lab)

    async def health_check(self, lab):
        self._tick("health_check")
        return await self._inner.health_check(lab)

    async def destroy(self, lab):
        self._tick("destroy")
        return await self._inner.destroy(lab)

    def snapshots(self, lab_id):
        self._tick("snapshots")
        return self._inner.snapshots(lab_id)

    def __getattr__(self, item):
        return getattr(self._inner, item)


def _api_task(n: int, env_name: str = "lab-bgp-3node") -> ApiEvalsetTask:
    return ApiEvalsetTask(
        task_id=f"fault/api-{n:02d}",
        title=f"api task {n}",
        network_env=NetworkEnvRef(
            name=env_name, params={"fault_type": "bgp_peer_down"}
        ),
        task=TaskSpec(prompt=PROMPT),
        expected=ExpectedAnswer(
            root_cause=RootCauseExpectation(expected="bfd timer mismatch")
        ),
    )


class TestApiMode:
    @pytest.mark.asyncio
    async def test_netprep_prepare_restore_per_task(self, tmp_path):
        rig = Rig(tmp_path)
        netprep = StubNetprep()
        rig.runner.netprep = netprep
        tasks = [api_task_to_task_record(_api_task(1)), api_task_to_task_record(_api_task(2))]
        stats = await rig.runner.run_batch(
            "batch-api-1", None, tasks, "cfg-v1", "sut-img", prep_mode="api"
        )
        assert stats.n == 2
        assert stats.missing == 0
        assert len(netprep.prepares) == 2
        assert len(netprep.restores) == 2
        # prepare carries the network env name/params and the batch id session
        assert netprep.prepares[0][0] == "lab-bgp-3node"
        assert netprep.prepares[0][1] == {"fault_type": "bgp_peer_down"}
        assert netprep.prepares[0][2] == "batch-api-1"
        # restore references the env id returned by prepare
        assert netprep.restores[0][:2] == ("lab-bgp-3node", "env-1")

    @pytest.mark.asyncio
    async def test_no_sandbox_lifecycle_calls(self, tmp_path):
        rig = Rig(tmp_path)
        spy = SandboxSpy(rig.sandbox)
        rig.runner.sandbox = spy
        rig.runner.netprep = StubNetprep()
        tasks = [api_task_to_task_record(_api_task(1))]
        await rig.runner.run_batch(
            "batch-api-2", None, tasks, "cfg-v1", "sut-img", prep_mode="api"
        )
        assert spy.calls.get("build_lab", 0) == 0
        assert spy.calls.get("wait_ready", 0) == 0
        assert spy.calls.get("snapshot", 0) == 0
        assert spy.calls.get("prepare", 0) == 0
        assert spy.calls.get("restore_task", 0) == 0
        assert spy.calls.get("restore", 0) == 0
        assert spy.calls.get("destroy", 0) == 0
        assert spy.calls.get("health_check", 0) == 0

    @pytest.mark.asyncio
    async def test_create_instance_gets_cred_none(self, tmp_path):
        rig = Rig(tmp_path)
        rig.runner.netprep = StubNetprep()
        seen: list = []
        orig = rig.carrier.create_instance

        def spy_create(**kw):
            seen.append(kw.get("cred"))
            return orig(**kw)

        rig.carrier.create_instance = spy_create
        tasks = [api_task_to_task_record(_api_task(1))]
        await rig.runner.run_batch(
            "batch-api-3", None, tasks, "cfg-v1", "sut-img", prep_mode="api"
        )
        assert seen == [None]

    @pytest.mark.asyncio
    async def test_netprep_failure_env_failed_batch_survives(self, tmp_path):
        rig = Rig(tmp_path)
        rig.runner.netprep = StubNetprep(fail_envs={"lab-broken"})
        tasks = [
            api_task_to_task_record(_api_task(1, env_name="lab-broken")),
            api_task_to_task_record(_api_task(2)),
        ]
        stats = await rig.runner.run_batch(
            "batch-api-4", None, tasks, "cfg-v1", "sut-img", prep_mode="api"
        )
        assert stats.n == 2
        assert stats.missing == 1  # failed task scored 0, not batch-fatal
        assert rig.runner.last_status == "finalized"
        # only the healthy task reached the judge
        assert len(rig.judge.calls) == 1

    @pytest.mark.asyncio
    async def test_api_mode_without_netprep_raises(self, tmp_path):
        rig = Rig(tmp_path)
        assert rig.runner.netprep is None
        with pytest.raises(EnvError):
            await rig.runner.run_batch(
                "batch-api-5", None, [api_task_to_task_record(_api_task(1))],
                "cfg-v1", "sut-img", prep_mode="api",
            )


class TestCliModeRegression:
    @pytest.mark.asyncio
    async def test_cli_pipeline_unchanged(self, tmp_path):
        rig = Rig(tmp_path)
        spy = SandboxSpy(rig.sandbox)
        rig.runner.sandbox = spy
        netprep = StubNetprep()
        rig.runner.netprep = netprep
        from tests.unit.test_batch_runner import _env

        stats = await rig.runner.run_batch(
            "batch-1", _env(), [_task(1)], "cfg-v1", "sut-img"
        )
        assert stats.n == 1 and stats.missing == 0
        # CLI lab lifecycle fully exercised
        assert spy.calls.get("build_lab", 0) == 1
        assert spy.calls.get("wait_ready", 0) == 1
        # F6/R5：snap0 由 build_lab 内部登记——BatchRunner 经 snapshots()
        # 列表取基线，不再显式补拍（否则每 lab 两条 snap0 记录）
        assert spy.calls.get("snapshots", 0) >= 1
        assert spy.calls.get("prepare", 0) == 1
        assert spy.calls.get("restore_task", 0) >= 1
        assert spy.calls.get("destroy", 0) == 1
        # netprep untouched in CLI mode
        assert netprep.prepares == [] and netprep.restores == []


class TestAdapter:
    def test_api_task_to_task_record_passthrough(self):
        at = _api_task(7)
        tr = api_task_to_task_record(at)
        assert tr.task_id == at.task_id
        assert tr.title == at.title
        assert tr.task.prompt == at.task.prompt
        assert tr.expected == at.expected
        assert tr.prepare == []  # env prep goes through netprep, not CLI
        assert tr.network_env == at.network_env  # preserved for run_task

    def test_defaults_and_output_untouched(self):
        # sanity: the fake carrier's canned response still parses (OUT reused)
        assert OUT.strip().startswith("agent output")


class TestResolverApiPath:
    def test_api_batch_resolves_via_fetcher(self):
        from rsi.orchestrator.resolver import build_batch_resolver

        class StubFetcher:
            def __init__(self):
                self.names: list[str] = []

            def fetch_sync(self, name):
                self.names.append(name)
                return [_api_task(1), _api_task(2)]

        fetcher = StubFetcher()
        resolve = build_batch_resolver(
            suts_store={"sut-1": {"image_ref": "ipops:v9"}},
            evalset_fetcher=fetcher,
        )
        env, tasks, cfg, img = resolve({
            "prep_mode": "api", "api_evalset_name": "lab-weekly",
            "sut_id": "sut-1",
        })
        assert env is None
        assert cfg == ""
        assert img == "ipops:v9"
        assert [t.task_id for t in tasks] == ["fault/api-01", "fault/api-02"]
        assert all(t.prepare == [] for t in tasks)
        assert fetcher.names == ["lab-weekly"]

    def test_api_batch_without_fetcher_raises(self):
        from rsi.core.errors import ConfigError
        from rsi.orchestrator.resolver import build_batch_resolver

        resolve = build_batch_resolver()
        with pytest.raises(ConfigError):
            resolve({"prep_mode": "api", "api_evalset_name": "x"})

    def test_api_batch_without_name_raises(self):
        from rsi.core.errors import ValidationError
        from rsi.orchestrator.resolver import build_batch_resolver

        resolve = build_batch_resolver(evalset_fetcher=object())
        with pytest.raises(ValidationError):
            resolve({"prep_mode": "api"})
