"""
app/workers/finance_worker.py

Background tasks that run after HTTP responses have been sent.
FastAPI's BackgroundTasks dispatches these — they receive a raw DB session
from get_async_session() because they run outside the request lifecycle.

Tasks
─────
  update_branch_revenue_snapshot()  – (re)sets Branch.total_invoiced after a
                                      GRN is confirmed; called by grns.py too.

  flag_undeclared_expenses()        – scans expenses that are paid but have a
                                      receipt_path yet is_kra_declared=False;
                                      logs a warning per expense so your log
                                      aggregator (e.g. Datadog) can alert.

  snapshot_daily_profit()           – generates and logs a daily ProfitReport
                                      so you have a timestamped trail without
                                      storing a separate table.

  post_inbound_payment_transaction() – called after an eTIMS invoice is marked
                                       PaymentStatus.paid; creates an INBOUND
                                       AccountTransaction so cash actually
                                       shows up in the account ledger.
"""

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── shared session factory ─────────────────────────────────────────────────────
# Import lazily inside each task to avoid circular imports at module load time.


def _today() -> date:
    return datetime.now(timezone.utc).date()


# ═══════════════════════════════════════════════════════════════════════════════
# Task 1 – update_branch_revenue_snapshot
# ═══════════════════════════════════════════════════════════════════════════════

async def update_branch_revenue_snapshot(
    branch_id:   str,   # str because BackgroundTasks serialises args
    delta_amount: float,
    db: AsyncSession,
) -> None:
    """
    Increment Branch.total_invoiced by `delta_amount` after a GRN is confirmed.

    This is called from grns.py after post_confirmation_update_balances() so
    the branch revenue figure is always current without a full aggregate query.

    Also increments the parent Business.total_invoiced by the same delta so the
    business-level outstanding balance stays in sync.
    """
    from app.db.models.business import Branch, Business  # avoid circular import

    try:
        bid = uuid.UUID(branch_id)
        branch = await db.get(Branch, bid)
        if not branch:
            logger.warning("update_branch_revenue_snapshot: branch %s not found", branch_id)
            return

        branch.total_invoiced = float(branch.total_invoiced or 0) + delta_amount
        logger.info(
            "update_branch_revenue_snapshot: branch=%s +%.2f → total_invoiced=%.2f",
            branch_id, delta_amount, branch.total_invoiced,
        )

        # Mirror to the parent business
        business = await db.get(Business, branch.business_id)
        if business:
            business.total_invoiced = float(business.total_invoiced or 0) + delta_amount
            logger.info(
                "update_branch_revenue_snapshot: business=%s +%.2f → total_invoiced=%.2f",
                business.id, delta_amount, business.total_invoiced,
            )

        await db.commit()

    except Exception as exc:  # pragma: no cover
        logger.exception("update_branch_revenue_snapshot failed: %s", exc)
        await db.rollback()


# ═══════════════════════════════════════════════════════════════════════════════
# Task 2 – flag_undeclared_expenses
# ═══════════════════════════════════════════════════════════════════════════════

async def flag_undeclared_expenses(db: AsyncSession) -> int:
    """
    Find every paid expense that has a receipt_path but is_kra_declared=False.

    Logs one WARNING line per expense so your log aggregator can create an alert.
    Returns the count of flagged rows (useful in tests).

    Recommended: schedule this nightly via a cron job or APScheduler.
    """
    from app.db.models.finance import Expense  # avoid circular import

    q = (
        select(Expense)
        .where(
            Expense.status == "paid",
            Expense.receipt_path.isnot(None),
            Expense.is_kra_declared == False,   # noqa: E712
        )
        .order_by(Expense.expense_date.asc())
    )
    rows = (await db.execute(q)).scalars().all()

    for exp in rows:
        logger.warning(
            "KRA_UNDECLARED_EXPENSE | id=%s date=%s amount=%.2f "
            "description=%r receipt_path=%s",
            exp.id,
            exp.expense_date,
            float(exp.amount or 0),
            exp.description[:60],
            exp.receipt_path,
        )

    if rows:
        logger.warning(
            "flag_undeclared_expenses: %d paid expense(s) have receipts "
            "but have NOT been declared to KRA. "
            "Run PATCH /expenses/{id} with is_kra_declared=true once submitted.",
            len(rows),
        )
    else:
        logger.info("flag_undeclared_expenses: all paid expenses with receipts are KRA-declared ✓")

    return len(rows)


# ═══════════════════════════════════════════════════════════════════════════════
# Task 3 – snapshot_daily_profit
# ═══════════════════════════════════════════════════════════════════════════════

async def snapshot_daily_profit(db: AsyncSession) -> None:
    """
    Generate a ProfitReport for yesterday and emit it as structured INFO logs.

    Why logs and not a DB table?
      • Zero schema migration cost.
      • Any log aggregator (Datadog, Loki, CloudWatch) can store and chart them.
      • Add a 'profit_snapshots' table later if you want queryable history.

    Recommended: call this from a nightly scheduled task (APScheduler / cron).
    """
    from app.services.finance_service import get_profit_report  # avoid circular import

    yesterday = _today() - timedelta(days=1)

    try:
        report = await get_profit_report(
            db,
            period_start=yesterday,
            period_end=yesterday,
        )

        logger.info(
            "DAILY_PROFIT_SNAPSHOT | date=%s revenue=%.2f expenses=%.2f "
            "net_profit=%.2f branches=%d general_expenses=%.2f",
            yesterday,
            report.total_revenue,
            report.total_expenses,
            report.net_profit,
            len(report.branches),
            report.general_expenses,
        )

        for line in report.branches:
            logger.info(
                "DAILY_PROFIT_BRANCH | date=%s branch=%s business=%s "
                "revenue=%.2f expenses=%.2f gross_profit=%.2f",
                yesterday,
                line.branch_name,
                line.business_name or "—",
                line.revenue,
                line.expenses,
                line.gross_profit,
            )

    except Exception as exc:  # pragma: no cover
        logger.exception("snapshot_daily_profit failed: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# Task 4 – post_inbound_payment_transaction
# ═══════════════════════════════════════════════════════════════════════════════

async def post_inbound_payment_transaction(
    invoice_id:     str,    # etims_invoices.id
    account_id:     str,    # which account received the money
    amount:         float,
    payment_method: str,
    reference_no:   str | None,
    logged_by_id:   str | None,
    db:             AsyncSession,
) -> None:
    """
    Called as a background task after an eTIMS invoice's payment_status
    transitions to 'paid'.

    Creates an INBOUND AccountTransaction so the cash shows up in the account
    ledger and Account.current_balance is updated accordingly.

    The "triple-check" audit trail is now:
      EtimsInvoice (what we billed)
      → AccountTransaction.invoice_id (the ledger entry)
      → AccountTransaction.reference_no (the M-Pesa / bank ref)
    """
    from app.services.finance_service import post_transaction  # avoid circular import

    try:
        inv_uuid   = uuid.UUID(invoice_id)
        acct_uuid  = uuid.UUID(account_id)
        lby_uuid   = uuid.UUID(logged_by_id) if logged_by_id else None

        txn = await post_transaction(
            db,
            account_id       = acct_uuid,
            transaction_type = "INBOUND",
            payment_method   = payment_method,
            amount           = amount,
            reference_no     = reference_no,
            invoice_id       = inv_uuid,
            logged_by_id     = lby_uuid,
        )

        logger.info(
            "post_inbound_payment_transaction: created txn=%s "
            "invoice=%s account=%s amount=%.2f ref=%s",
            txn.id, invoice_id, account_id, amount, reference_no,
        )

    except Exception as exc:  # pragma: no cover
        logger.exception(
            "post_inbound_payment_transaction failed for invoice=%s: %s",
            invoice_id, exc,
        )
        await db.rollback()


# ═══════════════════════════════════════════════════════════════════════════════
# Task 5 – reconcile_all_accounts  [admin / repair utility]
# ═══════════════════════════════════════════════════════════════════════════════

async def reconcile_all_accounts(db: AsyncSession) -> dict[str, float]:
    """
    Run reconcile_account_balance() for every account and return a dict of
    {account_id: drift} for any that were out of sync.

    Recommended: run once after a data migration or schema change.
    """
    from app.db.models.finance import Account  # avoid circular import
    from app.services.finance_service import reconcile_account_balance

    accounts = (await db.execute(select(Account))).scalars().all()
    drifts: dict[str, float] = {}

    for acct in accounts:
        old, new = await reconcile_account_balance(db, acct.id)
        drift = round(new - old, 2)
        if abs(drift) > 0.001:
            drifts[str(acct.id)] = drift
            logger.warning(
                "reconcile_all_accounts: account=%s (%s) drift=%.2f",
                acct.id, acct.account_name, drift,
            )

    logger.info(
        "reconcile_all_accounts: checked %d accounts, %d had drift",
        len(accounts), len(drifts),
    )
    return drifts