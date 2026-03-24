"""
app/api/grns.py

POST   /grns/upload               – upload PDF or image, auto-extract only
POST   /grns/from-order/{id}      – create a GRN directly from an existing order
                                    (no file upload; extracted_data is built from
                                    the order's fields and sent straight to the
                                    confirm screen with status=extracted)
GET    /grns                      – list all GRNs (paginated)
GET    /grns/{id}                 – get single GRN
POST   /grns/{id}/confirm         – operator confirms; resolves business/branch HERE,
                                    then updates invoiced totals + creates eTIMS invoice
POST   /grns/{id}/reject          – reject a GRN
"""

import logging
import uuid
from datetime import date

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


def _order_items_to_grn_items(items) -> list:
    """
    Convert order line items (either ORM objects or dicts) to the
    GRN extracted_data items format expected by GrnItem.fromJson().
    Returns an empty list if the order has no items.
    """
    if not items:
        return []
    result = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            qty   = float(item.get("quantity") or item.get("qty") or 0)
            price = float(item.get("unit_price") or item.get("price") or 0)
            result.append({
                "id":           str(item.get("id") or i + 1),
                "description":  item.get("description") or item.get("name") or f"Item {i + 1}",
                "qty_received": qty,
                "uom":          item.get("unit") or item.get("uom") or "PCS",
                "unit_price":   price,
                "net_amount":   float(item.get("net_amount") or item.get("total") or qty * price),
                **({"item_code": item["item_code"]} if item.get("item_code") else {}),
            })
        else:
            # ORM object with attribute access
            qty   = float(getattr(item, "quantity",   0) or 0)
            price = float(getattr(item, "unit_price", 0) or 0)
            result.append({
                "id":           str(getattr(item, "id", i + 1)),
                "description":  getattr(item, "description", None) or f"Item {i + 1}",
                "qty_received": qty,
                "uom":          getattr(item, "unit", None) or getattr(item, "uom", None) or "PCS",
                "unit_price":   price,
                "net_amount":   float(
                    getattr(item, "net_amount", None)
                    or getattr(item, "total_price", None)
                    or qty * price
                ),
                **({"item_code": item.item_code} if getattr(item, "item_code", None) else {}),
            })
    return result


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
    grn = await _get_grn_with_uploader(db, grn.id)
    return GRNResponse.from_orm_grn(grn)


@router.post("/from-order/{order_id}", response_model=GRNResponse, status_code=201)
async def create_grn_from_order(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = None,
):
    """
    Create a GRN directly from an existing order — no file upload required.

    The order's fields are mapped into `extracted_data` using the same schema
    that the GRN extractor produces, so the confirm screen can pre-fill every
    section without any modifications.  The GRN is created with
    status=extracted so the operator can review and edit before confirming.
    """
    from app.db.models.order import Order

    order = await db.get(Order, order_id)
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    # Resolve order total — try a few common attribute names defensively
    raw_total = (
        getattr(order, "order_total", None)
        or getattr(order, "total_amount", None)
        or getattr(order, "total", None)
        or 0
    )
    order_total = float(raw_total)

    # Build the same extracted_data shape the GRN extractor produces
    extracted_data: dict = {
        # GRN header
        "receipt_voucher_no":   order.order_number,
        "lpo_number":           getattr(order, "lpo_number", None),
        "delivery_invoice_no":  None,
        "receipt_date":         date.today().isoformat(),

        # Link back to the source order so the frontend can display it
        "order_id":             str(order.id),

        # Store block — mirrors GRNStoreBlock schema
        "store": {
            "company_name": getattr(order, "store_name", None),
            "store_name":   getattr(order, "store_name", None),
            "address":      getattr(order, "delivery_address", None),
            "location":     getattr(order, "delivery_location", None),
        },

        # Supplier block — mirrors GRNSupplierBlock schema
        "supplier": {
            "company_name": order.supplier_name,
            "email":        getattr(order, "supplier_email", None),
        },

        # Line items (empty list if the order model has none)
        "items": _order_items_to_grn_items(
            getattr(order, "items", None) or getattr(order, "line_items", None)
        ),

        # Totals
        "sub_total":   order_total,
        "vat":         0.0,
        "order_total": order_total,
    }

    grn = GRN(
        # No physical file for order-derived GRNs
        original_filename=f"ORDER-{order.order_number}",
        storage_path=None,
        file_type="order",
        status=GRNStatus.extracted,
        uploaded_by_id=user.id if user else None,
        extracted_data=extracted_data,
    )
    db.add(grn)
    await db.commit()

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
        .options(joinedload(GRN.uploaded_by))
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
    background_tasks: BackgroundTasks,
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

    grn = await _get_grn_with_uploader(db, grn.id)

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