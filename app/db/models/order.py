"""
app/db/models/order.py

Order = Local Purchase Order (LPO) sent to a supplier.

State machine:
  draft → sent → partially_received → fully_received → cancelled
"""

import enum
import uuid
from sqlalchemy import Enum, ForeignKey, Numeric, String, Text, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin


class OrderStatus(str, enum.Enum):
    draft              = "draft"
    sent               = "sent"
    partially_received = "partially_received"
    fully_received     = "fully_received"
    cancelled          = "cancelled"


class Order(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "orders"

    # ── Identifiers ───────────────────────────────────────────────────────────
    order_number:  Mapped[str]       = mapped_column(String(100), unique=True, nullable=False, index=True)
    lpo_number:    Mapped[str | None] = mapped_column(String(100), index=True)

    # ── Supplier ──────────────────────────────────────────────────────────────
    supplier_name:  Mapped[str]       = mapped_column(String(255), nullable=False)
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

    def __repr__(self) -> str:
        return f"<Order {self.order_number} [{self.status}]>"