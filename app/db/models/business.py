"""
app/db/models/business.py

Business = a customer/client company  (e.g. "Naivas Limited").
Branch   = a specific store/location  (e.g. "SAFARI CENTER NAIVASHA").

Financial tracking columns (updated on every GRN confirmation):
  total_invoiced   – running sum of confirmed GRN order_totals
  total_paid       – manual/payment entries
  outstanding_balance (property) = total_invoiced - total_paid
"""

import uuid
from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin


class Business(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "businesses"

    # ── Identity ──────────────────────────────────────────────────────────────
    name:    Mapped[str]        = mapped_column(String(255), nullable=False, index=True)
    kra_pin: Mapped[str | None] = mapped_column(String(50),  unique=True,   index=True)
    email:   Mapped[str | None] = mapped_column(String(255))
    phone:   Mapped[str | None] = mapped_column(String(50))

    # ── Credit / payment terms ─────────────────────────────────────────────
    credit_limit:       Mapped[float] = mapped_column(Numeric(15, 2), default=0,  nullable=False)
    payment_terms_days: Mapped[int]   = mapped_column(Integer,        default=0,  nullable=False)

    # ── Financial running totals ──────────────────────────────────────────────
    # total_invoiced  : sum of all confirmed GRN order_totals for this business
    # total_paid      : sum of all recorded payments received from this business
    total_invoiced: Mapped[float] = mapped_column(Numeric(15, 2), default=0, nullable=False, server_default="0")
    total_paid:     Mapped[float] = mapped_column(Numeric(15, 2), default=0, nullable=False, server_default="0")

    # ── Status ─────────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── Relationships ─────────────────────────────────────────────────────────
    branches: Mapped[list["Branch"]] = relationship(
        "Branch", back_populates="business", cascade="all, delete-orphan"
    )
    grns: Mapped[list["GRN"]] = relationship(          # type: ignore[name-defined]
        "GRN", back_populates="business", foreign_keys="GRN.business_id"
    )

    @property
    def outstanding_balance(self) -> float:
        """Derived: how much this business still owes us."""
        return float(self.total_invoiced or 0) - float(self.total_paid or 0)

    def __repr__(self) -> str:
        return f"<Business {self.name}>"


class Branch(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "branches"

    # ── Parent ────────────────────────────────────────────────────────────────
    business_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ── Identity ──────────────────────────────────────────────────────────────
    branch_name:    Mapped[str]        = mapped_column(String(255), nullable=False, index=True)
    store_number:   Mapped[str | None] = mapped_column(String(50))
    location:       Mapped[str | None] = mapped_column(String(255))   # ← NEW: from GRN store.location
    contact_person: Mapped[str | None] = mapped_column(String(255))
    phone:          Mapped[str | None] = mapped_column(String(50))
    email:          Mapped[str | None] = mapped_column(String(255))
    address:        Mapped[str | None] = mapped_column(Text)
    county:         Mapped[str | None] = mapped_column(String(100))

    # ── Financial running totals ──────────────────────────────────────────────
    # Same logic as Business but scoped to this specific branch/store
    total_invoiced: Mapped[float] = mapped_column(Numeric(15, 2), default=0, nullable=False, server_default="0")
    total_paid:     Mapped[float] = mapped_column(Numeric(15, 2), default=0, nullable=False, server_default="0")

    # ── Status ─────────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # ── Relationships ─────────────────────────────────────────────────────────
    business: Mapped["Business"] = relationship("Business", back_populates="branches")
    grns: Mapped[list["GRN"]] = relationship(          # type: ignore[name-defined]
        "GRN", back_populates="branch", foreign_keys="GRN.branch_id"
    )

    @property
    def outstanding_balance(self) -> float:
        """Derived: how much is outstanding at this branch."""
        return float(self.total_invoiced or 0) - float(self.total_paid or 0)

    def __repr__(self) -> str:
        return f"<Branch {self.branch_name} (business={self.business_id})>"