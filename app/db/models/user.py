
import enum
from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class UserRole(str, enum.Enum):
    admin       = "admin"
    sales       = "sales"
    accountant  = "accountant"


class User(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email:        Mapped[str]      = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name:    Mapped[str]      = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str]  = mapped_column(String(255), nullable=False)
    role:         Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False, default=UserRole.sales)
    is_active:    Mapped[bool]     = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<User {self.email} [{self.role}]>"