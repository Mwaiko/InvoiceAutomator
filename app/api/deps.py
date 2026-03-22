import uuid
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.models.user import User, UserRole
from app.db.session import get_db


# ── Current user ──────────────────────────────────────────────────────────────

async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not authorization or not authorization.startswith("Bearer "):
        raise credentials_exc

    token = authorization.removeprefix("Bearer ").strip()

    try:
        payload = decode_token(token)
        user_id: str = payload.get("sub", "")
        token_type: str = payload.get("type", "")

        if not user_id or token_type != "access":
            raise credentials_exc

    except JWTError:
        raise credentials_exc

    result = await db.execute(
        select(User).where(User.id == uuid.UUID(user_id))
    )
    user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise credentials_exc

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


# ── Role guards ───────────────────────────────────────────────────────────────

def require_roles(*roles: UserRole):
    """Factory: returns a dependency function enforcing allowed roles."""

    async def _check(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' is not permitted here",
            )
        return user

    return _check


AdminOnly = Annotated[
    User,
    Depends(require_roles(UserRole.admin)),
]

AccountantOrAdmin = Annotated[
    User,
    Depends(require_roles(UserRole.admin, UserRole.accountant)),
]


# ── Pagination ────────────────────────────────────────────────────────────────

class Pagination:
    def __init__(self, page: int = 1, page_size: int = 20):
        self.page = max(1, page)
        self.page_size = min(max(1, page_size), 100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


PaginationDep = Annotated[Pagination, Depends(Pagination)]