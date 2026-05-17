"""
Auth module unit tests — no real DB, no real HTTP.

Tests:
  - Password hashing / verification
  - JWT create + decode round-trip
  - register: happy path, duplicate email, duplicate username
  - login: happy path, wrong password, inactive account
  - me: authenticated, unauthenticated
  - middleware: public path bypass, missing token, invalid token

Run:
    cd agent && python3 run_tests.py tests/test_auth.py -v
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

# ── helpers from auth_router ──────────────────────────────────────────────────
from routers.auth_router import _hash_password, _verify_password

# ── Password hashing ──────────────────────────────────────────────────────────


class TestPasswordHashing:
    def test_hash_is_not_plain(self):
        h = _hash_password("mysecret")
        assert h != "mysecret"

    def test_verify_correct_password(self):
        h = _hash_password("correct")
        assert _verify_password("correct", h) is True

    def test_reject_wrong_password(self):
        h = _hash_password("correct")
        assert _verify_password("wrong", h) is False

    def test_two_hashes_differ(self):
        # bcrypt uses random salt
        assert _hash_password("same") != _hash_password("same")


# ── JWT round-trip ─────────────────────────────────────────────────────────────


class TestJWT:
    def test_create_and_decode(self):
        from core.config import settings
        from middleware.auth import create_access_token

        # Needs a secret key
        with patch.object(settings, "jwt_secret_key", "test-secret"):
            token = create_access_token("42")

        import jwt

        payload = jwt.decode(token, "test-secret", algorithms=["HS256"])
        assert payload["sub"] == "42"
        assert "exp" in payload
        assert "iat" in payload

    def test_token_expires_in_future(self):
        from core.config import settings
        from middleware.auth import create_access_token

        with patch.object(settings, "jwt_secret_key", "test-secret"):
            token = create_access_token("99")

        import jwt

        payload = jwt.decode(token, "test-secret", algorithms=["HS256"])
        assert payload["exp"] > time.time()


# ── Register endpoint ─────────────────────────────────────────────────────────


class TestRegister:
    def _call(self, email, username, password, db_mock):
        """Call register() with patched DB."""
        import asyncio

        from models.user_schemas import RegisterRequest
        from routers.auth_router import register

        body = RegisterRequest(email=email, username=username, password=password)
        with patch("routers.auth_router.users_db", db_mock):
            return asyncio.run(register(body))

    def _fresh_db(self):
        db = MagicMock()
        db.get_user_by_email.return_value = None  # email not taken
        db.get_user_by_username.return_value = None  # username not taken
        db.create_user.return_value = 1  # new user id
        return db

    def test_happy_path_returns_token(self):
        from core.config import settings

        with patch.object(settings, "jwt_secret_key", "secret"):
            result = self._call("new@example.com", "newuser", "password123", self._fresh_db())
        assert result.access_token
        assert result.token_type == "bearer"

    def test_duplicate_email_raises_409(self):
        from fastapi import HTTPException

        db = self._fresh_db()
        db.get_user_by_email.return_value = {"id": 1}  # email already exists

        with pytest.raises(HTTPException) as exc:
            self._call("taken@example.com", "other", "password123", db)
        assert exc.value.status_code == 409
        assert "Email" in exc.value.detail

    def test_duplicate_username_raises_409(self):
        from fastapi import HTTPException

        db = self._fresh_db()
        db.get_user_by_username.return_value = {"id": 2}

        with pytest.raises(HTTPException) as exc:
            self._call("unique@example.com", "taken", "password123", db)
        assert exc.value.status_code == 409
        assert "Username" in exc.value.detail

    def test_short_password_rejected(self):
        from pydantic import ValidationError

        from models.user_schemas import RegisterRequest

        with pytest.raises(ValidationError):
            RegisterRequest(email="a@b.com", username="user", password="short")

    def test_invalid_email_rejected(self):
        from pydantic import ValidationError

        from models.user_schemas import RegisterRequest

        with pytest.raises(ValidationError):
            RegisterRequest(email="not-an-email", username="user", password="password123")

    def test_username_normalized_to_lowercase(self):
        from models.user_schemas import RegisterRequest

        req = RegisterRequest(email="a@b.com", username="MyUser", password="password123")
        assert req.username == "myuser"


# ── Login endpoint ─────────────────────────────────────────────────────────────


class TestLogin:
    def _stored_user(self, password: str, is_active: bool = True) -> dict:
        return {
            "id": 5,
            "email": "user@example.com",
            "hashed_password": _hash_password(password),
            "is_active": is_active,
        }

    def _call(self, email, password, db_mock):
        import asyncio

        from models.user_schemas import LoginRequest
        from routers.auth_router import login

        body = LoginRequest(email=email, password=password)
        with patch("routers.auth_router.users_db", db_mock):
            return asyncio.run(login(body))

    def test_correct_credentials_return_token(self):
        db = MagicMock()
        db.get_user_by_email.return_value = self._stored_user("correctpass")
        from core.config import settings

        with patch.object(settings, "jwt_secret_key", "secret"):
            result = self._call("user@example.com", "correctpass", db)
        assert result.access_token

    def test_wrong_password_raises_401(self):
        from fastapi import HTTPException

        db = MagicMock()
        db.get_user_by_email.return_value = self._stored_user("correctpass")

        with pytest.raises(HTTPException) as exc:
            self._call("user@example.com", "wrongpass", db)
        assert exc.value.status_code == 401

    def test_unknown_email_raises_401(self):
        from fastapi import HTTPException

        db = MagicMock()
        db.get_user_by_email.return_value = None

        with pytest.raises(HTTPException) as exc:
            self._call("ghost@example.com", "anypass", db)
        assert exc.value.status_code == 401

    def test_inactive_account_raises_403(self):
        from fastapi import HTTPException

        db = MagicMock()
        db.get_user_by_email.return_value = self._stored_user("pass", is_active=False)

        with pytest.raises(HTTPException) as exc:
            self._call("user@example.com", "pass", db)
        assert exc.value.status_code == 403


# ── /me endpoint ──────────────────────────────────────────────────────────────


class TestMe:
    def test_returns_user_profile(self):
        import asyncio
        from datetime import datetime

        from routers.auth_router import me

        db = MagicMock()
        db.get_user_by_id.return_value = {
            "id": 7,
            "email": "u@example.com",
            "username": "user7",
            "is_active": True,
            "created_at": datetime.now(),
        }
        with patch("routers.auth_router.users_db", db):
            result = asyncio.run(me(user_id="7"))
        assert result["id"] == 7

    def test_missing_user_raises_404(self):
        import asyncio

        from fastapi import HTTPException

        from routers.auth_router import me

        db = MagicMock()
        db.get_user_by_id.return_value = None
        with patch("routers.auth_router.users_db", db):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(me(user_id="999"))
        assert exc.value.status_code == 404


# ── Middleware ─────────────────────────────────────────────────────────────────


class TestAuthMiddleware:
    def _make_request(self, path: str, token: str | None = None):
        req = MagicMock()
        req.url.path = path
        req.method = "GET"
        if token:
            req.headers = {"Authorization": f"Bearer {token}"}
        else:
            req.headers = {}
        req.state = MagicMock()
        return req

    def test_public_path_no_token_passes(self):
        from middleware.auth import _PUBLIC_PATHS

        assert "/api/auth/register" in _PUBLIC_PATHS
        assert "/api/auth/login" in _PUBLIC_PATHS
        assert "/health" in _PUBLIC_PATHS

    def test_protected_prefix_configured(self):
        from middleware.auth import _PROTECTED_PREFIXES

        assert "/api/podcast-agent" in _PROTECTED_PREFIXES
        assert "/api/auth/me" in _PROTECTED_PREFIXES

    def test_extract_token_from_header(self):
        from middleware.auth import _extract_token

        req = MagicMock()
        req.headers = {"Authorization": "Bearer mytoken123"}
        assert _extract_token(req) == "mytoken123"

    def test_extract_token_missing_returns_none(self):
        from middleware.auth import _extract_token

        req = MagicMock()
        req.headers = {}
        assert _extract_token(req) is None

    def test_verify_valid_jwt(self):
        from core.config import settings
        from middleware.auth import _verify_jwt, create_access_token

        with patch.object(settings, "jwt_secret_key", "secret"):
            token = create_access_token("user1")
            payload = _verify_jwt(token)
        assert payload is not None
        assert payload["sub"] == "user1"

    def test_verify_expired_jwt_returns_none(self):
        import jwt as pyjwt

        from core.config import settings
        from middleware.auth import _verify_jwt

        expired_token = pyjwt.encode(
            {"sub": "1", "exp": int(time.time()) - 60, "iat": int(time.time()) - 120}, "secret", algorithm="HS256"
        )
        with patch.object(settings, "jwt_secret_key", "secret"):
            result = _verify_jwt(expired_token)
        assert result is None

    def test_verify_tampered_jwt_returns_none(self):
        from core.config import settings
        from middleware.auth import _verify_jwt

        with patch.object(settings, "jwt_secret_key", "secret"):
            result = _verify_jwt("not.a.valid.jwt")
        assert result is None


# ── Refresh token ─────────────────────────────────────────────────────────────


class TestRefreshToken:
    def test_create_refresh_round_trip(self):
        from core.config import settings
        from middleware.auth import create_refresh_token, verify_refresh_token

        with patch.object(settings, "jwt_secret_key", "secret"):
            token = create_refresh_token("42")
            assert verify_refresh_token(token) == "42"

    def test_access_token_rejected_by_refresh_verifier(self):
        from core.config import settings
        from middleware.auth import create_access_token, verify_refresh_token

        with patch.object(settings, "jwt_secret_key", "secret"):
            access = create_access_token("42")
            assert verify_refresh_token(access) is None

    def test_login_returns_both_tokens(self):
        from core.config import settings

        db = MagicMock()
        db.get_user_by_email.return_value = {
            "id": 9,
            "email": "u@e.com",
            "hashed_password": _hash_password("pw12345678"),
            "is_active": True,
        }
        with patch.object(settings, "jwt_secret_key", "secret"):
            result = TestLogin()._call("u@e.com", "pw12345678", db)
        assert result.access_token
        assert result.refresh_token
        assert result.expires_in > 0
        assert result.refresh_expires_in > result.expires_in

    def test_refresh_returns_new_access_token(self):
        import asyncio

        from core.config import settings
        from middleware.auth import create_refresh_token
        from models.user_schemas import RefreshRequest
        from routers.auth_router import refresh

        db = MagicMock()
        db.get_user_by_id.return_value = {"id": 7, "is_active": True}
        with patch.object(settings, "jwt_secret_key", "secret"):
            refresh_token = create_refresh_token("7")
            with patch("routers.auth_router.users_db", db):
                result = asyncio.run(refresh(RefreshRequest(refresh_token=refresh_token)))
        assert result.access_token

    def test_refresh_with_invalid_token_raises_401(self):
        import asyncio

        from fastapi import HTTPException

        from core.config import settings
        from models.user_schemas import RefreshRequest
        from routers.auth_router import refresh

        with patch.object(settings, "jwt_secret_key", "secret"):
            with pytest.raises(HTTPException) as exc:
                asyncio.run(refresh(RefreshRequest(refresh_token="garbage")))
        assert exc.value.status_code == 401

    def test_refresh_with_inactive_user_raises_403(self):
        import asyncio

        from fastapi import HTTPException

        from core.config import settings
        from middleware.auth import create_refresh_token
        from models.user_schemas import RefreshRequest
        from routers.auth_router import refresh

        db = MagicMock()
        db.get_user_by_id.return_value = {"id": 7, "is_active": False}
        with patch.object(settings, "jwt_secret_key", "secret"):
            token = create_refresh_token("7")
            with patch("routers.auth_router.users_db", db):
                with pytest.raises(HTTPException) as exc:
                    asyncio.run(refresh(RefreshRequest(refresh_token=token)))
        assert exc.value.status_code == 403


# ── Profile / password / delete ───────────────────────────────────────────────


class TestProfileUpdate:
    def _call(self, *, username=None, email=None, db, uid="3"):
        import asyncio

        from models.user_schemas import UpdateProfileRequest
        from routers.auth_router import update_me

        body = UpdateProfileRequest(username=username, email=email)
        with patch("routers.auth_router.users_db", db):
            return asyncio.run(update_me(body, user_id=uid))

    def _db_with_user(self, uid=3):
        from datetime import datetime

        db = MagicMock()
        db.get_user_by_username.return_value = None
        db.get_user_by_email.return_value = None
        db.get_user_by_id.return_value = {
            "id": uid,
            "email": "u@e.com",
            "username": "newname",
            "is_active": True,
            "created_at": datetime.now(),
        }
        return db

    def test_update_username_only(self):
        db = self._db_with_user()
        self._call(username="newname", db=db)
        db.update_user_username.assert_called_once_with(3, "newname")
        db.update_user_email.assert_not_called()

    def test_update_email_only(self):
        db = self._db_with_user()
        self._call(email="new@example.com", db=db)
        db.update_user_email.assert_called_once_with(3, "new@example.com")
        db.update_user_username.assert_not_called()

    def test_empty_body_raises_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._call(db=self._db_with_user())
        assert exc.value.status_code == 400

    def test_username_taken_by_other_user_raises_409(self):
        from fastapi import HTTPException

        db = self._db_with_user(uid=3)
        db.get_user_by_username.return_value = {"id": 99, "username": "taken"}
        with pytest.raises(HTTPException) as exc:
            self._call(username="taken", db=db)
        assert exc.value.status_code == 409

    def test_username_owned_by_same_user_allowed(self):
        db = self._db_with_user(uid=3)
        # Returning ourselves means it's not actually taken — should not raise
        db.get_user_by_username.return_value = {"id": 3, "username": "newname"}
        self._call(username="newname", db=db)
        db.update_user_username.assert_called_once()


class TestChangePassword:
    def _call(self, current, new, db, uid="4"):
        import asyncio

        from models.user_schemas import ChangePasswordRequest
        from routers.auth_router import change_password

        body = ChangePasswordRequest(current_password=current, new_password=new)
        with patch("routers.auth_router.users_db", db):
            return asyncio.run(change_password(body, user_id=uid))

    def test_happy_path(self):
        db = MagicMock()
        db.get_password_hash.return_value = _hash_password("oldpass123")
        self._call("oldpass123", "newpass456", db)
        db.update_user_password.assert_called_once()

    def test_wrong_current_password_raises_401(self):
        from fastapi import HTTPException

        db = MagicMock()
        db.get_password_hash.return_value = _hash_password("oldpass123")
        with pytest.raises(HTTPException) as exc:
            self._call("WRONG", "newpass456", db)
        assert exc.value.status_code == 401

    def test_same_password_raises_400(self):
        from fastapi import HTTPException

        db = MagicMock()
        db.get_password_hash.return_value = _hash_password("samepass1")
        with pytest.raises(HTTPException) as exc:
            self._call("samepass1", "samepass1", db)
        assert exc.value.status_code == 400


class TestDeactivateMe:
    def test_calls_deactivate(self):
        import asyncio

        from routers.auth_router import deactivate_me

        db = MagicMock()
        with patch("routers.auth_router.users_db", db):
            asyncio.run(deactivate_me(user_id="13"))
        db.deactivate_user.assert_called_once_with(13)
