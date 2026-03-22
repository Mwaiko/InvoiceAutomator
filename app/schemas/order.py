"""
app/schemas/order.py
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db.models.order import OrderStatus


# ── Line item ─────────────────────────────────────────────────────────────────

class OrderItem(BaseModel):
    item_code:   str | None = None
    description: str
    uom:         str        = "PCS"
    qty_ordered: float      = Field(..., gt=0)
    unit_price:  float      = Field(..., ge=0)
    net_amount:  float      = 0.0


# ── Create ────────────────────────────────────────────────────────────────────

class OrderCreateRequest(BaseModel):
    order_number:   str
    lpo_number:     str | None = None
    supplier_name:  str
    supplier_email: str | None = None
    supplier_phone: str | None = None
    vendor_id:      str | None = None
    store_name:     str | None = None
    store_number:   str | None = None
    items:          list[OrderItem] = Field(default_factory=list)
    sub_total:      float | None   = None
    vat:            float          = 0.0
    order_total:    float | None   = None
    order_date:     str | None     = None
    expected_date:  str | None     = None
    notes:          str | None     = None


# ── Update (partial) ──────────────────────────────────────────────────────────

class OrderUpdateRequest(BaseModel):
    supplier_name:  str | None = None
    supplier_email: str | None = None
    supplier_phone: str | None = None
    store_name:     str | None = None
    store_number:   str | None = None
    items:          list[OrderItem] | None = None
    sub_total:      float | None           = None
    vat:            float | None           = None
    order_total:    float | None           = None
    order_date:     str | None             = None
    expected_date:  str | None             = None
    notes:          str | None             = None


# ── Status transition ─────────────────────────────────────────────────────────

class OrderStatusUpdate(BaseModel):
    status: OrderStatus
    notes:  str | None = None


# ── Response ──────────────────────────────────────────────────────────────────

class OrderResponse(BaseModel):
    id:             uuid.UUID
    order_number:   str
    lpo_number:     str | None
    supplier_name:  str
    supplier_email: str | None
    supplier_phone: str | None
    vendor_id:      str | None
    store_name:     str | None
    store_number:   str | None
    status:         str
    items:          list[Any]
    sub_total:      float | None
    vat:            float
    order_total:    float | None
    order_date:     str | None
    expected_date:  str | None
    notes:          str | None
    created_at:     datetime
    updated_at:     datetime

    model_config = {"from_attributes": True}