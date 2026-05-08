"""
app/services/finance_service.py

All business logic that touches the four finance tables lives here.
Routes stay thin; they call these functions and return the result.

Public surface
──────────────
  record_expense_payment()   – atomically create Expense + AccountTransaction
                               and update Account.current_balance
  post_transaction()         – create a standalone AccountTransaction
                               (e.g. inbound customer payment)
  get_profit_report()        – per-branch and whole-business P&L
  get_kra_audit_export()     – all KRA-declared expenses with receipt paths
  reconcile_account_balance()– recompute current_balance from ledger (repair tool)
"""

import logging
from datetime import date, datetime, timezone
from typing import Sequence
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.db.models.finance import Account, AccountTransaction, Expense, ExpenseCategory
from app.schemas.finance import (
    BranchProfitLine,
    KRAExportRow,
    ProfitReport,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def _get_account_or_raise(db: AsyncSession, account_id: uuid.UUID) -> Account:
    acct = await db.get(Account, account_id)
    if not acct:
        raise ValueError(f"Account {account_id} not found")
    return acct


# ═══════════════════════════════════════════════════════════════════════════════
# 1. record_expense_payment()
# ═══════════════════════════════════════════════════════════════════════════════

async def record_expense_payment(
    db:             AsyncSession,
    *,
    # Expense fields
    amount:          float,
    description:     str,
    expense_date:    date,
    category_id:     uuid.UUID | None = None,
    business_id:     uuid.UUID | None = None,
    branch_id:       uuid.UUID | None = None,
    created_by_id:   uuid.UUID | None = None,
    status:          str               = "paid",
    is_kra_declared: bool              = False,
    receipt_path:    str | None        = None,
    # Payment / transaction fields (all optional)
    account_id:      uuid.UUID | None = None,
    payment_method:  str | None       = None,
    reference_no:    str | None       = None,
    transaction_date: datetime | None = None,
) -> tuple[Expense, AccountTransaction | None]:
    """
    Atomically:
      1. Insert an Expense row.
      2. If account_id is provided, insert an OUTBOUND AccountTransaction
         and decrement Account.current_balance by `amount`.

    Returns (expense, txn).  `txn` is None when no account_id was given.

    Raises ValueError for:
      - account_id provided but account not found
      - account_id provided but payment_method omitted
    """
    if account_id and not payment_method:
        raise ValueError("payment_method is required when account_id is provided")

    # ── 1. Create the expense ──────────────────────────────────────────────────
    expense = Expense(
        amount          = amount,
        description     = description,
        expense_date    = expense_date,
        category_id     = category_id,
        business_id     = business_id,
        branch_id       = branch_id,
        created_by_id   = created_by_id,
        status          = status,
        is_kra_declared = is_kra_declared,
        receipt_path    = receipt_path,
    )
    db.add(expense)
    await db.flush()   # assign expense.id before linking it to the transaction

    txn: AccountTransaction | None = None

    # ── 2. Create the ledger entry and update the account balance ──────────────
    if account_id:
        acct = await _get_account_or_raise(db, account_id)

        txn = AccountTransaction(
            account_id       = account_id,
            expense_id       = expense.id,
            transaction_type = "OUTBOUND",
            payment_method   = payment_method,
            amount           = amount,
            reference_no     = reference_no,
            transaction_date = transaction_date or _now_utc(),
            logged_by_id     = created_by_id,
        )
        db.add(txn)

        # Decrement balance in-place
        acct.current_balance = float(acct.current_balance or 0) - amount

        logger.info(
            "record_expense_payment: expense=%s account=%s amount=%.2f "
            "new_balance=%.2f ref=%s",
            expense.id, account_id, amount, acct.current_balance, reference_no,
        )

    await db.commit()
    await db.refresh(expense)
    if txn:
        await db.refresh(txn)

    return expense, txn


# ═══════════════════════════════════════════════════════════════════════════════
# 2. post_transaction()
# ═══════════════════════════════════════════════════════════════════════════════

async def post_transaction(
    db:               AsyncSession,
    *,
    account_id:       uuid.UUID,
    transaction_type: str,           # 'INBOUND' | 'OUTBOUND'
    payment_method:   str,
    amount:           float,
    reference_no:     str | None       = None,
    expense_id:       uuid.UUID | None = None,
    invoice_id:       uuid.UUID | None = None,
    logged_by_id:     uuid.UUID | None = None,
    transaction_date: datetime | None  = None,
) -> AccountTransaction:
    """
    Post a freestanding transaction (no Expense record created).

    Typical use:
      - Recording an inbound customer payment against an invoice.
      - Manually correcting a balance.

    INBOUND  → current_balance += amount
    OUTBOUND → current_balance -= amount
    """
    if transaction_type not in ("INBOUND", "OUTBOUND"):
        raise ValueError("transaction_type must be 'INBOUND' or 'OUTBOUND'")

    acct = await _get_account_or_raise(db, account_id)

    txn = AccountTransaction(
        account_id       = account_id,
        transaction_type = transaction_type,
        payment_method   = payment_method,
        amount           = amount,
        reference_no     = reference_no,
        expense_id       = expense_id,
        invoice_id       = invoice_id,
        logged_by_id     = logged_by_id,
        transaction_date = transaction_date or _now_utc(),
    )
    db.add(txn)

    delta = amount if transaction_type == "INBOUND" else -amount
    acct.current_balance = float(acct.current_balance or 0) + delta

    logger.info(
        "post_transaction: %s %.2f → account=%s new_balance=%.2f ref=%s",
        transaction_type, amount, account_id, acct.current_balance, reference_no,
    )

    await db.commit()
    await db.refresh(txn)
    return txn


# ═══════════════════════════════════════════════════════════════════════════════
# 3. get_profit_report()
# ═══════════════════════════════════════════════════════════════════════════════

async def get_profit_report(
    db:           AsyncSession,
    *,
    period_start: date | None = None,
    period_end:   date | None = None,
) -> ProfitReport:
    """
    Calculate a full P&L report.

    Revenue  = sum of confirmed GRN order_totals (pulled from Branch.total_invoiced
               which is kept in sync on every GRN confirmation — no JOIN needed).

    Expenses = sum of Expense.amount for paid expenses in the period:
               • branch_expenses  → expenses WHERE branch_id IS NOT NULL
               • general_expenses → expenses WHERE branch_id IS NULL
                 (overheads: wages, pipes, tanks, office rent …)

    net_profit = total_revenue - branch_expenses - general_expenses
    """
    from app.db.models.business import Branch, Business  # avoid circular import

    # ── 1. Aggregate branch revenue from Branch.total_invoiced ────────────────
    branch_q = (
        select(
            Branch.id,
            Branch.branch_name,
            Branch.business_id,
            Branch.total_invoiced,
        )
    )
    branch_rows = (await db.execute(branch_q)).all()

    # ── 2. Aggregate branch-scoped expenses (paid only) ────────────────────────
    exp_q = (
        select(
            Expense.branch_id,
            func.sum(Expense.amount).label("total"),
            func.count(Expense.id).label("cnt"),
        )
        .where(Expense.status == "paid")
    )
    if period_start:
        exp_q = exp_q.where(Expense.expense_date >= period_start)
    if period_end:
        exp_q = exp_q.where(Expense.expense_date <= period_end)
    exp_q = exp_q.group_by(Expense.branch_id)
    expense_rows = (await db.execute(exp_q)).all()

    # Build lookup: branch_id → (total_expenses, count)
    branch_expense_map: dict[uuid.UUID | None, tuple[float, int]] = {
        row.branch_id: (float(row.total or 0), int(row.cnt or 0))
        for row in expense_rows
    }

    # ── 3. Load business names ─────────────────────────────────────────────────
    biz_ids = {row.business_id for row in branch_rows if row.business_id}
    biz_map: dict[uuid.UUID, str] = {}
    if biz_ids:
        biz_result = await db.execute(
            select(Business.id, Business.name).where(Business.id.in_(biz_ids))
        )
        biz_map = {row.id: row.name for row in biz_result.all()}

    # ── 4. Assemble per-branch lines ───────────────────────────────────────────
    branch_lines: list[BranchProfitLine] = []
    total_revenue = 0.0
    total_branch_exp = 0.0

    for b in branch_rows:
        revenue = float(b.total_invoiced or 0)
        exp, cnt = branch_expense_map.get(b.id, (0.0, 0))
        branch_lines.append(
            BranchProfitLine(
                branch_id     = b.id,
                branch_name   = b.branch_name,
                business_name = biz_map.get(b.business_id) if b.business_id else None,
                revenue       = revenue,
                expenses      = exp,
                gross_profit  = revenue - exp,
                expense_count = cnt,
            )
        )
        total_revenue    += revenue
        total_branch_exp += exp

    # ── 5. General (overhead) expenses — branch_id IS NULL ────────────────────
    gen_exp, _gen_cnt = branch_expense_map.get(None, (0.0, 0))

    # ── 6. Assemble final report ───────────────────────────────────────────────
    total_exp = total_branch_exp + gen_exp

    # Sort branches by gross_profit descending for quick visual scanning
    branch_lines.sort(key=lambda x: x.gross_profit, reverse=True)

    return ProfitReport(
        period_start     = period_start,
        period_end       = period_end,
        total_revenue    = total_revenue,
        total_expenses   = total_exp,
        branch_expenses  = total_branch_exp,
        general_expenses = gen_exp,
        net_profit       = total_revenue - total_exp,
        branches         = branch_lines,
        generated_at     = _now_utc(),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. get_kra_audit_export()
# ═══════════════════════════════════════════════════════════════════════════════

async def get_kra_audit_export(
    db:           AsyncSession,
    *,
    period_start: date | None = None,
    period_end:   date | None = None,
) -> list[KRAExportRow]:
    """
    Return all KRA-declared expenses with their receipt paths and linked
    transaction reference numbers.

    This is the "triple-check" export:
      • Expense record          → proof the cost was recorded in the system
      • receipt_path            → digital copy of the supplier receipt / invoice
      • AccountTransaction.reference_no → the matching bank / M-Pesa reference

    Suitable for direct download as JSON or CSV during a tax audit.
    """
    from app.db.models.business import Branch, Business  # avoid circular import

    q = (
        select(Expense)
        .options(
            joinedload(Expense.category),
            joinedload(Expense.business),
            joinedload(Expense.branch),
            joinedload(Expense.account_transaction),
        )
        .where(Expense.is_kra_declared == True)   # noqa: E712
    )
    if period_start:
        q = q.where(Expense.expense_date >= period_start)
    if period_end:
        q = q.where(Expense.expense_date <= period_end)
    q = q.order_by(Expense.expense_date.asc())

    expenses: Sequence[Expense] = (await db.execute(q)).scalars().all()

    rows: list[KRAExportRow] = []
    for exp in expenses:
        txn_ref = None
        if exp.account_transaction:
            txn_ref = exp.account_transaction.reference_no

        rows.append(
            KRAExportRow(
                expense_id    = exp.id,
                expense_date  = exp.expense_date,
                description   = exp.description,
                category      = exp.category.name if exp.category else None,
                amount        = float(exp.amount or 0),
                receipt_path  = exp.receipt_path,
                business_name = exp.business.name if exp.business else None,
                branch_name   = exp.branch.branch_name if exp.branch else None,
                reference_no  = txn_ref,
            )
        )

    logger.info(
        "kra_audit_export: %d rows returned (period %s → %s)",
        len(rows), period_start, period_end,
    )
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# 5. reconcile_account_balance()  [repair / admin tool]
# ═══════════════════════════════════════════════════════════════════════════════

async def reconcile_account_balance(
    db: AsyncSession,
    account_id: uuid.UUID,
) -> tuple[float, float]:
    """
    Recompute an account's current_balance from its full transaction ledger.

    Useful if the balance column somehow drifts (e.g. a migration bug or
    a manual DB edit that bypassed the service layer).

    Returns (old_balance, new_balance).
    """
    acct = await _get_account_or_raise(db, account_id)

    # Sum signed amounts: INBOUND positive, OUTBOUND negative
    result = await db.execute(
        select(
            func.coalesce(
                func.sum(
                    # Use SQL CASE since we can't call the Python property here
                    # SQLAlchemy expression equivalent of `signed_amount`
                    AccountTransaction.amount *
                    func.cast(
                        func.case(
                            (AccountTransaction.transaction_type == "INBOUND", 1),
                            else_=-1,
                        ),
                        type_=AccountTransaction.amount.type,
                    )
                ),
                0,
            ).label("recomputed")
        ).where(AccountTransaction.account_id == account_id)
    )
    recomputed = float(result.scalar_one())

    old_balance = float(acct.current_balance or 0)
    acct.current_balance = recomputed
    await db.commit()

    if abs(old_balance - recomputed) > 0.001:
        logger.warning(
            "reconcile_account_balance: account=%s drift detected "
            "old=%.2f recomputed=%.2f delta=%.2f",
            account_id, old_balance, recomputed, recomputed - old_balance,
        )
    else:
        logger.info(
            "reconcile_account_balance: account=%s balance confirmed at %.2f",
            account_id, recomputed,
        )

    return old_balance, recomputed