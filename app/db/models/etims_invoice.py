import enum
import uuid
from sqlalchemy import Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin


class EtimsStatus(str, enum.Enum):
    pending   = "pending"
    submitted = "submitted"
    approved  = "approved"
    rejected  = "rejected"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    paid    = "paid"


class EtimsInvoice(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "etims_invoices"

    # ── Link back to the GRN that triggered this invoice ─────────────────────
    grn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("grns.id", ondelete="SET NULL"), index=True
    )
    grn_number: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True,
        comment="Store-side GRN reference, e.g. NVS-007602248",
    )
    store_number: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
        comment="Branch-assigned store number, e.g. 110, 6, 99, 2065Q",
    )
    invoice_number: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True,
        comment="Supplier's sequential invoice number, e.g. 2063 or 2065Q",
    )

    # ── Business / Branch (denormalised for financial reporting) ──────────────
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("businesses.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    business_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    branch_name:   Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── eTIMS submission state ─────────────────────────────────────────────────
    status: Mapped[EtimsStatus] = mapped_column(
        Enum(EtimsStatus), nullable=False, default=EtimsStatus.pending, index=True,
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status_new"),
        nullable=False, default=PaymentStatus.pending,
    )
    invoice_amount: Mapped[float | None] = mapped_column(
        nullable=True,
        comment="Snapshot of confirmed GRN order_total at time of invoice creation",
    )
    amount_paid: Mapped[float] = mapped_column(
        default=0, nullable=False, server_default="0",
        comment="Running total of payments received against this invoice",
    )

    # ── Payload sent to KRA ───────────────────────────────────────────────────
    payload:          Mapped[dict | None] = mapped_column(JSONB)
    kra_response:     Mapped[dict | None] = mapped_column(JSONB)
    kra_invoice_no:   Mapped[str | None]  = mapped_column(String(100))
    invoice_pdf_path: Mapped[str | None]  = mapped_column(String(1000))
    error_message:    Mapped[str | None]  = mapped_column(Text)
    retry_count:      Mapped[int]         = mapped_column(Integer, default=0, nullable=False)
    submitted_by_id:  Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    business: Mapped["Business | None"] = relationship(  # type: ignore[name-defined]
        "Business", foreign_keys=[business_id]
    )
    branch: Mapped["Branch | None"] = relationship(      # type: ignore[name-defined]
        "Branch", foreign_keys=[branch_id]
    )

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def outstanding_amount(self) -> float:
        """How much is still owed on this specific invoice."""
        return float(self.invoice_amount or 0) - float(self.amount_paid or 0)

    def recalculate_payment_status(self) -> None:
        """
        Single source of truth when a payment amount is recorded.
        Always call this after mutating amount_paid.

        Rules:
          paid <= 0        → pending  (amount_paid clamped to 0)
          0 < paid < total → pending  (stays pending until fully covered)
          paid >= total    → paid     (amount_paid clamped to invoice_amount — no overpayment)
        """
        total = float(self.invoice_amount or 0)
        paid  = float(self.amount_paid    or 0)

        if paid <= 0:
            self.payment_status = PaymentStatus.pending
            self.amount_paid    = 0.0
        elif paid >= total:
            self.payment_status = PaymentStatus.paid
            self.amount_paid    = total  # clamp — never exceed invoice_amount

    def sync_from_status(self) -> None:
        """
        Inverse of recalculate_payment_status(). Call this when payment_status
        is force-set via the manual override endpoint so amount_paid matches.

          paid    → amount_paid = invoice_amount
          pending → amount_paid = 0
        """
        if self.payment_status == PaymentStatus.paid:
            self.amount_paid = float(self.invoice_amount or 0)
        elif self.payment_status == PaymentStatus.pending:
            self.amount_paid = 0.0

    def __repr__(self) -> str:
        return (
            f"<EtimsInvoice {self.id} [{self.status}/{self.payment_status}] "
            f"grn={self.grn_id} business={self.business_name}>"
        )