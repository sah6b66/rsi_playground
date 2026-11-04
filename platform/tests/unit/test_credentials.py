import base64

import pytest

from rsi.core.errors import RsiError
from rsi.core.models import DeviceSpec, EnvSpec, ExternalDep, LinkSpec, SshAccess, Topology
from rsi.security.credentials import (
    CredentialResolver,
    CredentialStore,
    ResolvedCreds,
    SecretFingerprint,
)

MASTER_KEY = base64.b64encode(b"test-key-32-bytes-1234567890!!!!").decode()


class TestCredentialStore:
    def test_roundtrip(self):
        store = CredentialStore(master_key=MASTER_KEY)
        cred_id = store.put("my-cred", "device_ssh", "secret123")
        assert cred_id.startswith("cred-")
        assert store.resolve("my-cred") == "secret123"

    def test_encrypted_at_rest(self):
        store = CredentialStore(master_key=MASTER_KEY)
        store.put("my-cred", "device_ssh", "secret123")
        token = store._by_name["my-cred"][2]
        assert "secret123" not in token

    def test_duplicate_name_rejected(self):
        store = CredentialStore(master_key=MASTER_KEY)
        store.put("my-cred", "device_ssh", "a")
        with pytest.raises(RsiError):
            store.put("my-cred", "device_ssh", "b")

    def test_unknown_name(self):
        store = CredentialStore(master_key=MASTER_KEY)
        with pytest.raises(RsiError):
            store.resolve("nope")

    def test_rotate(self):
        store = CredentialStore(master_key=MASTER_KEY)
        store.put("my-cred", "device_ssh", "old")
        store.rotate(store.resolve_id("my-cred"), "new")
        assert store.resolve("my-cred") == "new"

    def test_fingerprint(self):
        store = CredentialStore(master_key=MASTER_KEY)
        store.put("my-cred", "device_ssh", "secret123")
        fp = store.fingerprints_of(store.resolve_id("my-cred"))
        assert isinstance(fp, SecretFingerprint)
        assert len(fp.sha8) == 8
        assert len(fp.patterns) == 2  # raw + base64
        assert "secret123" in fp.patterns
        assert base64.b64encode(b"secret123").decode() in fp.patterns

    def test_bad_master_key(self):
        short = base64.b64encode(b"too-short").decode()
        with pytest.raises(RsiError):
            CredentialStore(master_key=short)


def _env(**kw) -> EnvSpec:
    defaults = dict(
        name="t",
        backend="fake",
        devices=[DeviceSpec(name="R1", type="vrp", config="c")],
        topology=Topology(links=[LinkSpec(endpoints=("R1:GE0/0/0", "R1:GE0/0/1"))]),
        ssh_access={"R1": SshAccess(ip="10.0.0.1", username="admin", password="pw")},
    )
    defaults.update(kw)
    return EnvSpec(**defaults)


class TestCredentialResolver:
    def test_resolve_env_plain(self):
        creds = CredentialResolver(store=None).resolve_env(_env(), permission="read-only")
        assert isinstance(creds, ResolvedCreds)
        assert creds.device_env["R1"]["DEV_R1_HOST"] == "10.0.0.1"
        assert creds.device_env["R1"]["DEV_R1_PORT"] == "22"
        assert creds.device_env["R1"]["DEV_R1_USER"] == "admin"
        assert creds.device_env["R1"]["DEV_R1_PASS"] == "pw"
        assert "DEV_RW" not in creds.device_env["R1"]

    def test_read_write_flag(self):
        creds = CredentialResolver(store=None).resolve_env(_env(), permission="read-write")
        assert creds.device_env["R1"]["DEV_RW"] == "1"

    def test_password_ref(self):
        store = CredentialStore(master_key=MASTER_KEY)
        store.put("r1-pw", "device_ssh", "ref-secret")
        env = _env(
            ssh_access={"R1": SshAccess(ip="10.0.0.1", username="admin", password_ref="r1-pw")}
        )
        creds = CredentialResolver(store=store).resolve_env(env, permission="read-only")
        assert creds.device_env["R1"]["DEV_R1_PASS"] == "ref-secret"

    def test_nms_token(self):
        store = CredentialStore(master_key=MASTER_KEY)
        store.put("nms", "nms_api", "tok-1")
        env = _env(external_deps=[ExternalDep(kind="nms", base_url="http://nms", auth_ref="nms")])
        creds = CredentialResolver(store=store).resolve_env(env, permission="read-only")
        assert creds.nms_token == "tok-1"

    def test_fingerprints_collected(self):
        creds = CredentialResolver(store=None).resolve_env(_env(), permission="read-only")
        assert len(creds.fingerprints) == 1
        assert "pw" in creds.fingerprints[0].patterns
