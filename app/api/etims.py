"""
app/api/routes/etims.py

GET    /etims-invoices                     – list invoices (filter by status, business, branch)
GET    /etims-invoices/summary             – AR rollup per business
GET    /etims-invoices/{id}                – get single invoice
POST   /etims-invoices/{id}/retry          – re-queue a rejected invoice
PATCH  /etims-invoices/{id}/payment        – record an incremental payment amount
PATCH  /etims-invoices/{id}/payment-status – force-override payment status
"""

import asyncio
import uuid
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AccountantOrAdmin, CurrentUser, PaginationDep, get_db
from app.core.exceptions import NotFoundError
from app.db.models.etims_invoice import EtimsInvoice, EtimsStatus, PaymentStatus
from app.schemas.etims import (
    EtimsInvoiceResponse,
    EtimsPaymentSummary,
    ManualPaymentStatusRequest,
    RecordInvoicePaymentRequest,
)

router = APIRouter(prefix="/etims-invoices", tags=["etims"])


# ── Shared helper ─────────────────────────────────────────────────────────────

async def _apply_payment_delta(
    db: AsyncSession,
    inv: EtimsInvoice,
    delta: float,
) -> None:
    """
    Add *delta* to total_paid on the Business and Branch linked to *inv*.

    Both payment endpoints call this so that the AR rollup is always consistent.
    delta is positive when money is received, negative when a payment is reversed.
    A delta of zero is a no-op and skips the DB load entirely.
    """
    if delta == 0:
        return

    if inv.business_id:
        from app.db.models.business import Business as BusinessModel
        biz = await db.get(BusinessModel, inv.business_id)
        if biz:
            biz.total_paid = float(biz.total_paid or 0) + delta

    if inv.branch_id:
        from app.db.models.business import Branch
        branch = await db.get(Branch, inv.branch_id)
        if branch:
            branch.total_paid = float(branch.total_paid or 0) + delta


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=List[EtimsInvoiceResponse])
async def list_invoices(
    pagination: PaginationDep,
    _user: CurrentUser,
    status: Optional[EtimsStatus]           = None,
    payment_status: Optional[PaymentStatus] = None,
    business_id: Optional[uuid.UUID]        = None,
    branch_id:   Optional[uuid.UUID]        = None,
    db: AsyncSession = Depends(get_db),
):
    q = select(EtimsInvoice).order_by(EtimsInvoice.created_at.desc())

    if status:
        q = q.where(EtimsInvoice.status == status)
    if payment_status:
        q = q.where(EtimsInvoice.payment_status == payment_status)
    if business_id:
        q = q.where(EtimsInvoice.business_id == business_id)
    if branch_id:
        q = q.where(EtimsInvoice.branch_id == branch_id)

    q = q.offset(pagination.offset).limit(pagination.limit)
    result = await db.execute(q)
    return result.scalars().all()


# ── AR payment summary per business ──────────────────────────────────────────

@router.get("/summary", response_model=List[EtimsPaymentSummary])
async def payment_summary(
    _user: AccountantOrAdmin,
    business_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Returns one row per business showing total invoiced, total paid,
    and outstanding amount — the accounts-receivable overview.
    """
    q = (
        select(
            EtimsInvoice.business_id,
            EtimsInvoice.business_name,
            func.sum(EtimsInvoice.invoice_amount).label("total_invoiced"),
            func.sum(EtimsInvoice.amount_paid).label("total_paid"),
            func.count(EtimsInvoice.id).label("invoice_count"),
            func.count(EtimsInvoice.id).filter(
                EtimsInvoice.payment_status == PaymentStatus.pending
            ).label("unpaid_count"),
        )
        .where(EtimsInvoice.business_id.is_not(None))
        .group_by(EtimsInvoice.business_id, EtimsInvoice.business_name)
        .order_by(EtimsInvoice.business_name)
    )
    if business_id:
        q = q.where(EtimsInvoice.business_id == business_id)

    result = await db.execute(q)
    rows = result.all()

    return [
        EtimsPaymentSummary(
            business_id=row.business_id,
            business_name=row.business_name or "",
            total_invoiced=float(row.total_invoiced or 0),
            total_paid=float(row.total_paid or 0),
            outstanding_amount=float(row.total_invoiced or 0) - float(row.total_paid or 0),
            invoice_count=row.invoice_count,
            unpaid_count=row.unpaid_count,
        )
        for row in rows
    ]


# ── Get single ────────────────────────────────────────────────────────────────

@router.get("/{invoice_id}", response_model=EtimsInvoiceResponse)
async def get_invoice(
    invoice_id: uuid.UUID,
    _user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    inv = await db.get(EtimsInvoice, invoice_id)
    if not inv:
        raise NotFoundError(f"eTIMS Invoice {invoice_id} not found")
    return inv


# ── Stream KRA receipt PDF ────────────────────────────────────────────────────

@router.get("/{invoice_id}/pdf")
async def download_invoice_pdf(
    invoice_id: uuid.UUID,
    _user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    """
    Stream the KRA receipt PDF for this invoice.

    Uses the popup POST method (popupTrnsSalesReceiptPDF) which is the only
    endpoint that reliably returns a real PDF.  The kra_invoice_no stored on
    the EtimsInvoice row is the sequential integer receipt ID assigned by KRA
    (e.g. "440").  That integer is passed directly as invcNo + curRcptNo in
    the popup form.
    """
    import os
    import tempfile
    from pathlib import Path
    from fastapi.responses import FileResponse
    from app.services.fill_kra import EtimsConfig, KraError, login, _make_session
    from app.services.download_invoice import download_pdf_via_popup

    inv = await db.get(EtimsInvoice, invoice_id)
    if not inv:
        raise NotFoundError(f"eTIMS Invoice {invoice_id} not found")

    if not inv.kra_invoice_no:
        raise HTTPException(
            status_code=400,
            detail=(
                "Invoice does not have a KRA receipt number yet. "
                "Wait for the submission to complete before downloading the receipt."
            ),
        )

    raw_no = str(inv.kra_invoice_no).strip()
    if "/" in raw_no:
        internal_id = raw_no.rsplit("/", 1)[-1].strip()
    else:
        internal_id = raw_no

    if not internal_id.isdigit():
        raise HTTPException(
            status_code=400,
            detail=(
                f"Stored kra_invoice_no {raw_no!r} does not contain a valid "
                "integer receipt ID — cannot download PDF."
            ),
        )

    cfg = EtimsConfig(
        pin      = os.environ["KRA_PIN"],
        branch   = os.environ.get("KRA_BRANCH", "001"),
        username = os.environ["KRA_USERNAME"],
        password = os.environ["KRA_PASSWORD"],
    )

    session = _make_session()
    try:
        await asyncio.to_thread(login, cfg, session)
    except KraError as exc:
        raise HTTPException(status_code=502, detail=f"KRA login failed: {exc}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to authenticate with KRA: {exc}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="etims_pdf_"))
    try:
        pdf_path = await asyncio.to_thread(
            download_pdf_via_popup, session, cfg, internal_id, tmp_dir
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"PDF download from KRA failed: {exc}")

    if pdf_path is None:
        raise HTTPException(
            status_code=502,
            detail=(
                f"KRA did not return a PDF for receipt ID {internal_id}. "
                "The receipt may not be finalised yet — try again in a few minutes."
            ),
        )

    safe_name = internal_id.replace("/", "_").replace("\\", "_")
    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"Receipt_{safe_name}.pdf",
        headers={"Content-Disposition": f'attachment; filename="Receipt_{safe_name}.pdf"'},
        background=None,
    )


# ── Retry rejected ────────────────────────────────────────────────────────────

@router.post("/{invoice_id}/retry", response_model=EtimsInvoiceResponse)
async def retry_invoice(
    invoice_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    _user: AccountantOrAdmin,
    db: AsyncSession = Depends(get_db),
):
    inv = await db.get(EtimsInvoice, invoice_id)
    if not inv:
        raise NotFoundError(f"eTIMS Invoice {invoice_id} not found")

    if inv.status != EtimsStatus.rejected:
        raise HTTPException(
            status_code=409,
            detail=f"Only rejected invoices can be retried. Current status: {inv.status}",
        )

    inv.status      = EtimsStatus.pending
    inv.retry_count = 0

    await db.commit()
    await db.refresh(inv)

    from app.workers.etims_tasks import submit_to_etims
    background_tasks.add_task(submit_to_etims, str(inv.grn_id), str(inv.id))

    return inv


# ── Record an incremental payment ─────────────────────────────────────────────

@router.patch("/{invoice_id}/payment", response_model=EtimsInvoiceResponse)
async def record_payment(
    invoice_id: uuid.UUID,
    payload: RecordInvoicePaymentRequest,
    _user: AccountantOrAdmin,
    db: AsyncSession = Depends(get_db),
):
    """
    Record a partial or full payment received against this invoice.

    Flow:
      1. Snapshot amount_paid before the mutation.
      2. Add payload.amount then call recalculate_payment_status(), which
         clamps amount_paid ≤ invoice_amount and sets payment_status.
      3. Delta = new amount_paid − snapshot. Using the post-clamp figure
         means we never over-credit Business/Branch even if the caller sends
         more than the outstanding balance.
      4. Apply that exact delta to Business.total_paid and Branch.total_paid
         via _apply_payment_delta().
    """
    inv = await db.get(EtimsInvoice, invoice_id)
    if not inv:
        raise NotFoundError(f"eTIMS Invoice {invoice_id} not found")

    if inv.payment_status == PaymentStatus.paid:
        raise HTTPException(
            status_code=409,
            detail="Invoice is already fully paid. Use the payment-status override endpoint if needed.",
        )

    amount_before = float(inv.amount_paid or 0)

    inv.amount_paid = amount_before + payload.amount
    inv.recalculate_payment_status()  # clamps amount_paid, sets payment_status

    # Post-clamp delta — this is the amount actually credited
    delta = float(inv.amount_paid) - amount_before

    await _apply_payment_delta(db, inv, delta)

    await db.commit()
    await db.refresh(inv)
    return inv


# ── Force-override payment status ─────────────────────────────────────────────

@router.patch("/{invoice_id}/payment-status", response_model=EtimsInvoiceResponse)
async def update_payment_status(
    invoice_id: uuid.UUID,
    payload: ManualPaymentStatusRequest,
    _user: AccountantOrAdmin,
    db: AsyncSession = Depends(get_db),
):
    """
    Manually override payment_status AND keep amount_paid, Business.total_paid,
    and Branch.total_paid all consistent in a single transaction.

    Flow:
      1. Snapshot amount_paid before the mutation.
      2. Set payment_status then call sync_from_status(), which forces:
           paid    → amount_paid = invoice_amount
           pending → amount_paid = 0
      3. Delta = new amount_paid − snapshot.
         Positive when marking paid (money credited).
         Negative when reversing to pending (correction / over-payment).
      4. Apply delta to Business.total_paid and Branch.total_paid.

    Previously this endpoint only called sync_from_status() but never touched
    Business or Branch totals, so the AR rollup always drifted after a status
    override.
    """
    inv = await db.get(EtimsInvoice, invoice_id)
    if not inv:
        raise NotFoundError(f"eTIMS Invoice {invoice_id} not found")

    # No-op guard — nothing to do, no DB writes needed
    if inv.payment_status == payload.payment_status:
        return inv

    amount_before = float(inv.amount_paid or 0)

    inv.payment_status = payload.payment_status
    inv.sync_from_status()  # amount_paid = invoice_amount (paid) or 0 (pending)

    delta = float(inv.amount_paid) - amount_before

    await _apply_payment_delta(db, inv, delta)

    await db.commit()
    await db.refresh(inv)
    return inv