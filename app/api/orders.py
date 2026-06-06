"""
app/api/orders.py

POST   /orders                    – create an order (LPO)
GET    /orders                    – list orders (paginated, filterable by status / order_type)
GET    /orders/{id}               – get single order
PATCH  /orders/{id}               – update draft order fields
PATCH  /orders/{id}/status        – transition order status
DELETE /orders/{id}               – cancel a draft order (soft — sets status=cancelled)
DELETE /orders/{id}/permanent     – permanently delete an order (draft/cancelled only)
GET    /orders/{id}/grns          – list GRNs linked to this order (via lpo_number)
"""
import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PaginationDep, get_current_user, get_db
from app.core.exceptions import NotFoundError
from app.db.models.grn import GRN
from app.db.models.items import Items, OrderItem as OrderItemModel, OrderItemSource
from app.db.models.order import Order, OrderStatus, OrderType
from app.schemas.order import (
    OrderCreateRequest,
    OrderItem as OrderItemSchema,
    OrderResponse,
    OrderStatusUpdate,
    OrderUpdateRequest,
)

router = APIRouter(prefix="/orders", tags=["orders"])

LOCKED_STATUSES = {OrderStatus.fully_received, OrderStatus.cancelled}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _build_order_items(
    db: AsyncSession,
    order_id: uuid.UUID,
    items: list[OrderItemSchema],
) -> list[OrderItemModel]:
    """
    Convert schema line items → ORM OrderItem rows.

    - If item_id is provided, validates it exists in the Items catalogue and
      uses it as product_id.
    - If item_id is omitted, looks up the catalogue by itemcd (item_code) as a
      fallback.  If still not found, raises 422 so the caller knows which line
      is broken rather than silently dropping it.
    """
    orm_items: list[OrderItemModel] = []

    for idx, line in enumerate(items, start=1):
        product_id: uuid.UUID | None = None

        # ── 1. Direct catalogue reference (preferred) ─────────────────────────
        if line.item_id:
            product = await db.get(Items, line.item_id)
            if not product:
                raise HTTPException(
                    status_code=422,
                    detail=f"Line {idx}: item_id '{line.item_id}' not found in catalogue.",
                )
            product_id = product.id

        # ── 2. Fallback: match by item_code → itemcd ──────────────────────────
        elif line.item_code:
            result = await db.execute(
                select(Items).where(Items.itemcd == line.item_code).limit(1)
            )
            product = result.scalar_one_or_none()
            if product:
                product_id = product.id

        if product_id is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Line {idx} ('{line.description}'): could not resolve a catalogue item. "
                    "Provide item_id or a valid item_code."
                ),
            )

        orm_items.append(
            OrderItemModel(
                order_id=order_id,
                product_id=product_id,
                quantity_requested=line.qty_ordered,
                unit_price=line.unit_price,
                source=line.source,  # ← use what the client sent
            )
        )

    return orm_items


def _items_to_jsonb(items: list[OrderItemSchema]) -> list[dict]:
    """Serialise schema items to the JSONB snapshot on Order.items."""
    return [
        {
            "item_id":     str(i.item_id) if i.item_id else None,
            "item_code":   i.item_code,
            "description": i.description,
            "uom":         i.uom,
            "qty_ordered": i.qty_ordered,
            "unit_price":  i.unit_price,
            "net_amount":  round(i.qty_ordered * i.unit_price, 2),
            "source":      i.source.value,  # ← NEW
        }
        for i in items
    ]


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
        raise HTTPException(
            status_code=409,
            detail=f"Order number '{body.order_number}' already exists",
        )

    # Build the order — exclude `items` from model_dump so we handle it ourselves
    order_data = body.model_dump(exclude={"items"})
    order_data["items"] = _items_to_jsonb(body.items)  # JSONB snapshot

    order = Order(
        **order_data,
        created_by_id=user.id,
        status=OrderStatus.draft,
    )
    db.add(order)
    # Flush to get the order.id before creating child rows
    await db.flush()

    # Persist normalised OrderItem rows
    if body.items:
        orm_items = await _build_order_items(db, order.id, body.items)
        db.add_all(orm_items)

    await db.commit()
    await db.refresh(order)
    return order


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[OrderResponse])
async def list_orders(
    pagination: PaginationDep,
    status: OrderStatus | None = None,
    order_type: OrderType | None = None,
    supplier_name: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    q = select(Order).order_by(Order.created_at.desc())
    if status:
        q = q.where(Order.status == status)
    if order_type:
        q = q.where(Order.order_type == order_type)
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
            detail=f"Cannot edit an order with status '{order.status}'",
        )
    if "order_type" in body.model_fields_set and order.status != OrderStatus.draft:
        raise HTTPException(
            status_code=409,
            detail="order_type can only be changed while the order is in 'draft' status.",
        )

    # Apply scalar fields (exclude items — handled separately below)
    for field, value in body.model_dump(exclude_unset=True, exclude={"items"}).items():
        setattr(order, field, value)

    # ── Re-sync line items if the caller sent a new list ──────────────────────
    if body.items is not None:
        # Replace JSONB snapshot
        order.items = _items_to_jsonb(body.items)

        # Replace normalised rows: delete existing, insert fresh
        existing_items = await db.execute(
            select(OrderItemModel).where(OrderItemModel.order_id == order_id)
        )
        for old in existing_items.scalars().all():
            await db.delete(old)

        await db.flush()  # ensure deletes land before inserts

        if body.items:
            new_orm_items = await _build_order_items(db, order_id, body.items)
            db.add_all(new_orm_items)

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

    valid_transitions: dict[OrderStatus, set[OrderStatus]] = {
        OrderStatus.draft:              {OrderStatus.sent, OrderStatus.cancelled},
        OrderStatus.sent:               {OrderStatus.partially_received, OrderStatus.fully_received, OrderStatus.cancelled},
        OrderStatus.partially_received: {OrderStatus.fully_received, OrderStatus.cancelled},
        OrderStatus.fully_received:     set(),
        OrderStatus.cancelled:          set(),
    }

    allowed = valid_transitions.get(order.status, set())
    if body.status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot transition from '{order.status}' to '{body.status}'. "
                f"Allowed: {[s.value for s in allowed] or 'none (terminal state)'}"
            ),
        )

    order.status = body.status

    if body.status == OrderStatus.fully_received:
        order.order_type = OrderType.sales_order

    if body.notes:
        order.notes = (order.notes or "") + f"\n[{body.status}] {body.notes}"

    await db.commit()
    await db.refresh(order)
    return order


# ── Cancel ────────────────────────────────────────────────────────────────────

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


# ── Permanent delete ──────────────────────────────────────────────────────────

@router.delete("/{order_id}/permanent", status_code=204)
async def delete_order_permanent(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
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
    order = await db.get(Order, order_id)
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    if not order.lpo_number:
        return []

    result = await db.execute(
        select(GRN).where(GRN.extracted_data["lpo_number"].astext == order.lpo_number)
    )
    return result.scalars().all()