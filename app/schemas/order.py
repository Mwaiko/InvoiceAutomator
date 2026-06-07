"""
app/schemas/order.py
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.db.models.order import OrderStatus, OrderType
from app.db.models.items import OrderItemSource  # ← NEW


# ── Line item ─────────────────────────────────────────────────────────────────

class OrderItem(BaseModel):
    item_id:     uuid.UUID | None = None
    item_code:   str | None       = None
    description: str
    uom:         str              = "PCS"
    qty_ordered: float            = Field(..., gt=0)
    unit_price:  float            = Field(..., ge=0)
    net_amount:  float            = 0.0
    source:      OrderItemSource  = OrderItemSource.farm  # ← NEW


# ── Create ────────────────────────────────────────────────────────────────────

class OrderCreateRequest(BaseModel):
    order_number:   str
    lpo_number:     str | None  = None
    supplier_name:  str
    supplier_email: str | None  = None
    supplier_phone: str | None  = None
    vendor_id:      str | None  = None
    store_name:     str | None  = None
    store_number:   str | None  = None
    order_type:     OrderType   = OrderType.purchase_order
    items:          list[OrderItem] = Field(default_factory=list)
    sub_total:      float | None    = None
    vat:            float           = 0.0
    order_total:    float | None    = None
    order_date:     str | None      = None
    expected_date:  str | None      = None
    notes:          str | None      = None

    @field_validator("order_type")
    @classmethod
    def creation_type_not_sales(cls, v: OrderType) -> OrderType:
        if v == OrderType.sales_order:
            raise ValueError(
                "Cannot create an order with type 'sales_order'. "
                "Orders are promoted to sales_order automatically when fully received."
            )
        return v


# ── Update (partial) ──────────────────────────────────────────────────────────

class OrderUpdateRequest(BaseModel):
    supplier_name:  str | None       = None
    supplier_email: str | None       = None
    supplier_phone: str | None       = None
    store_name:     str | None       = None
    store_number:   str | None       = None
    order_type:     OrderType | None = None
    items:          list[OrderItem] | None = None
    sub_total:      float | None     = None
    vat:            float | None     = None
    order_total:    float | None     = None
    order_date:     str | None       = None
    expected_date:  str | None       = None
    notes:          str | None       = None

    @field_validator("order_type")
    @classmethod
    def update_type_not_sales(cls, v: OrderType | None) -> OrderType | None:
        if v == OrderType.sales_order:
            raise ValueError(
                "Cannot manually set order_type to 'sales_order'. "
                "It is promoted automatically when the order is fully received."
            )
        return v


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
    order_type:     str
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