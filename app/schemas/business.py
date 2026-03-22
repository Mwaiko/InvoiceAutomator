"""
app/schemas/business.py

Changes vs original:
  • BranchCreateRequest / BranchResponse  → added `location` field
  • BusinessResponse / BusinessSummaryResponse →
      added total_invoiced, total_paid, outstanding_balance
  • BranchResponse →
      added total_invoiced, total_paid, outstanding_balance
"""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, computed_field


# ── Branch schemas ────────────────────────────────────────────────────────────

class BranchCreateRequest(BaseModel):
    branch_name:    str
    store_number:   str | None = None
    location:       str | None = None      # ← NEW
    contact_person: str | None = None
    phone:          str | None = None
    email:          EmailStr | None = None
    address:        str | None = None
    county:         str | None = None


class BranchUpdateRequest(BaseModel):
    branch_name:    str | None = None
    store_number:   str | None = None
    location:       str | None = None      # ← NEW
    contact_person: str | None = None
    phone:          str | None = None
    email:          EmailStr | None = None
    address:        str | None = None
    county:         str | None = None
    is_active:      bool | None = None


class BranchResponse(BaseModel):
    id:             uuid.UUID
    business_id:    uuid.UUID
    branch_name:    str
    store_number:   str | None
    location:       str | None             # ← NEW
    contact_person: str | None
    phone:          str | None
    email:          str | None
    address:        str | None
    county:         str | None
    is_active:      bool

    # Financial
    total_invoiced:      float
    total_paid:          float
    outstanding_balance: float             # computed in model property

    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Business schemas ──────────────────────────────────────────────────────────

class BusinessCreateRequest(BaseModel):
    name:               str
    kra_pin:            str | None = None
    email:              EmailStr | None = None
    phone:              str | None = None
    credit_limit:       float = Field(default=0.0, ge=0)
    payment_terms_days: int   = Field(default=0, ge=0)
    branches:           list[BranchCreateRequest] = Field(default_factory=list)


class BusinessUpdateRequest(BaseModel):
    name:               str | None = None
    kra_pin:            str | None = None
    email:              EmailStr | None = None
    phone:              str | None = None
    credit_limit:       float | None = Field(default=None, ge=0)
    payment_terms_days: int | None   = Field(default=None, ge=0)
    is_active:          bool | None  = None


class BusinessResponse(BaseModel):
    id:                 uuid.UUID
    name:               str
    kra_pin:            str | None
    email:              str | None
    phone:              str | None
    credit_limit:       float
    payment_terms_days: int
    is_active:          bool
    branches:           list[BranchResponse] = []

    # Financial
    total_invoiced:      float
    total_paid:          float
    outstanding_balance: float      # model property

    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BusinessSummaryResponse(BaseModel):
    """Lightweight list-view — no branches array."""
    id:                 uuid.UUID
    name:               str
    kra_pin:            str | None
    email:              str | None
    phone:              str | None
    credit_limit:       float
    payment_terms_days: int
    is_active:          bool

    # Financial
    total_invoiced:      float
    total_paid:          float
    outstanding_balance: float

    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Payment recording ─────────────────────────────────────────────────────────

class RecordPaymentRequest(BaseModel):
    """
    POST /businesses/{id}/payments
    Records a payment received from the business and increments total_paid.
    Optionally also attribute to a specific branch.
    """
    amount:     float = Field(..., gt=0, description="Payment amount (must be > 0)")
    branch_id:  uuid.UUID | None = None
    reference:  str | None = None           # e.g. bank reference / receipt number
    note:       str | None = None


class RecordPaymentResponse(BaseModel):
    business_id:         uuid.UUID
    branch_id:           uuid.UUID | None
    amount_recorded:     float
    new_total_paid:      float
    outstanding_balance: float
    reference:           str | None