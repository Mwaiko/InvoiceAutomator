"""
app/api/account_transactions.py

POST   /transactions                – post a freestanding transaction (e.g. inbound payment)
GET    /transactions                – list transactions (filterable by account / type / date)
GET    /transactions/{id}           – get single transaction
GET    /finance/profit-report       – full P&L report  (mounted on /finance router)
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PaginationDep, get_current_user, get_db
from app.core.exceptions import NotFoundError
from app.db.models.finance import AccountTransaction
from app.schemas.finance import (
    AccountTransactionCreate,
    AccountTransactionResponse,
    ProfitReport,
)
from app.services.finance_service import get_profit_report, post_transaction

txn_router = APIRouter(prefix="/transactions", tags=["transactions"])
finance_router = APIRouter(prefix="/finance", tags=["finance"])

_LOAD_TXN = [
    joinedload(AccountTransaction.account),
    joinedload(AccountTransaction.logged_by),
]


async def _get_txn_or_404(db: AsyncSession, txn_id: uuid.UUID) -> AccountTransaction:
    result = await db.execute(
        select(AccountTransaction).options(*_LOAD_TXN).where(AccountTransaction.id == txn_id)
    )
    txn = result.scalar_one_or_none()
    if not txn:
        raise NotFoundError(f"AccountTransaction {txn_id} not found")
    return txn


@txn_router.post("", response_model=AccountTransactionResponse, status_code=201)
async def create_transaction(
    body: AccountTransactionCreate,
    db:   AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """
    Post a freestanding transaction.

    Most OUTBOUND transactions are created automatically via POST /expenses.
    Use this endpoint for:
      • INBOUND customer payments not tied to an eTIMS invoice
      • Manual balance corrections
      • Opening-balance entries for new accounts
    """
    txn = await post_transaction(
        db,
        account_id       = body.account_id,
        transaction_type = body.transaction_type,
        payment_method   = body.payment_method,
        amount           = body.amount,
        reference_no     = body.reference_no,
        expense_id       = body.expense_id,
        invoice_id       = body.invoice_id,
        logged_by_id     = user.id,
        transaction_date = body.transaction_date,
    )
    txn = await _get_txn_or_404(db, txn.id)
    return AccountTransactionResponse.from_orm_txn(txn)


@txn_router.get("", response_model=list[AccountTransactionResponse])
async def list_transactions(
    pagination:       PaginationDep,
    account_id:       uuid.UUID | None = Query(None),
    transaction_type: str       | None = Query(None),
    date_from:        date      | None = Query(None),
    date_to:          date      | None = Query(None),
    db:               AsyncSession     = Depends(get_db),
    _user=Depends(get_current_user),
):
    q = (
        select(AccountTransaction)
        .options(*_LOAD_TXN)
        .order_by(AccountTransaction.transaction_date.desc())
    )
    if account_id:
        q = q.where(AccountTransaction.account_id == account_id)
    if transaction_type:
        q = q.where(AccountTransaction.transaction_type == transaction_type)
    if date_from:
        q = q.where(AccountTransaction.transaction_date >= date_from)
    if date_to:
        q = q.where(AccountTransaction.transaction_date <= date_to)

    q = q.offset(pagination.offset).limit(pagination.limit)
    rows = (await db.execute(q)).scalars().all()
    return [AccountTransactionResponse.from_orm_txn(t) for t in rows]


@txn_router.get("/{txn_id}", response_model=AccountTransactionResponse)
async def get_transaction(
    txn_id: uuid.UUID,
    db:     AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    return AccountTransactionResponse.from_orm_txn(await _get_txn_or_404(db, txn_id))


# ── Profit report (on /finance router) ───────────────────────────────────────

@finance_router.get("/profit-report", response_model=ProfitReport)
async def profit_report(
    date_from: date | None = Query(None, description="Start of period (inclusive)"),
    date_to:   date | None = Query(None, description="End of period (inclusive)"),
    db:        AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Full P&L report.

    • Per-branch revenue vs expenses and gross profit.
    • Separate line for general overhead expenses (branch_id IS NULL).
    • net_profit = total_revenue − branch_expenses − general_expenses

    Omit date_from / date_to to see all-time figures.
    """
    return await get_profit_report(db, period_start=date_from, period_end=date_to)