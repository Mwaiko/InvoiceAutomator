"""
app/workers/etims_tasks.py

Celery task that reads a confirmed GRN, builds the eTIMS payload,
converts it to a ReceiptHeader, and submits it to the KRA portal.

FIX 6 (was missing entirely): this is the bridge that calls
  build_etims_payload()  →  invoice_dict_to_receipt()  →  run_fill()
so that the mapper output (plain dicts) is correctly transformed into
the ReceiptHeader / SaleItem dataclasses that fill_kra expects.
"""

import logging
import os
import uuid

from celery import shared_task
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.etims_invoice import EtimsInvoice, EtimsStatus
from app.db.models.grn import GRN
from app.db.session import SyncSessionLocal          # use a *sync* session in Celery
from app.services.etims_mapper import build_etims_payload
from app.services.fill_kra import EtimsConfig, KraError, invoice_dict_to_receipt, run_fill

logger = get_logger(__name__)


def _get_etims_cfg() -> EtimsConfig:
    """Build EtimsConfig from environment variables."""
    return EtimsConfig(
        pin      = os.environ["KRA_PIN"],
        branch   = os.environ.get("KRA_BRANCH",   "001"),
        username = os.environ["KRA_USERNAME"],
        password = os.environ["KRA_PASSWORD"],
    )


@shared_task(
    bind=True,
    name="etims.submit_to_etims",
    max_retries=3,
    default_retry_delay=60,
    queue="etims",
)
def submit_to_etims(self, grn_id: str, etims_invoice_id: str) -> dict:
    """
    1. Load the confirmed GRN and its associated EtimsInvoice from the DB.
    2. Build the eTIMS payload via etims_mapper.build_etims_payload().
    3. Convert to ReceiptHeader via fill_kra.invoice_dict_to_receipt()  ← FIX 6
    4. Submit to KRA portal via fill_kra.run_fill().
    5. Update EtimsInvoice.status to accepted / rejected.
    """
    grn_uuid  = uuid.UUID(grn_id)
    inv_uuid  = uuid.UUID(etims_invoice_id)

    with SyncSessionLocal() as db:
        grn = db.get(GRN, grn_uuid)
        if not grn:
            logger.error("submit_to_etims: GRN %s not found", grn_id)
            return {"error": f"GRN {grn_id} not found"}

        inv = db.get(EtimsInvoice, inv_uuid)
        if not inv:
            logger.error("submit_to_etims: EtimsInvoice %s not found", etims_invoice_id)
            return {"error": f"EtimsInvoice {etims_invoice_id} not found"}

        # ── Build mapper payload ──────────────────────────────────────────────
        try:
            invoice_header, items_list, _meta = build_etims_payload(
                confirmed_data = grn.confirmed_data,
                invoice_no     = grn.invoice_no or "",
                business_id    = grn.business_id,
                branch_id      = grn.branch_id,
                business_name  = inv.business_name,
                branch_name    = inv.branch_name,
            )
        except ValueError as exc:
            logger.error("submit_to_etims: payload build failed for GRN %s: %s", grn_id, exc)
            inv.status          = EtimsStatus.rejected
            inv.rejection_reason = str(exc)
            db.commit()
            return {"error": str(exc)}

        # ── FIX 6: convert mapper dicts → ReceiptHeader / SaleItem objects ────
        try:
            receipt_header = invoice_dict_to_receipt(invoice_header, items_list)
        except ValueError as exc:
            logger.error("submit_to_etims: receipt conversion failed for GRN %s: %s", grn_id, exc)
            inv.status           = EtimsStatus.rejected
            inv.rejection_reason = str(exc)
            db.commit()
            return {"error": str(exc)}

        # ── Submit to KRA ─────────────────────────────────────────────────────
        cfg = _get_etims_cfg()
        try:
            results = run_fill(cfg, receipt_header)
        except KraError as exc:
            logger.error("submit_to_etims: KRA submission failed for GRN %s: %s", grn_id, exc)
            inv.status           = EtimsStatus.rejected
            inv.rejection_reason = str(exc)
            inv.retry_count      = (inv.retry_count or 0) + 1
            db.commit()
            # Retry via Celery if we haven't exceeded max_retries
            raise self.retry(exc=exc)

        # ── Persist result ────────────────────────────────────────────────────
        first_result = results[0] if results else {}
        if first_result.get("status") == "ok":
            inv.status   = EtimsStatus.accepted
            inv.kra_response = first_result.get("response")
        else:
            inv.status           = EtimsStatus.rejected
            inv.rejection_reason = first_result.get("error", "unknown error")
            inv.retry_count      = (inv.retry_count or 0) + 1

        db.commit()
        logger.info(
            "submit_to_etims: GRN %s → eTIMS %s  status=%s",
            grn_id, etims_invoice_id, inv.status,
        )
        return first_result