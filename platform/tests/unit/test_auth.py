import pytest

from rsi.core.errors import AuthError
from rsi.security.auth import AuthService, Claims


class TestAuthService:
    def test_hash_and_verify(self):
        svc = AuthService(secret="test-secret")
        hashed = svc.hash_password("mypassword")
        assert svc.verify_password("mypassword", hashed)
        assert not svc.verify_password("wrong", hashed)

    def test_login_and_verify_roundtrip(self):
        svc = AuthService(secret="test-secret")
        token, claims = svc.login(
            user_id="u-1", role="admin", stored_hash=svc.hash_password("pw"), password="pw"
        )
        assert claims.sub == "u-1"
        assert claims.role == "admin"
        assert claims.jti
        verified = svc.verify(token)
        assert verified.sub == "u-1"
        assert verified.jti == claims.jti

    def test_expired_token_rejected(self):
        svc = AuthService(secret="test-secret", expire_hours=-1)
        token, _ = svc.login(
            user_id="u-1", role="admin", stored_hash=svc.hash_password("pw"), password="pw"
        )
        with pytest.raises(AuthError):
            svc.verify(token)

    def test_wrong_password_same_error(self):
        svc = AuthService(secret="s")
        with pytest.raises(AuthError) as e1:
            svc.login(user_id="nonexist", role="admin", stored_hash="x", password="wrong")
        with pytest.raises(AuthError) as e2:
            svc.login(
                user_id="real", role="admin", stored_hash=svc.hash_password("pw"), password="bad"
            )
        assert str(e1.value) == str(e2.value)

    def test_verify_rejects_garbage_and_wrong_secret(self):
        svc = AuthService(secret="s")
        with pytest.raises(AuthError):
            svc.verify("not-a-token")
        token, _ = svc.login(
            user_id="u", role="admin", stored_hash=svc.hash_password("pw"), password="pw"
        )
        with pytest.raises(AuthError):
            AuthService(secret="other").verify(token)

    def test_claims_model_fields(self):
        c = Claims(sub="u", role="researcher", exp=1, iat=0, jti="j")
        assert c.sub == "u" and c.role == "researcher"
