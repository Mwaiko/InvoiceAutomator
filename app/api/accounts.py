"""
app/api/accounts.py

POST   /accounts                   – create a new account (bank, mpesa, cash)
GET    /accounts                   – list all accounts
GET    /accounts/{id}              – get single account with full transaction history
PATCH  /accounts/{id}              – update name / type (balance updated via transactions)
DELETE /accounts/{id}              – delete only if balance is 0 and no transactions
POST   /accounts/{id}/reconcile    – admin: recompute balance from ledger
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.exceptions import NotFoundError
from app.db.models.finance import Account, AccountTransaction
from app.schemas.finance import AccountCreate, AccountResponse, AccountUpdate

router = APIRouter(prefix="/accounts", tags=["accounts"])


# ── helpers ───────────────────────────────────────────────────────────────────

async def _get_or_404(db: AsyncSession, account_id: uuid.UUID) -> Account:
    acct = await db.get(Account, account_id)
    if not acct:
        raise NotFoundError(f"Account {account_id} not found")
    return acct


# ── routes ────────────────────────────────────────────────────────────────────

@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(
    body: AccountCreate,
    db:   AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    acct = Account(**body.model_dump())
    db.add(acct)
    await db.commit()
    await db.refresh(acct)
    return acct


@router.get("", response_model=list[AccountResponse])
async def list_accounts(
    db:    AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    result = await db.execute(select(Account).order_by(Account.account_name))
    return result.scalars().all()


@router.get("/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: uuid.UUID,
    db:         AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    return await _get_or_404(db, account_id)


@router.patch("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: uuid.UUID,
    body:       AccountUpdate,
    db:         AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    acct = await _get_or_404(db, account_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(acct, field, value)
    await db.commit()
    await db.refresh(acct)
    return acct


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: uuid.UUID,
    db:         AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Hard-delete only when balance = 0 and no transaction history.
    This preserves the audit trail for active accounts.
    """
    acct = await _get_or_404(db, account_id)

    if float(acct.current_balance or 0) != 0:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete account with non-zero balance ({acct.current_balance}). "
                   "Zero the balance first or archive it by renaming.",
        )

    txn_check = await db.execute(
        select(AccountTransaction.id)
        .where(AccountTransaction.account_id == account_id)
        .limit(1)
    )
    if txn_check.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="Cannot delete an account that has transaction history. "
                   "Archive it by renaming instead.",
        )

    await db.delete(acct)
    await db.commit()


@router.post("/{account_id}/reconcile", response_model=dict)
async def reconcile_account(
    account_id: uuid.UUID,
    db:         AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Admin tool: recompute current_balance from the full transaction ledger.
    Returns old and new balances plus the drift.
    """
    from app.services.finance_service import reconcile_account_balance

    await _get_or_404(db, account_id)  # 404 guard
    old, new = await reconcile_account_balance(db, account_id)
    return {
        "account_id":   str(account_id),
        "old_balance":  old,
        "new_balance":  new,
        "drift":        round(new - old, 2),
        "was_in_sync":  abs(new - old) < 0.01,
    }