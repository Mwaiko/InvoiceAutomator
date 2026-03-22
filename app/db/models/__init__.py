"""
app/db/models/__init__.py

Importing this package registers all models with SQLAlchemy's metadata,
which lets Alembic auto-generate migrations with `alembic revision --autogenerate`.
"""

from app.db.models.user import User, UserRole                               # noqa: F401
from app.db.models.business import Business, Branch                         # noqa: F401
from app.db.models.order import Order, OrderStatus                         # noqa: F401
from app.db.models.grn import GRN, GRNStatus                               # noqa: F401
from app.db.models.etims_invoice import EtimsInvoice, EtimsStatus          # noqa: F401

__all__ = [
    "User", "UserRole",
    "Business", "Branch",
    "Order", "OrderStatus",
    "GRN", "GRNStatus",
    "EtimsInvoice", "EtimsStatus",
]