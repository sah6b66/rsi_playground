"""F4/W10：user.login 审计节流（S11 实测 4 万条灌水）+ init 清理."""
from rsi.security.audit import AuditLog


def _entry(actor: str, action: str = "user.login") -> dict:
    return {
        "actor": actor,
        "action": action,
        "object_type": "session",
        "object_id": "x",
        "detail": {"ip": "127.0.0.1"},
    }


def test_login_throttled_per_actor_window():
    alog = AuditLog()
    alog.record(**_entry("admin"))
    alog.record(**_entry("admin"))   # 窗口内重复 → 丢弃
    alog.record(**_entry("admin"))
    alog.record(**_entry("bob"))     # 不同 actor → 记录
    assert sum(1 for e in alog.entries if e.action == "user.login") == 2


def test_other_actions_never_throttled():
    alog = AuditLog()
    for _ in range(5):
        alog.record(**_entry("admin", action="sut.file_write"))
    assert len(alog.entries) == 5


def test_window_expiry_records_again(monkeypatch):
    import rsi.security.audit as mod

    alog = AuditLog()
    now = [0.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    alog.record(**_entry("admin"))
    now[0] = mod.LOGIN_THROTTLE_S + 1
    alog.record(**_entry("admin"))
    assert len(alog.entries) == 2
