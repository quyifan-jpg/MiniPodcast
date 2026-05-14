"""
Auth router — register, login, me.

Endpoints:
  POST /api/auth/register   Create new account → TokenResponse
  POST /api/auth/login      Authenticate       → TokenResponse
  GET  /api/auth/me         Current user info  → UserResponse  (requires token)
"""

from __future__ import annotations

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, status

from core.config import settings
from db import users as users_db
from middleware.auth import create_access_token, require_auth
from models.user_schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse

router = APIRouter()


# ── helpers ───────────────────────────────────────────────────────────────────


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def _verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def _make_token(user_id: int) -> TokenResponse:
    token = create_access_token(str(user_id))
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_expire_minutes * 60,
    )


# ── routes ────────────────────────────────────────────────────────────────────


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest):
    """
    Create a new account.
    Returns a JWT token so the client is immediately logged in after registration.
    """
    # Check duplicates
    if users_db.get_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="Email already registered")
    if users_db.get_user_by_username(body.username):
        raise HTTPException(status_code=409, detail="Username already taken")

    hashed = _hash_password(body.password)
    user_id = users_db.create_user(
        email=body.email,
        username=body.username,
        hashed_password=hashed,
    )
    return _make_token(user_id)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    """
    Authenticate with email + password. Returns a JWT token.
    """
    user = users_db.get_user_by_email(body.email)
    if not user or not _verify_password(body.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    return _make_token(user["id"])


@router.get("/me", response_model=UserResponse)
async def me(user_id: str = Depends(require_auth)):
    """
    Return the currently authenticated user's profile.
    Requires: Authorization: Bearer <token>
    """
    user = users_db.get_user_by_id(int(user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user
