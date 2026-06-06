"""
app/api/items.py

GET  /items        – list / search catalog items (for order-creation dropdowns)
GET  /items/{id}   – get a single item by UUID
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PaginationDep, get_current_user, get_db
from app.core.exceptions import NotFoundError
from app.db.models.items import Items
from app.schemas.items import ItemResponse

router = APIRouter(prefix="/items", tags=["items"])


@router.get("", response_model=list[ItemResponse])
async def list_items(
    pagination: PaginationDep,
    search: str | None = None,          # fuzzy-match product_name or itemcd
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Return catalog items.  Use ?search=<term> to filter by product name or
    item code — handy for a frontend autocomplete / search-as-you-type box.
    """
    q = select(Items).order_by(Items.product_name)

    if search:
        term = f"%{search}%"
        q = q.where(
            Items.product_name.ilike(term) | Items.itemcd.ilike(term)
        )

    q = q.offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/{item_id}", response_model=ItemResponse)
async def get_item(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    item = await db.get(Items, item_id)
    if not item:
        raise NotFoundError(f"Item {item_id} not found")
    return item