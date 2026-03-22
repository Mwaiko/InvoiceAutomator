"""
app/api/businesses.py

Businesses (clients/customers) and their branches.

POST   /businesses                              – create business (+ optional inline branches)
GET    /businesses                              – list businesses
GET    /businesses/{id}                         – get business with all branches
PATCH  /businesses/{id}                         – update business fields
DELETE /businesses/{id}                         – deactivate (soft delete)
POST   /businesses/{id}/payments                – record a payment (increments total_paid) ← NEW

POST   /businesses/{id}/branches                – add a branch
GET    /businesses/{id}/branches                – list branches for this business
GET    /businesses/{id}/branches/{branch_id}    – get single branch
PATCH  /businesses/{id}/branches/{branch_id}    – update branch
DELETE /businesses/{id}/branches/{branch_id}    – deactivate branch

Changes vs original:
  • All responses now include total_invoiced, total_paid, outstanding_balance
  • BranchCreateRequest / BranchResponse include `location`
  • New POST /{id}/payments endpoint records received payments
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PaginationDep, get_current_user, get_db
from app.core.exceptions import NotFoundError
from app.db.models.business import Branch, Business
from app.schemas.business import (
    BranchCreateRequest,
    BranchResponse,
    BranchUpdateRequest,
    BusinessCreateRequest,
    BusinessResponse,
    BusinessSummaryResponse,
    BusinessUpdateRequest,
    RecordPaymentRequest,
    RecordPaymentResponse,
)

router = APIRouter(prefix="/businesses", tags=["businesses"])


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_business_or_404(db: AsyncSession, business_id: uuid.UUID) -> Business:
    result = await db.execute(
        select(Business)
        .where(Business.id == business_id)
        .options(selectinload(Business.branches))
    )
    business = result.scalar_one_or_none()
    if not business:
        raise NotFoundError(f"Business {business_id} not found")
    return business


async def _get_branch_or_404(
    db: AsyncSession, business_id: uuid.UUID, branch_id: uuid.UUID
) -> Branch:
    result = await db.execute(
        select(Branch).where(
            Branch.id == branch_id,
            Branch.business_id == business_id,
        )
    )
    branch = result.scalar_one_or_none()
    if not branch:
        raise NotFoundError(f"Branch {branch_id} not found for business {business_id}")
    return branch


# ── Business CRUD ─────────────────────────────────────────────────────────────

@router.post("", response_model=BusinessResponse, status_code=201)
async def create_business(
    body: BusinessCreateRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Create a business. Branches can be created inline or added later."""
    if body.kra_pin:
        existing = await db.execute(
            select(Business).where(Business.kra_pin == body.kra_pin)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail=f"A business with KRA PIN '{body.kra_pin}' already exists",
            )

    branch_data     = body.branches
    business_fields = body.model_dump(exclude={"branches"})

    business = Business(**business_fields)
    db.add(business)
    await db.flush()

    for b in branch_data:
        db.add(Branch(**b.model_dump(), business_id=business.id))

    await db.commit()

    result = await db.execute(
        select(Business)
        .where(Business.id == business.id)
        .options(selectinload(Business.branches))
    )
    return result.scalar_one()


@router.get("", response_model=list[BusinessSummaryResponse])
async def list_businesses(
    pagination: PaginationDep,
    is_active: bool | None = None,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """List businesses. ?search= filters on name or KRA PIN. ?is_active=false shows inactive."""
    q = select(Business).order_by(Business.name)
    if is_active is not None:
        q = q.where(Business.is_active == is_active)
    if search:
        q = q.where(
            Business.name.ilike(f"%{search}%") | Business.kra_pin.ilike(f"%{search}%")
        )
    q = q.offset(pagination.offset).limit(pagination.limit)

    result = await db.execute(q)
    return result.scalars().all()


@router.get("/{business_id}", response_model=BusinessResponse)
async def get_business(
    business_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Returns the business with all its branches including financial totals."""
    return await _get_business_or_404(db, business_id)


@router.patch("/{business_id}", response_model=BusinessResponse)
async def update_business(
    business_id: uuid.UUID,
    body: BusinessUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    business = await _get_business_or_404(db, business_id)

    if body.kra_pin and body.kra_pin != business.kra_pin:
        existing = await db.execute(
            select(Business).where(Business.kra_pin == body.kra_pin)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail=f"KRA PIN '{body.kra_pin}' is already assigned to another business",
            )

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(business, field, value)

    await db.commit()
    return await _get_business_or_404(db, business_id)


@router.delete("/{business_id}", response_model=BusinessSummaryResponse)
async def deactivate_business(
    business_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Soft-delete: sets is_active=False. All history is preserved."""
    business = await _get_business_or_404(db, business_id)
    if not business.is_active:
        raise HTTPException(status_code=409, detail="Business is already inactive")

    business.is_active = False
    await db.commit()
    await db.refresh(business)
    return business


@router.delete("/{business_id}/permanent", status_code=204)
async def delete_business_permanently(
    business_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Hard-delete a business and all its branches.
    This is irreversible. The business must be inactive before it can be
    permanently deleted (deactivate first, then delete).
    Returns 204 No Content on success.
    """
    business = await _get_business_or_404(db, business_id)
    if business.is_active:
        raise HTTPException(
            status_code=409,
            detail="Business must be deactivated before it can be permanently deleted.",
        )
    await db.delete(business)
    await db.commit()


# ── Payment recording ─────────────────────────────────────────────────────────

@router.post("/{business_id}/payments", response_model=RecordPaymentResponse)
async def record_payment(
    business_id: uuid.UUID,
    body: RecordPaymentRequest,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Record a payment received from this business.
    Increments total_paid on the Business (and optionally on a specific Branch).
    outstanding_balance = total_invoiced - total_paid is updated automatically.
    """
    business = await _get_business_or_404(db, business_id)

    business.total_paid = float(business.total_paid or 0) + body.amount

    branch_obj: Branch | None = None
    if body.branch_id:
        branch_obj = await _get_branch_or_404(db, business_id, body.branch_id)
        branch_obj.total_paid = float(branch_obj.total_paid or 0) + body.amount

    await db.commit()
    await db.refresh(business)

    return RecordPaymentResponse(
        business_id=business.id,
        branch_id=body.branch_id,
        amount_recorded=body.amount,
        new_total_paid=float(business.total_paid),
        outstanding_balance=business.outstanding_balance,
        reference=body.reference,
    )


# ── Branch CRUD ───────────────────────────────────────────────────────────────

@router.post("/{business_id}/branches", response_model=BranchResponse, status_code=201)
async def add_branch(
    business_id: uuid.UUID,
    body: BranchCreateRequest,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    await _get_business_or_404(db, business_id)
    branch = Branch(**body.model_dump(), business_id=business_id)
    db.add(branch)
    await db.commit()
    await db.refresh(branch)
    return branch


@router.get("/{business_id}/branches", response_model=list[BranchResponse])
async def list_branches(
    business_id: uuid.UUID,
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    await _get_business_or_404(db, business_id)
    q = select(Branch).where(Branch.business_id == business_id).order_by(Branch.branch_name)
    if is_active is not None:
        q = q.where(Branch.is_active == is_active)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/{business_id}/branches/{branch_id}", response_model=BranchResponse)
async def get_branch(
    business_id: uuid.UUID,
    branch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    return await _get_branch_or_404(db, business_id, branch_id)


@router.patch("/{business_id}/branches/{branch_id}", response_model=BranchResponse)
async def update_branch(
    business_id: uuid.UUID,
    branch_id: uuid.UUID,
    body: BranchUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    branch = await _get_branch_or_404(db, business_id, branch_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(branch, field, value)
    await db.commit()
    await db.refresh(branch)
    return branch


@router.delete("/{business_id}/branches/{branch_id}", response_model=BranchResponse)
async def deactivate_branch(
    business_id: uuid.UUID,
    branch_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Soft-delete: sets is_active=False.
    Branches are never hard-deleted as they are referenced in GRNs.
    """
    branch = await _get_branch_or_404(db, business_id, branch_id)
    if not branch.is_active:
        raise HTTPException(status_code=409, detail="Branch is already inactive")
    branch.is_active = False
    await db.commit()
    await db.refresh(branch)
    return branch