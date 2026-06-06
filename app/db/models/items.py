"""
app/db/models/items.py

OrderItem  – a line item on a Local Purchase Order (Order).
GRNItem    – receipt record for an OrderItem on a Goods Received Note (GRN).

Relationships (ownership / FK direction):
  Order ──< OrderItem ──< GRNItem
"""

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin


# ── Source enum ───────────────────────────────────────────────────────────────
class OrderItemSource(str, enum.Enum):
    manual   = "manual"
    imported = "imported"
    system   = "system"


# ── 1. Items (master catalogue) ───────────────────────────────────────────────
class Items(UUIDMixin, Base):
    """
    Master catalogue entry for a product / stock-keeping unit.

    product_name  – human-readable display name
    itemcd        – supplier or ERP item code (indexed for fast GRN matching)
    item_cls_cd   – KRA item classification code
    tax_ty_cd     – KRA tax type code (e.g. "VAT16", "EXEMPT")
    uom           – default unit of measure (e.g. "PCS", "KG", "LTR")
    pkg_cd        – packaging code
    """

    __tablename__ = "items"

    product_name: Mapped[str]        = mapped_column(String(255), nullable=False, index=True)
    itemcd:       Mapped[str | None] = mapped_column(String(100), index=True)
    item_cls_cd:  Mapped[str | None] = mapped_column(String(50))
    tax_ty_cd:    Mapped[str | None] = mapped_column(String(50))
    uom:          Mapped[str | None] = mapped_column(String(50))
    pkg_cd:       Mapped[str | None] = mapped_column(String(50))

    # ── Relationships ─────────────────────────────────────────────────────────
    order_items: Mapped[list["OrderItem"]] = relationship(
        "OrderItem", back_populates="product"
    )

    def __repr__(self) -> str:
        return f"<Product {self.product_name!r} itemcd={self.itemcd}>"


# ── 2. OrderItem ──────────────────────────────────────────────────────────────
class OrderItem(UUIDMixin, TimestampMixin, Base):
    """
    One normalised line on a purchase order.

    product_id FK points at Items (the catalogue).  The JSONB `items` array on
    Order is kept for display/legacy purposes; OrderItem is the queryable source
    of truth.
    """

    __tablename__ = "order_items"

    # ── Parent order ──────────────────────────────────────────────────────────
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        index=True,
    )
    order: Mapped["Order"] = relationship("Order", back_populates="order_items")  # type: ignore[name-defined]

    # ── Product reference ─────────────────────────────────────────────────────
    # FIX: was ForeignKey("products.id") — the table is "items"
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # FIX: was relationship("Item", ...) — the class name is "Items"
    product: Mapped[Items] = relationship("Items", back_populates="order_items")

    # ── Quantities & pricing ──────────────────────────────────────────────────
    quantity_requested: Mapped[float] = mapped_column(Numeric(15, 4), nullable=False)
    unit_price:         Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)

    # ── Origin ────────────────────────────────────────────────────────────────
    source: Mapped[OrderItemSource] = mapped_column(
        Enum(OrderItemSource),
        nullable=False,
        default=OrderItemSource.manual,
    )

    # ── Child receipts ────────────────────────────────────────────────────────
    grn_items: Mapped[list["GRNItem"]] = relationship(
        "GRNItem", back_populates="order_item", cascade="all, delete-orphan"
    )

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def associated_product_name(self) -> str:
        return self.product.product_name if self.product else ""
    
    @property
    def net_amount(self) -> float:
        return float(self.quantity_requested) * float(self.unit_price)

    @property
    def quantity_received(self) -> float:
        return sum(float(g.quantity_accepted or 0) for g in self.grn_items)

    @property
    def quantity_outstanding(self) -> float:
        return max(0.0, float(self.quantity_requested) - self.quantity_received)

    def __repr__(self) -> str:
        return (
            f"<OrderItem product_id={self.product_id} "
            f"qty={self.quantity_requested} @ {self.unit_price}>"
        )


# ── 3. GRNItem ────────────────────────────────────────────────────────────────
class GRNItem(UUIDMixin, Base):
    """
    Records how much of an OrderItem was accepted / rejected on a specific GRN.
    """

    __tablename__ = "grn_items"

    grn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("grns.id", ondelete="CASCADE"),
        index=True,
    )

    order_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("order_items.id", ondelete="SET NULL"),
        index=True,
    )
    order_item: Mapped[OrderItem | None] = relationship(
        "OrderItem", back_populates="grn_items"
    )

    quantity_accepted: Mapped[float | None] = mapped_column(Numeric(15, 4), default=0.0)
    quantity_rejected: Mapped[float | None] = mapped_column(Numeric(15, 4), default=0.0)
    rejection_reason:  Mapped[str | None]   = mapped_column(Text)

    @property
    def quantity_received(self) -> float:
        return float(self.quantity_accepted or 0) + float(self.quantity_rejected or 0)

    def __repr__(self) -> str:
        return (
            f"<GRNItem order_item={self.order_item_id} "
            f"accepted={self.quantity_accepted} rejected={self.quantity_rejected}>"
        )