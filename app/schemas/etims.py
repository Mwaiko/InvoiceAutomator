"""
app/schemas/etims.py

Changes:
  • EtimsInvoiceResponse → added grn_number, store_number, invoice_number
    (the three fields that appear in the supplier statement spreadsheet but
    were missing from the DB schema)
  • EtimsInvoiceResponse → added business_id, branch_id, business_name,
    branch_name, invoice_amount, amount_paid, outstanding_amount
  • RecordPaymentRequest accepts an amount and auto-derives payment_status
  • Added EtimsPaymentSummary for the business-level rollup view
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.db.models.etims_invoice import PaymentStatus


# ── Response ──────────────────────────────────────────────────────────────────

class EtimsInvoiceResponse(BaseModel):
    id:      uuid.UUID
    grn_id:  uuid.UUID | None

    # ── Document reference numbers (matches supplier statement spreadsheet) ───
    # grn_number    → Col C  e.g. "NVS-007602248"  (store-side GRN reference)
    # store_number  → Col I  e.g. "110", "6", "2065Q"  (branch-assigned store no)
    # invoice_number→ Col H  e.g. "2063", "1945"  (supplier's sequential invoice no)
    grn_number:     str | None
    store_number:   str | None
    invoice_number: str | None

    # Business / branch context
    business_id:   uuid.UUID | None
    branch_id:     uuid.UUID | None
    business_name: str | None
    branch_name:   str | None

    # eTIMS submission state
    status:           str
    kra_invoice_no:   str | None
    invoice_pdf_path: str | None
    error_message:    str | None
    retry_count:      int

    # Financial
    invoice_amount:     float | None
    amount_paid:        float
    outstanding_amount: float
    payment_status:     str

    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Requests ──────────────────────────────────────────────────────────────────

class RecordInvoicePaymentRequest(BaseModel):
    """
    PATCH /etims-invoices/{id}/payment
    Records a payment. payment_status is derived automatically.
    """
    amount:    float = Field(..., gt=0, description="Amount being paid (must be > 0)")
    reference: str | None = None
    note:      str | None = None


class ManualPaymentStatusRequest(BaseModel):
    """
    PATCH /etims-invoices/{id}/payment-status
    Force-override payment status (accountant correction).
    """
    payment_status: PaymentStatus


# ── Rollup summary ────────────────────────────────────────────────────────────

class EtimsPaymentSummary(BaseModel):
    """
    GET /etims-invoices/summary?business_id=...
    Accounts-receivable overview per business.
    """
    business_id:        uuid.UUID
    business_name:      str
    total_invoiced:     float
    total_paid:         float
    outstanding_amount: float
    invoice_count:      int
    unpaid_count:       int