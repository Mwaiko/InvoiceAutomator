"""
app/api/payments.py

POST   /payments                  – record a new payment
GET    /payments                  – list all payments (paginated, filterable)
GET    /payments/{id}             – get single payment
POST   /payments/{id}/confirm     – confirm a pending payment
POST   /payments/{id}/void        – void a payment (admin/accountant only)
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import PaginationDep, get_current_user, get_db
from app.core.exceptions import NotFoundError
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.user import UserRole
from app.schemas.payment import (
    PaymentConfirmRequest,
    PaymentCreateRequest,
    PaymentResponse,
    PaymentVoidRequest,
)

router = APIRouter(prefix="/payments", tags=["payments"])


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("", response_model=PaymentResponse, status_code=201)
async def create_payment(
    body: PaymentCreateRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """
    Record a payment. At least one of order_id or etims_invoice_id is required.
    Payment starts as 'pending' until confirmed.
    """
    # Validate linked records exist
    if body.order_id:
        from app.db.models.order import Order
        if not await db.get(Order, body.order_id):
            raise HTTPException(status_code=404, detail=f"Order {body.order_id} not found")

    if body.etims_invoice_id:
        from app.db.models.etims_invoice import EtimsInvoice
        if not await db.get(EtimsInvoice, body.etims_invoice_id):
            raise HTTPException(status_code=404, detail=f"eTIMS Invoice {body.etims_invoice_id} not found")

    payment = Payment(
        **body.model_dump(),
        created_by_id=user.id,
        status=PaymentStatus.pending,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    return payment


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[PaymentResponse])
async def list_payments(
    pagination: PaginationDep,
    status: PaymentStatus | None = None,
    order_id: uuid.UUID | None = None,
    etims_invoice_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    q = select(Payment).order_by(Payment.created_at.desc())
    if status:
        q = q.where(Payment.status == status)
    if order_id:
        q = q.where(Payment.order_id == order_id)
    if etims_invoice_id:
        q = q.where(Payment.etims_invoice_id == etims_invoice_id)
    q = q.offset(pagination.offset).limit(pagination.limit)

    result = await db.execute(q)
    return result.scalars().all()


# ── Get single ────────────────────────────────────────────────────────────────

@router.get("/{payment_id}", response_model=PaymentResponse)
async def get_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    payment = await db.get(Payment, payment_id)
    if not payment:
        raise NotFoundError(f"Payment {payment_id} not found")
    return payment


# ── Confirm ───────────────────────────────────────────────────────────────────

@router.post("/{payment_id}/confirm", response_model=PaymentResponse)
async def confirm_payment(
    payment_id: uuid.UUID,
    body: PaymentConfirmRequest,
    db: AsyncSession = Depends(get_db),
    _user=Depends(get_current_user),
):
    """
    Moves a payment from pending → confirmed.
    Optionally attaches a transaction ID / reference received from the bank.
    """
    payment = await db.get(Payment, payment_id)
    if not payment:
        raise NotFoundError(f"Payment {payment_id} not found")

    if payment.status != PaymentStatus.pending:
        raise HTTPException(
            status_code=409,
            detail=f"Only pending payments can be confirmed. Current status: {payment.status}",
        )

    payment.status = PaymentStatus.confirmed
    if body.transaction_id:
        payment.transaction_id = body.transaction_id
    if body.payment_reference:
        payment.payment_reference = body.payment_reference
    if body.notes:
        payment.notes = (payment.notes or "") + f"\n{body.notes}"

    await db.commit()
    await db.refresh(payment)
    return payment


# ── Reconcile ─────────────────────────────────────────────────────────────────

@router.post("/{payment_id}/reconcile", response_model=PaymentResponse)
async def reconcile_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """
    Moves confirmed → reconciled. Accountant / Admin only.
    Reconciliation means the payment has been matched against bank records.
    """
    if user.role not in (UserRole.admin, UserRole.accountant):
        raise HTTPException(status_code=403, detail="Accountant or Admin role required")

    payment = await db.get(Payment, payment_id)
    if not payment:
        raise NotFoundError(f"Payment {payment_id} not found")

    if payment.status != PaymentStatus.confirmed:
        raise HTTPException(
            status_code=409,
            detail=f"Only confirmed payments can be reconciled. Current status: {payment.status}",
        )

    payment.status = PaymentStatus.reconciled
    await db.commit()
    await db.refresh(payment)
    return payment


# ── Void ──────────────────────────────────────────────────────────────────────

@router.post("/{payment_id}/void", response_model=PaymentResponse)
async def void_payment(
    payment_id: uuid.UUID,
    body: PaymentVoidRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """
    Voids a payment. Admin / Accountant only.
    Reconciled payments cannot be voided — they are permanent financial records.
    """
    if user.role not in (UserRole.admin, UserRole.accountant):
        raise HTTPException(status_code=403, detail="Accountant or Admin role required")

    payment = await db.get(Payment, payment_id)
    if not payment:
        raise NotFoundError(f"Payment {payment_id} not found")

    if payment.status == PaymentStatus.reconciled:
        raise HTTPException(
            status_code=409,
            detail="Reconciled payments are permanent financial records and cannot be voided",
        )
    if payment.status == PaymentStatus.voided:
        raise HTTPException(status_code=409, detail="Payment is already voided")

    payment.status        = PaymentStatus.voided
    payment.voided_reason = body.reason
    await db.commit()
    await db.refresh(payment)
    return payment