"""D1 (ADR-27): ApiEvalsetTask / NetworkEnvRef models + evalset/netprep settings.

Also asserts the config_apis removal from EnvSpec (per-evalset ConfigApi
superseded by the system-level NetworkPrepClient, ADR-27).
"""

from pathlib import Path

from rsi.core.models import (
    ApiEvalsetTask,
    EnvSpec,
    ExpectedAnswer,
    NetworkEnvRef,
    RootCauseExpectation,
    TaskSpec,
)
from rsi.settings import Settings


def make_env(**over):
    from rsi.core.models import DeviceSpec, SshAccess, Topology

    kw = dict(
        name="test-env",
        backend="fake",
        devices=[DeviceSpec(name="R1", type="vrp", config="configs/R1.cfg")],
        topology=Topology(links=[]),
        ssh_access={"R1": SshAccess(ip="10.0.0.1", username="a", password="p")},
    )
    kw.update(over)
    return EnvSpec(**kw)


class TestNetworkEnvRef:
    def test_construct(self):
        ref = NetworkEnvRef(
            name="lab-bgp-3node",
            params={"topology": "triangle", "fault_type": "bgp_peer_down"},
        )
        assert ref.name == "lab-bgp-3node"
        assert ref.params["fault_type"] == "bgp_peer_down"

    def test_params_default_empty(self):
        assert NetworkEnvRef(name="x").params == {}


class TestApiEvalsetTask:
    def test_construct(self):
        t = ApiEvalsetTask(
            task_id="fault/bgp-down-01",
            title="BGP session down",
            network_env=NetworkEnvRef(name="lab-bgp-3node", params={"topology": "triangle"}),
            task=TaskSpec(prompt="fix it, FINAL_JSON"),
            expected=ExpectedAnswer(
                root_cause=RootCauseExpectation(expected="bad peer config")
            ),
        )
        assert t.task_id == "fault/bgp-down-01"
        assert t.network_env.name == "lab-bgp-3node"
        assert t.expected.root_cause.expected == "bad peer config"


class TestSettingsSections:
    def test_evalset_api_default_empty(self):
        assert Settings().evalset_api.url == ""

    def test_network_prep_api_defaults(self):
        s = Settings()
        assert s.network_prep_api.url == ""
        assert s.network_prep_api.timeout_s == 120

    def test_envspec_no_config_apis(self):
        env = make_env()
        assert not hasattr(env, "config_apis")
        assert "config_apis" not in EnvSpec.model_fields

    def test_yaml_evalset_api_url(self, tmp_path: Path, monkeypatch):
        (tmp_path / "settings.yaml").write_text(
            "evalset_api:\n  url: http://10.0.0.1:9090\n", encoding="utf-8"
        )
        monkeypatch.setenv("RSI_SETTINGS", str(tmp_path / "settings.yaml"))
        assert Settings().evalset_api.url == "http://10.0.0.1:9090"
