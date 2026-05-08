"""
app/api/expense_categories.py

POST   /expense-categories          – create
GET    /expense-categories          – list all
PATCH  /expense-categories/{id}     – update
DELETE /expense-categories/{id}     – delete (only if no expenses reference it)
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.exceptions import NotFoundError
from app.db.models.finance import Expense, ExpenseCategory
from app.schemas.finance import (
    ExpenseCategoryCreate,
    ExpenseCategoryResponse,
    ExpenseCategoryUpdate,
)

router = APIRouter(prefix="/expense-categories", tags=["expense-categories"])


async def _get_or_404(db: AsyncSession, cat_id: uuid.UUID) -> ExpenseCategory:
    cat = await db.get(ExpenseCategory, cat_id)
    if not cat:
        raise NotFoundError(f"ExpenseCategory {cat_id} not found")
    return cat


@router.post("", response_model=ExpenseCategoryResponse, status_code=201)
async def create_category(
    body: ExpenseCategoryCreate,
    db:   AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    # Reject duplicate names early with a friendly message
    existing = await db.execute(
        select(ExpenseCategory).where(ExpenseCategory.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Category '{body.name}' already exists")

    cat = ExpenseCategory(**body.model_dump())
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return cat


@router.get("", response_model=list[ExpenseCategoryResponse])
async def list_categories(
    db:    AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    result = await db.execute(
        select(ExpenseCategory).order_by(ExpenseCategory.name)
    )
    return result.scalars().all()


@router.patch("/{category_id}", response_model=ExpenseCategoryResponse)
async def update_category(
    category_id: uuid.UUID,
    body:        ExpenseCategoryUpdate,
    db:          AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    cat = await _get_or_404(db, category_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(cat, field, value)
    await db.commit()
    await db.refresh(cat)
    return cat


@router.delete("/{category_id}", status_code=204)
async def delete_category(
    category_id: uuid.UUID,
    db:          AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Blocked by DB RESTRICT constraint if any expenses reference this category.
    We check first so the error message is human-readable.
    """
    cat = await _get_or_404(db, category_id)

    in_use = await db.execute(
        select(Expense.id).where(Expense.category_id == category_id).limit(1)
    )
    if in_use.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail=f"Category '{cat.name}' is used by existing expenses and cannot be deleted. "
                   "Reassign those expenses first.",
        )

    await db.delete(cat)
    await db.commit()