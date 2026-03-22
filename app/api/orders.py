"""
app/api/orders.py

POST   /orders                    – create an order (LPO)
GET    /orders                    – list orders (paginated, filterable by status)
GET    /orders/{id}               – get single order
PATCH  /orders/{id}               – update draft order fields
PATCH  /orders/{id}/status        – transition order status
DELETE /orders/{id}               – cancel a draft order (soft — sets status=cancelled)
DELETE /orders/{id}/permanent     – permanently delete an order (draft/cancelled only)
GET    /orders/{id}/grns          – list GRNs linked to this order (via lpo_number)
GET    /orders/{id}/payments      – list payments linked to this order
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PaginationDep, get_current_user, get_db
from app.core.exceptions import NotFoundError
from app.db.models.grn import GRN
from app.db.models.order import Order, OrderStatus
from app.schemas.order import (
    OrderCreateRequest,
    OrderResponse,
    OrderStatusUpdate,
    OrderUpdateRequest,
)

router = APIRouter(prefix="/orders", tags=["orders"])

# Statuses that cannot be edited
LOCKED_STATUSES = {OrderStatus.fully_received, OrderStatus.cancelled}


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", response_model=OrderResponse, status_code=201)
async def create_order(
    body: OrderCreateRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    # Prevent duplicate order numbers
    existing = await db.execute(
        select(Order).where(Order.order_number == body.order_number)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Order number '{body.order_number}' already exists")

    order = Order(
        **body.model_dump(),
        created_by_id=user.id,
        status=OrderStatus.draft,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return order


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[OrderResponse])
async def list_orders(
    pagination: PaginationDep,
    status: OrderStatus | None = None,
    supplier_name: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    q = select(Order).order_by(Order.created_at.desc())
    if status:
        q = q.where(Order.status == status)
    if supplier_name:
        q = q.where(Order.supplier_name.ilike(f"%{supplier_name}%"))
    q = q.offset(pagination.offset).limit(pagination.limit)

    result = await db.execute(q)
    return result.scalars().all()


# ── Get single ────────────────────────────────────────────────────────────────

@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    order = await db.get(Order, order_id)
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    return order


# ── Update fields (draft only) ────────────────────────────────────────────────

@router.patch("/{order_id}", response_model=OrderResponse)
async def update_order(
    order_id: uuid.UUID,
    body: OrderUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    order = await db.get(Order, order_id)
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    if order.status in LOCKED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot edit an order with status '{order.status}'"
        )

    # Apply only the fields that were actually sent
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(order, field, value)

    await db.commit()
    await db.refresh(order)
    return order


# ── Status transition ─────────────────────────────────────────────────────────

@router.patch("/{order_id}/status", response_model=OrderResponse)
async def update_order_status(
    order_id: uuid.UUID,
    body: OrderStatusUpdate,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    order = await db.get(Order, order_id)
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    # Enforce valid transitions
    valid_transitions: dict[OrderStatus, set[OrderStatus]] = {
        OrderStatus.draft:              {OrderStatus.sent, OrderStatus.cancelled},
        OrderStatus.sent:               {OrderStatus.partially_received, OrderStatus.fully_received, OrderStatus.cancelled},
        OrderStatus.partially_received: {OrderStatus.fully_received, OrderStatus.cancelled},
        OrderStatus.fully_received:     set(),   # terminal
        OrderStatus.cancelled:          set(),   # terminal
    }

    allowed = valid_transitions.get(order.status, set())
    if body.status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot transition from '{order.status}' to '{body.status}'. "
                   f"Allowed: {[s.value for s in allowed] or 'none (terminal state)'}",
        )

    order.status = body.status
    if body.notes:
        order.notes = (order.notes or "") + f"\n[{body.status}] {body.notes}"

    await db.commit()
    await db.refresh(order)
    return order


# ── Cancel (convenience alias for status → cancelled) ────────────────────────

@router.delete("/{order_id}", response_model=OrderResponse)
async def cancel_order(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    order = await db.get(Order, order_id)
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    if order.status == OrderStatus.cancelled:
        raise HTTPException(status_code=409, detail="Order is already cancelled")
    if order.status == OrderStatus.fully_received:
        raise HTTPException(status_code=409, detail="Cannot cancel a fully received order")

    order.status = OrderStatus.cancelled
    await db.commit()
    await db.refresh(order)
    return order


# ── Permanent delete (draft / cancelled only) ─────────────────────────────────

@router.delete("/{order_id}/permanent", status_code=204)
async def delete_order_permanent(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Hard-delete an order. Only allowed for draft or cancelled orders."""
    order = await db.get(Order, order_id)
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    if order.status not in {OrderStatus.draft, OrderStatus.cancelled}:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot permanently delete an order with status '{order.status}'. "
                "Only draft or cancelled orders may be deleted."
            ),
        )
    await db.delete(order)
    await db.commit()


# ── Related GRNs ──────────────────────────────────────────────────────────────

@router.get("/{order_id}/grns")
async def get_order_grns(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Returns GRNs that were linked to this order via matching lpo_number."""
    order = await db.get(Order, order_id)
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    if not order.lpo_number:
        return []

    result = await db.execute(
        select(GRN).where(GRN.extracted_data["lpo_number"].astext == order.lpo_number)
    )
    return result.scalars().all()