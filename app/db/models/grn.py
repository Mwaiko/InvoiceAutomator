"""
app/db/models/grn.py

GRN state machine:
  uploaded → extracted → pending_confirmation → confirmed → invoiced
                                             ↘ rejected

Each GRN is now linked to the Business (company) and Branch (store)
resolved automatically from the extracted store data on upload.
"""

import enum
import uuid
from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin


class GRNStatus(str, enum.Enum):
    uploaded             = "uploaded"
    extracted            = "extracted"
    pending_confirmation = "pending_confirmation"
    confirmed            = "confirmed"
    invoiced             = "invoiced"
    rejected             = "rejected"


class GRN(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "grns"

    # ── File ──────────────────────────────────────────────────────────────────
    original_filename: Mapped[str]        = mapped_column(String(500), nullable=False)
    storage_path:      Mapped[str | None] = mapped_column(String(1000))
    file_type:         Mapped[str | None] = mapped_column(String(10))   # "pdf" | "image"

    # ── State ─────────────────────────────────────────────────────────────────
    status: Mapped[GRNStatus] = mapped_column(
        Enum(GRNStatus),
        nullable=False,
        default=GRNStatus.uploaded,
        index=True,
    )

    # ── Extracted data (AI / OCR output — mutable until confirmed) ────────────
    extracted_data: Mapped[dict | None] = mapped_column(JSONB)

    # ── Confirmed data (operator-reviewed — IMMUTABLE after confirmation) ─────
    confirmed_data: Mapped[dict | None] = mapped_column(JSONB)

    # ── eTIMS fields ──────────────────────────────────────────────────────────
    etims_payload:  Mapped[dict | None] = mapped_column(JSONB)
    etims_response: Mapped[dict | None] = mapped_column(JSONB)
    invoice_no:     Mapped[str | None]  = mapped_column(String(100))

    # ── Business / Branch linkage (resolved from extracted store data) ─────────
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ── Uploader ──────────────────────────────────────────────────────────────
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    rejection_reason: Mapped[str | None] = mapped_column(Text)

    # ── Relationships ──────────────────────────────────────────────────────────
    business: Mapped["Business | None"] = relationship(   # type: ignore[name-defined]
        "Business", back_populates="grns", foreign_keys=[business_id]
    )
    branch: Mapped["Branch | None"] = relationship(       # type: ignore[name-defined]
        "Branch", back_populates="grns", foreign_keys=[branch_id]
    )
    # ── NEW: relationship to User so the uploader's name is always accessible ──
    uploaded_by: Mapped["User | None"] = relationship(    # type: ignore[name-defined]
        "User", foreign_keys=[uploaded_by_id], lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<GRN {self.id} [{self.status}]>"