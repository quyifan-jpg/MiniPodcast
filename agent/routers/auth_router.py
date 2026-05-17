"""
Auth router — register, login, refresh, profile, password.

Endpoints:
  POST   /api/auth/register          Create new account     → TokenResponse
  POST   /api/auth/login             Authenticate            → TokenResponse
  POST   /api/auth/refresh           Exchange refresh token  → AccessTokenResponse
  GET    /api/auth/me                Current user profile    → UserResponse
  PATCH  /api/auth/me                Update profile          → UserResponse  (auth)
  POST   /api/auth/change-password   Change password         → 204 No Content (auth)
  DELETE /api/auth/me                Deactivate account      → 204 No Content (auth)
"""

from __future__ import annotations

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Response, status

from core.config import settings
from db import users as users_db
from middleware.auth import (
    create_access_token,
    create_refresh_token,
    require_auth,
    verify_refresh_token,
)
from models.user_schemas import (
    AccessTokenResponse,
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserResponse,
)

router = APIRouter()


# ── helpers ───────────────────────────────────────────────────────────────────


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _make_token_pair(user_id: int) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(str(user_id)),
        refresh_token=create_refresh_token(str(user_id)),
        expires_in=settings.jwt_expire_minutes * 60,
        refresh_expires_in=settings.jwt_refresh_expire_minutes * 60,
    )


# ── auth ──────────────────────────────────────────────────────────────────────


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest):
    """Create a new account and return both access + refresh tokens."""
    if users_db.get_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="Email already registered")
    if users_db.get_user_by_username(body.username):
        raise HTTPException(status_code=409, detail="Username already taken")

    user_id = users_db.create_user(
        email=body.email,
        username=body.username,
        hashed_password=_hash_password(body.password),
    )
    return _make_token_pair(user_id)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    """Authenticate with email + password and return both tokens."""
    user = users_db.get_user_by_email(body.email)
    if not user or not _verify_password(body.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    return _make_token_pair(user["id"])


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(body: RefreshRequest):
    """
    Exchange a valid refresh token for a fresh access token.
    The refresh token itself stays valid until its own expiry.
    """
    user_id = verify_refresh_token(body.refresh_token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )
    # Confirm the user is still active before re-issuing.
    user = users_db.get_user_by_id(int(user_id))
    if not user or not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account is no longer active")

    return AccessTokenResponse(
        access_token=create_access_token(user_id),
        expires_in=settings.jwt_expire_minutes * 60,
    )


# ── profile ───────────────────────────────────────────────────────────────────


@router.get("/me", response_model=UserResponse)
async def me(user_id: str = Depends(require_auth)):
    """Return the currently authenticated user's profile."""
    user = users_db.get_user_by_id(int(user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.patch("/me", response_model=UserResponse)
async def update_me(body: UpdateProfileRequest, user_id: str = Depends(require_auth)):
    """Update the current user's username and/or email. At least one field required."""
    if body.username is None and body.email is None:
        raise HTTPException(status_code=400, detail="Provide at least one field to update")

    uid = int(user_id)
    if body.username is not None:
        existing = users_db.get_user_by_username(body.username)
        if existing and existing["id"] != uid:
            raise HTTPException(status_code=409, detail="Username already taken")
        users_db.update_user_username(uid, body.username)

    if body.email is not None:
        existing = users_db.get_user_by_email(body.email)
        if existing and existing["id"] != uid:
            raise HTTPException(status_code=409, detail="Email already registered")
        users_db.update_user_email(uid, body.email)

    user = users_db.get_user_by_id(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(body: ChangePasswordRequest, user_id: str = Depends(require_auth)):
    """Verify current password and replace with new one."""
    uid = int(user_id)
    hashed = users_db.get_password_hash(uid)
    if not hashed or not _verify_password(body.current_password, hashed):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if body.current_password == body.new_password:
        raise HTTPException(status_code=400, detail="New password must differ from current password")

    users_db.update_user_password(uid, _hash_password(body.new_password))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_me(user_id: str = Depends(require_auth)):
    """
    Soft-delete the current user by setting is_active = FALSE.
    Subsequent token refresh attempts will fail; existing access tokens stop
    working as soon as they expire (max `jwt_expire_minutes`).
    """
    users_db.deactivate_user(int(user_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
