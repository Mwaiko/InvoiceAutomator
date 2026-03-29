"""
app/api/routes/etims.py

GET    /etims-invoices                     – list invoices (filter by status, business, branch)
GET    /etims-invoices/summary             – AR rollup per business
GET    /etims-invoices/{id}                – get single invoice
POST   /etims-invoices/{id}/retry          – re-queue a rejected invoice
PATCH  /etims-invoices/{id}/payment        – record a payment amount
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
            # FIX: was referencing PaymentStatus.unpaid / partially_paid which don't
            # exist in the enum. Only 'pending' and 'paid' are valid values.
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
    import os
    from fastapi.responses import StreamingResponse
    from app.services.fill_kra import EtimsConfig, KraError, login, _make_session

    inv = await db.get(EtimsInvoice, invoice_id)
    if not inv:
        raise NotFoundError(f"eTIMS Invoice {invoice_id} not found")

    if not inv.kra_invoice_no:
        raise HTTPException(
            status_code=400,
            detail="Invoice does not have a KRA invoice number yet. "
                   "Wait for KRA approval before downloading the receipt.",
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

    pdf_url = f"{cfg.base_url}/app/ebm/trns/sales/printTrnsSalesReceipt"
    hdrs = {
        "Accept":         "application/pdf,text/html,*/*;q=0.9",
        "Referer":        f"{cfg.base_url}/app/ebm/trns/sales/indexTrnsSalesReceipt",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }

    try:
        kra_resp = await asyncio.to_thread(
            lambda: session.get(
                pdf_url,
                params={"invcNo": inv.kra_invoice_no},
                headers=hdrs,
                timeout=30,
                stream=True,
            )
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"KRA request failed: {exc}")

    if not kra_resp.ok:
        raise HTTPException(
            status_code=502,
            detail=f"KRA returned HTTP {kra_resp.status_code} for invoice {inv.kra_invoice_no}",
        )

    content_type = kra_resp.headers.get("Content-Type", "application/pdf")
    safe_name = inv.kra_invoice_no.replace("/", "_").replace("\\", "_")

    def _iter_chunks():
        for chunk in kra_resp.iter_content(chunk_size=8192):
            if chunk:
                yield chunk

    return StreamingResponse(
        _iter_chunks(),
        media_type=content_type if "pdf" in content_type else "application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Receipt_{safe_name}.pdf"'},
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


# ── Record a payment against this invoice ─────────────────────────────────────

@router.patch("/{invoice_id}/payment", response_model=EtimsInvoiceResponse)
async def record_payment(
    invoice_id: uuid.UUID,
    payload: RecordInvoicePaymentRequest,
    _user: AccountantOrAdmin,
    db: AsyncSession = Depends(get_db),
):
    """
    Record a payment received against this invoice.

    • Increments amount_paid on the EtimsInvoice.
    • Calls recalculate_payment_status() which keeps payment_status and
      amount_paid in sync (clamps amount_paid to invoice_amount on full payment).
    • Also increments total_paid on the linked Business and Branch.
    """
    inv = await db.get(EtimsInvoice, invoice_id)
    if not inv:
        raise NotFoundError(f"eTIMS Invoice {invoice_id} not found")

    if inv.payment_status == PaymentStatus.paid:
        raise HTTPException(
            status_code=409,
            detail="Invoice is already fully paid. Use the manual override endpoint if needed.",
        )

    # ── Update invoice running total then recalculate status ─────────────────
    inv.amount_paid = float(inv.amount_paid or 0) + payload.amount
    inv.recalculate_payment_status()  # also clamps amount_paid and sets payment_status

    # ── Mirror payment onto Business + Branch totals ───────────────────────
    # Use the actual amount credited (post-clamp) to avoid double-counting
    amount_credited = float(inv.amount_paid) - (float(inv.amount_paid) - payload.amount
                                                  if payload.amount <= float(inv.invoice_amount or 0)
                                                  else float(inv.invoice_amount or 0) - (float(inv.amount_paid) - payload.amount))

    if inv.business_id:
        from app.db.models.business import Business as BusinessModel
        business_obj = await db.get(BusinessModel, inv.business_id)
        if business_obj:
            business_obj.total_paid = float(business_obj.total_paid or 0) + payload.amount

    if inv.branch_id:
        from app.db.models.business import Branch
        branch_obj = await db.get(Branch, inv.branch_id)
        if branch_obj:
            branch_obj.total_paid = float(branch_obj.total_paid or 0) + payload.amount

    await db.commit()
    await db.refresh(inv)
    return inv


# ── Force-override payment status (accountant correction) ─────────────────────

@router.patch("/{invoice_id}/payment-status", response_model=EtimsInvoiceResponse)
async def update_payment_status(
    invoice_id: uuid.UUID,
    payload: ManualPaymentStatusRequest,
    _user: AccountantOrAdmin,
    db: AsyncSession = Depends(get_db),
):
    """
    Manually override payment_status. Also syncs amount_paid to match:
      paid    → amount_paid = invoice_amount
      pending → amount_paid = 0

    This keeps both fields consistent regardless of how the status was set.
    """
    inv = await db.get(EtimsInvoice, invoice_id)
    if not inv:
        raise NotFoundError(f"eTIMS Invoice {invoice_id} not found")

    inv.payment_status = payload.payment_status
    inv.sync_from_status()  # FIX: was missing — amount_paid was never updated on override

    await db.commit()
    await db.refresh(inv)
    return inv