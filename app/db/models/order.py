"""
app/db/models/order.py

Order = Local Purchase Order (LPO) sent to a supplier.

State machine:
  draft → sent → partially_received → fully_received → cancelled

Order type:
  purchase_order  – default; order placed with a supplier
  return_order    – goods being returned to a supplier
  sales_order     – automatically set when status transitions to fully_received
"""

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, Numeric, String, Text, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.db.models.items import OrderItem


class OrderStatus(str, enum.Enum):
    draft              = "draft"
    sent               = "sent"
    partially_received = "partially_received"
    fully_received     = "fully_received"
    cancelled          = "cancelled"


class OrderType(str, enum.Enum):
    purchase_order = "purchase_order"
    return_order   = "return_order"
    sales_order    = "sales_order"


class Order(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "orders"

    # ── Identifiers ───────────────────────────────────────────────────────────
    order_number:  Mapped[str]        = mapped_column(String(100), unique=True, nullable=False, index=True)
    lpo_number:    Mapped[str | None] = mapped_column(String(100), index=True)

    # ── Supplier ──────────────────────────────────────────────────────────────
    supplier_name:  Mapped[str]        = mapped_column(String(255), nullable=False)
    supplier_email: Mapped[str | None] = mapped_column(String(255))
    supplier_phone: Mapped[str | None] = mapped_column(String(50))
    vendor_id:      Mapped[str | None] = mapped_column(String(100))   # matches GRN.vendor_id

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus),
        nullable=False,
        default=OrderStatus.draft,
        index=True,
    )

    # ── Order Type ────────────────────────────────────────────────────────────
    # Automatically promoted to sales_order when status → fully_received.
    # Can also be set to return_order manually on draft orders.
    order_type: Mapped[OrderType] = mapped_column(
        Enum(OrderType),
        nullable=False,
        default=OrderType.purchase_order,
        index=True,
    )

    # ── Store / branch receiving the order ────────────────────────────────────
    store_name:   Mapped[str | None] = mapped_column(String(255))
    store_number: Mapped[str | None] = mapped_column(String(50))

    # ── Line items stored as JSONB ─────────────────────────────────────────────
    # Schema: [{ item_code, description, uom, qty_ordered, unit_price, net_amount }]
    items: Mapped[list | None] = mapped_column(JSONB)

    # ── Totals ────────────────────────────────────────────────────────────────
    sub_total:   Mapped[float | None] = mapped_column(Numeric(15, 2))
    vat:         Mapped[float]        = mapped_column(Numeric(15, 2), default=0.0, nullable=False)
    order_total: Mapped[float | None] = mapped_column(Numeric(15, 2))

    # ── Dates ─────────────────────────────────────────────────────────────────
    order_date:    Mapped[str | None] = mapped_column(String(50))
    expected_date: Mapped[str | None] = mapped_column(String(50))

    # ── Notes ─────────────────────────────────────────────────────────────────
    notes: Mapped[str | None] = mapped_column(Text)

    # ── Who created it ────────────────────────────────────────────────────────
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # ── Normalised line items (preferred over JSONB `items` for queries) ───────
    order_items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem", back_populates="order", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Order {self.order_number} [{self.status}] ({self.order_type})>"