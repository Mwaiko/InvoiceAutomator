"""
app/api/expenses.py

POST   /expenses                   – record a new expense (+ optional AccountTransaction)
GET    /expenses                   – list expenses (filterable)
GET    /expenses/{id}              – get single expense with resolved names
PATCH  /expenses/{id}              – update fields (mark KRA-declared, attach receipt, etc.)
DELETE /expenses/{id}              – hard-delete (paid expenses are blocked)
GET    /expenses/kra-export        – KRA audit export (all declared expenses + receipts)
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PaginationDep, get_current_user, get_db
from app.core.exceptions import NotFoundError
from app.db.models.finance import Expense
from app.schemas.finance import ExpenseCreate, ExpenseResponse, ExpenseUpdate, KRAExportRow
from app.services.finance_service import record_expense_payment

router = APIRouter(prefix="/expenses", tags=["expenses"])

_LOAD_EXPENSE = [
    joinedload(Expense.category),
    joinedload(Expense.business),
    joinedload(Expense.branch),
    joinedload(Expense.created_by),
    joinedload(Expense.account_transaction),
]


async def _get_or_404(db: AsyncSession, expense_id: uuid.UUID) -> Expense:
    result = await db.execute(
        select(Expense).options(*_LOAD_EXPENSE).where(Expense.id == expense_id)
    )
    exp = result.scalar_one_or_none()
    if not exp:
        raise NotFoundError(f"Expense {expense_id} not found")
    return exp


@router.post("", response_model=ExpenseResponse, status_code=201)
async def create_expense(
    body: ExpenseCreate,
    db:   AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """
    If `account_id` is included in the body, this also creates an OUTBOUND
    AccountTransaction and decrements Account.current_balance atomically.
    """
    expense, _txn = await record_expense_payment(
        db,
        amount          = body.amount,
        description     = body.description,
        expense_date    = body.expense_date,
        category_id     = body.category_id,
        business_id     = body.business_id,
        branch_id       = body.branch_id,
        created_by_id   = user.id,
        status          = body.status,
        is_kra_declared = body.is_kra_declared,
        receipt_path    = body.receipt_path,
        account_id      = body.account_id,
        payment_method  = body.payment_method,
        reference_no    = body.reference_no,
    )
    # Re-fetch with relationships eager-loaded for the response
    expense = await _get_or_404(db, expense.id)
    return ExpenseResponse.from_orm_expense(expense)


@router.get("", response_model=list[ExpenseResponse])
async def list_expenses(
    pagination:    PaginationDep,
    status:        str | None       = Query(None),
    branch_id:     uuid.UUID | None = Query(None),
    business_id:   uuid.UUID | None = Query(None),
    category_id:   uuid.UUID | None = Query(None),
    is_kra_declared: bool | None    = Query(None),
    date_from:     date | None      = Query(None),
    date_to:       date | None      = Query(None),
    db:            AsyncSession     = Depends(get_db),
    _user=Depends(get_current_user),
):
    q = (
        select(Expense)
        .options(*_LOAD_EXPENSE)
        .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
    )
    if status:
        q = q.where(Expense.status == status)
    if branch_id:
        q = q.where(Expense.branch_id == branch_id)
    if business_id:
        q = q.where(Expense.business_id == business_id)
    if category_id:
        q = q.where(Expense.category_id == category_id)
    if is_kra_declared is not None:
        q = q.where(Expense.is_kra_declared == is_kra_declared)
    if date_from:
        q = q.where(Expense.expense_date >= date_from)
    if date_to:
        q = q.where(Expense.expense_date <= date_to)

    q = q.offset(pagination.offset).limit(pagination.limit)
    expenses = (await db.execute(q)).scalars().all()
    return [ExpenseResponse.from_orm_expense(e) for e in expenses]


@router.get("/kra-export", response_model=list[KRAExportRow])
async def kra_export(
    date_from: date | None = Query(None),
    date_to:   date | None = Query(None),
    db:        AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Returns all KRA-declared expenses with receipt paths and bank/M-Pesa refs.

    Perfect for copy-pasting into an auditor's spreadsheet or driving a CSV
    download on the frontend.
    """
    from app.services.finance_service import get_kra_audit_export
    return await get_kra_audit_export(db, period_start=date_from, period_end=date_to)


@router.get("/{expense_id}", response_model=ExpenseResponse)
async def get_expense(
    expense_id: uuid.UUID,
    db:         AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    return ExpenseResponse.from_orm_expense(await _get_or_404(db, expense_id))


@router.patch("/{expense_id}", response_model=ExpenseResponse)
async def update_expense(
    expense_id: uuid.UUID,
    body:       ExpenseUpdate,
    db:         AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Key use-cases:
      • Mark as KRA-declared: PATCH { "is_kra_declared": true }
      • Attach a receipt:     PATCH { "receipt_path": "receipts/2025/fuel.pdf" }
      • Change status:        PATCH { "status": "cancelled" }
    """
    exp = await _get_or_404(db, expense_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(exp, field, value)
    await db.commit()
    exp = await _get_or_404(db, expense_id)
    return ExpenseResponse.from_orm_expense(exp)


@router.delete("/{expense_id}", status_code=204)
async def delete_expense(
    expense_id: uuid.UUID,
    db:         AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Blocks deletion of paid expenses to preserve the audit trail.
    Cancel the expense first, then delete if needed.
    """
    exp = await _get_or_404(db, expense_id)
    if exp.status == "paid":
        raise HTTPException(
            status_code=409,
            detail="Cannot delete a paid expense. "
                   "Set status to 'cancelled' first to preserve the audit trail.",
        )
    await db.delete(exp)
    await db.commit()