"""
app/schemas/grn.py

Changes vs previous version:
  • GRNItem            – to_storage_dict() `no`/`id` fallback cleaned up into
                         a small helper; no behaviour change, just readable.
  • GRNConfirmedData   – explicit `store` + `supplier` fields added so
                         business_resolver always receives them reliably
                         (previously leaked through model_extra only).
  • GRNResponse        – `status` typed as GRNStatus instead of plain str.
  • GRNResponse        – `uploaded_by_name` + `uploaded_by_email` added;
                         populated from the `uploaded_by` relationship via
                         from_orm_grn() so the API always returns uploader
                         identity alongside the UUID.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db.models.grn import GRNStatus   # single source of truth for the enum


# ── Store / supplier sub-objects ──────────────────────────────────────────────

class GRNStoreBlock(BaseModel):
    """Mirrors the 'store' dict written by read_pdf / grn_extractor."""
    company_name: str | None = None
    store_name:   str | None = None
    address:      str | None = None
    location:     str | None = None

    model_config = {"extra": "allow"}


class GRNSupplierBlock(BaseModel):
    """Mirrors the 'supplier' dict written by read_pdf / grn_extractor."""
    company_name: str | None = None
    email:        str | None = None

    model_config = {"extra": "allow"}


# ── Line item ─────────────────────────────────────────────────────────────────

class GRNItem(BaseModel):
    no: int | None       = None
    id: str | int | None = None   # Flutter sends this instead of "no"

    item_code:    str | None = None
    description:  str        = ""
    uom:          str        = "PCS"
    qty_received: float      = 1.0
    unit_price:   float      = 0.0
    net_amount:   float      = 0.0

    model_config = {"extra": "allow"}

    def _line_no(self) -> int:
        """Resolve whichever of `no` / `id` was sent, always returning an int."""
        if self.no is not None:
            return self.no
        try:
            return int(self.id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    def to_storage_dict(self) -> dict:
        return {
            "no":           self._line_no(),
            "item_code":    self.item_code,
            "description":  self.description,
            "uom":          self.uom,
            "qty_received": self.qty_received,
            "unit_price":   self.unit_price,
            "net_amount":   self.net_amount,
        }


# ── Confirmed-data body ───────────────────────────────────────────────────────

class GRNConfirmedData(BaseModel):
    receipt_voucher_no:  str | None = None
    lpo_number:          str | None = None
    delivery_invoice_no: str | None = None
    receipt_date:        str | None = None

    # ── Explicit store/supplier so business_resolver always gets them ─────────
    store:    GRNStoreBlock    | None = None
    supplier: GRNSupplierBlock | None = None

    items:       list[GRNItem] = Field(default_factory=list)
    sub_total:   float = 0.0
    vat:         float = 0.0
    order_total: float = 0.0

    model_config = {"extra": "allow"}

    def to_storage_dict(self) -> dict:
        base = {
            "receipt_voucher_no":  self.receipt_voucher_no,
            "lpo_number":          self.lpo_number,
            "delivery_invoice_no": self.delivery_invoice_no,
            "receipt_date":        self.receipt_date,
            # store + supplier are serialised explicitly so they are always
            # present as dicts in the JSONB column regardless of model_extra
            "store":    self.store.model_dump()    if self.store    else None,
            "supplier": self.supplier.model_dump() if self.supplier else None,
            "items":       [i.to_storage_dict() for i in self.items],
            "sub_total":   self.sub_total,
            "vat":         self.vat,
            "order_total": self.order_total,
        }
        extra = self.model_extra or {}
        return {**extra, **base}   # explicit keys win over any extra fields


# ── Request schemas ───────────────────────────────────────────────────────────

class GRNConfirmRequest(BaseModel):
    confirmed_data: GRNConfirmedData
    invoice_no:     str | None = None


class GRNRejectRequest(BaseModel):
    reason: str


# ── Response schema ───────────────────────────────────────────────────────────

class GRNResponse(BaseModel):
    id:                uuid.UUID
    status:            GRNStatus          # was `str` — now validated enum
    original_filename: str | None = None
    file_type:         str | None = None
    extracted_data:    dict[str, Any] | None = None
    confirmed_data:    dict[str, Any] | None = None
    invoice_no:        str | None = None
    rejection_reason:  str | None = None

    business_id: uuid.UUID | None = None
    branch_id:   uuid.UUID | None = None

    # ── Uploader identity ─────────────────────────────────────────────────────
    uploaded_by_id:    uuid.UUID | None = None
    uploaded_by_name:  str | None = None   # human-readable; resolved from relationship
    uploaded_by_email: str | None = None   # human-readable; resolved from relationship

    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_grn(cls, grn: Any) -> "GRNResponse":
        """
        Preferred constructor for route handlers.

        Pydantic's model_validate(grn) maps all scalar columns (including
        uploaded_by_id) automatically via from_attributes=True.  We then
        read the already-loaded `uploaded_by` relationship (lazy="selectin"
        on the ORM model) to populate the human-readable name/email fields.

        Usage in routes:
            return GRNResponse.from_orm_grn(grn)
        """
        schema = cls.model_validate(grn)
        uploader = getattr(grn, "uploaded_by", None)
        if uploader is not None:
            schema.uploaded_by_name  = uploader.full_name
            schema.uploaded_by_email = uploader.email
        return schema

class EtimsPayloadPreviewRequest(BaseModel):
    """
    Request body for POST /grns/{id}/etims-preview.
 
    Accepts the same confirmed_data shape as GRNConfirmRequest so the Flutter
    client can reuse the exact same object it would pass to /confirm.
 
    override_business_id / override_branch_id are lifted to the top level so
    the preview route can resolve names without digging into model_extra.
    """
    confirmed_data:       "GRNConfirmedData"   # noqa: F821  (forward ref, same file)
    invoice_no:           str | None       = None
    override_business_id: uuid.UUID | None = None
    override_branch_id:   uuid.UUID | None = None
 
 
class EtimsLineItemPreview(BaseModel):
    """One line item as it will appear in the KRA payload."""
    sequence:      int
    item_code:     str | None = None
    description:   str
    uom:           str
    qty:           float
    unit_price:    float
    discount_rate: float
    total:         float   # qty × unit_price (before any discount)
 
 
class EtimsPayloadPreviewResponse(BaseModel):
    """
    Full read-only view of the eTIMS payload that *would* be submitted to KRA.
 
    The invoice_no shown here is the next sequential number for this store
    at the moment the preview is generated.  It may increment by 1 if another
    GRN is confirmed before this one — add a note to the UI accordingly.
    """
 
    # ── Invoice header ────────────────────────────────────────────────────────
    cust_tin:        str | None = None
    cust_nm:         str                    # "Business Name - Branch Name"
    cust_branch_nm:  str
    cust_mbl_no:     str
    pmt_ty_cd:       str                    # always "07" for now
    remark:          str                    # the full remark string
    invoice_no:      str                    # zero-padded sequential number
 
    # ── Resolved store identity ───────────────────────────────────────────────
    store_number:    str                    # "?" if unknown
    business_name:   str
    branch_name:     str
 
    # ── Financials ────────────────────────────────────────────────────────────
    invoice_amount:  float
    items_total:     float                  # sum of all line-item totals
    item_count:      int
 
    # ── Line items ────────────────────────────────────────────────────────────
    items: list[EtimsLineItemPreview]
 
    # ── Operator warnings ─────────────────────────────────────────────────────
    # Non-fatal issues the operator should acknowledge before confirming.
    # e.g. unknown store number, zero-price items, mobile number is fallback.
    warnings: list[str] = Field(default_factory=list)
 