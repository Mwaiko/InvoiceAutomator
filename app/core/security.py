"""
app/core/security.py

Password hashing + JWT creation / verification.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Password ──────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────

def _create_token(subject: str, expires_delta: timedelta, extra: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + expires_delta,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.app_secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: str, role: str) -> str:
    return _create_token(
        subject=user_id,
        expires_delta=timedelta(minutes=settings.jwt_access_token_expire_minutes),
        extra={"role": role, "type": "access"},
    )


def create_refresh_token(user_id: str) -> str:
    return _create_token(
        subject=user_id,
        expires_delta=timedelta(days=settings.jwt_refresh_token_expire_days),
        extra={"type": "refresh"},
    )


def decode_token(token: str) -> dict[str, Any]:
    """
    Returns the decoded payload or raises JWTError.
    Callers should catch JWTError and convert to HTTP 401.
    """
    return jwt.decode(token, settings.app_secret_key, algorithms=[settings.jwt_algorithm])