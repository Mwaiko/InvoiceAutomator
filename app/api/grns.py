"""
app/api/grns.py

POST   /grns/upload         – upload PDF or image, auto-extract only
GET    /grns                – list all GRNs (paginated)
GET    /grns/{id}           – get single GRN
POST   /grns/{id}/confirm   – operator confirms; resolves business/branch HERE,
                              then updates invoiced totals + creates eTIMS invoice
POST   /grns/{id}/reject    – reject a GRN

Changes vs previous version:
  • Celery removed — submit_to_etims is now scheduled via FastAPI BackgroundTasks.
  • upload_grn  → business/branch resolution REMOVED. Upload now only saves
    the file, extracts data, and sets status=extracted. business_id and
    branch_id remain NULL until confirmation.
  • confirm_grn → resolve_business_and_branch() is now called HERE, using the
    operator-reviewed confirmed_data (which contains the final store block).
    After resolution the rest of the flow (balance update, eTIMS invoice) is
    unchanged.
  • All routes now return GRNResponse.from_orm_grn(grn) so that
    uploaded_by_name and uploaded_by_email are always populated from the
    eager-loaded `uploaded_by` relationship (lazy="selectin" on GRN model).
  • list_grns uses joinedload(GRN.uploaded_by) as an explicit safety net so
    the relationship is always loaded even if the ORM default changes.
"""

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, PaginationDep, get_db
from app.core.exceptions import GRNLockedError, NotFoundError
from app.db.models.grn import GRN, GRNStatus
from app.db.models.etims_invoice import EtimsInvoice, EtimsStatus
from app.schemas.grn import GRNConfirmRequest, GRNRejectRequest, GRNResponse
from app.services import file_storage, grn_extractor
from app.services.business_resolver import (
    post_confirmation_update_balances,
    resolve_business_and_branch,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/grns", tags=["grns"])

LOCKED_STATUSES = {GRNStatus.confirmed, GRNStatus.invoiced}


# ── helpers ───────────────────────────────────────────────────────────────────

async def _get_grn_with_uploader(db: AsyncSession, grn_id: uuid.UUID) -> GRN:
    """Fetch a single GRN with the uploader relationship eager-loaded."""
    result = await db.execute(
        select(GRN)
        .options(joinedload(GRN.uploaded_by))
        .where(GRN.id == grn_id)
    )
    grn = result.scalar_one_or_none()
    if not grn:
        raise NotFoundError(f"GRN {grn_id} not found")
    return grn


# ── routes ────────────────────────────────────────────────────────────────────

@router.post("/upload", response_model=GRNResponse, status_code=201)
async def upload_grn(
    file: UploadFile = File(...),
    order_id: str | None = Form(None),
    user: CurrentUser = None,
    db: AsyncSession = Depends(get_db),
):
    storage_path, file_type = await file_storage.save_grn_upload(file)

    grn = GRN(
        original_filename=file.filename or "upload",
        storage_path=storage_path,
        file_type=file_type,
        status=GRNStatus.uploaded,
        uploaded_by_id=user.id if user else None,
    )
    db.add(grn)
    await db.flush()

    try:
        extracted = await grn_extractor.extract_grn(storage_path)
        if order_id:
            extracted["order_id"] = order_id
        grn.extracted_data = extracted
        grn.status         = GRNStatus.extracted
    except Exception as exc:
        grn.status           = GRNStatus.rejected
        grn.rejection_reason = f"Extraction failed: {exc}"
        await db.commit()
        raise HTTPException(status_code=422, detail=f"Could not extract GRN: {exc}")

    await db.commit()

    # Re-fetch with uploader so from_orm_grn() can resolve the name
    grn = await _get_grn_with_uploader(db, grn.id)
    return GRNResponse.from_orm_grn(grn)


@router.get("", response_model=list[GRNResponse])
async def list_grns(
    pagination: PaginationDep,
    status: GRNStatus | None = None,
    business_id: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = None,
):
    q = (
        select(GRN)
        .options(joinedload(GRN.uploaded_by))   # single extra join, not N+1
        .order_by(GRN.created_at.desc())
    )
    if status:
        q = q.where(GRN.status == status)
    if business_id:
        q = q.where(GRN.business_id == business_id)
    if branch_id:
        q = q.where(GRN.branch_id == branch_id)
    q = q.offset(pagination.offset).limit(pagination.limit)

    result = await db.execute(q)
    grns   = result.scalars().all()
    return [GRNResponse.from_orm_grn(g) for g in grns]


@router.get("/{grn_id}", response_model=GRNResponse)
async def get_grn(
    grn_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = None,
):
    grn = await _get_grn_with_uploader(db, grn_id)
    return GRNResponse.from_orm_grn(grn)


@router.post("/{grn_id}/confirm", response_model=GRNResponse)
async def confirm_grn(
    grn_id: uuid.UUID,
    body: GRNConfirmRequest,
    background_tasks: BackgroundTasks,          # ← replaces Celery queue
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = None,
):
    grn = await _get_grn_with_uploader(db, grn_id)
    if grn.status in LOCKED_STATUSES:
        raise GRNLockedError()

    confirmed_dict     = body.confirmed_data.to_storage_dict()
    grn.confirmed_data = confirmed_dict
    grn.invoice_no     = body.invoice_no
    grn.status         = GRNStatus.confirmed
    business_name: str | None = None
    branch_name:   str | None = None

    try:
        business, branch = await resolve_business_and_branch(db, confirmed_dict)
        grn.business_id = business.id if business else None
        grn.branch_id   = branch.id   if branch   else None
        business_name   = business.name            if business else None
        branch_name     = branch.branch_name       if branch   else None
    except Exception as exc:
        logger.warning(
            "confirm_grn: could not resolve business/branch for GRN %s: %s",
            grn.id, exc,
        )

    order_total = body.confirmed_data.order_total
    if order_total and grn.business_id and grn.branch_id:
        from app.db.models.business import Business as BusinessModel, Branch
        business_obj = await db.get(BusinessModel, grn.business_id)
        branch_obj   = await db.get(Branch, grn.branch_id)
        if business_obj and branch_obj:
            await post_confirmation_update_balances(db, business_obj, branch_obj, order_total)

    etims_inv = EtimsInvoice(
        grn_id          = grn.id,
        status          = EtimsStatus.pending,
        submitted_by_id = user.id if user else None,
        business_id     = grn.business_id,
        branch_id       = grn.branch_id,
        business_name   = business_name,
        branch_name     = branch_name,
        invoice_amount  = order_total,
        amount_paid     = 0,
    )
    db.add(etims_inv)
    await db.flush()

    await db.commit()

    # Re-fetch to get refreshed state + uploader still loaded
    grn = await _get_grn_with_uploader(db, grn.id)

    # ── Schedule eTIMS submission as a background task (no Celery) ────────────
    from app.workers.etims_tasks import submit_to_etims
    background_tasks.add_task(submit_to_etims, str(grn.id), str(etims_inv.id))

    return GRNResponse.from_orm_grn(grn)


@router.post("/{grn_id}/reject", response_model=GRNResponse)
async def reject_grn(
    grn_id: uuid.UUID,
    body: GRNRejectRequest,
    db: AsyncSession = Depends(get_db),
    _user: CurrentUser = None,
):
    grn = await _get_grn_with_uploader(db, grn_id)
    if grn.status in LOCKED_STATUSES:
        raise GRNLockedError()

    grn.status           = GRNStatus.rejected
    grn.rejection_reason = body.reason
    await db.commit()

    grn = await _get_grn_with_uploader(db, grn.id)
    return GRNResponse.from_orm_grn(grn)