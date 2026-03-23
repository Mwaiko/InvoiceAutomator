"""
app/workers/etims_tasks.py

Plain async function that reads a confirmed GRN, builds the eTIMS payload,
converts it to a ReceiptHeader, and submits it to the KRA portal.

Celery has been removed. This function is called via FastAPI BackgroundTasks,
so it runs in the same event loop as the request but after the response is sent.

Retry logic is handled internally with asyncio.sleep (3 attempts, 60 s apart).
"""

import asyncio
import os
import uuid

from app.core.logging import get_logger
from app.db.models.etims_invoice import EtimsInvoice, EtimsStatus
from app.db.models.grn import GRN
from app.db.session import AsyncSessionLocal          # async session — no Celery sync needed
from app.services.etims_mapper import build_etims_payload
from app.services.fill_kra import EtimsConfig, KraError, invoice_dict_to_receipt, run_fill

logger = get_logger(__name__)

MAX_RETRIES       = 3
RETRY_DELAY_SECS  = 60


def _get_etims_cfg() -> EtimsConfig:
    """Build EtimsConfig from environment variables."""
    return EtimsConfig(
        pin      = os.environ["KRA_PIN"],
        branch   = os.environ.get("KRA_BRANCH", "001"),
        username = os.environ["KRA_USERNAME"],
        password = os.environ["KRA_PASSWORD"],
    )


async def submit_to_etims(grn_id: str, etims_invoice_id: str) -> dict:
    """
    Background task (no Celery):

    1. Load the confirmed GRN and its EtimsInvoice from the DB.
    2. Build the eTIMS payload via etims_mapper.build_etims_payload().
    3. Convert to ReceiptHeader via fill_kra.invoice_dict_to_receipt().
    4. Submit to KRA portal via fill_kra.run_fill().
       Retries up to MAX_RETRIES times on KraError before giving up.
    5. Persist EtimsInvoice.status → accepted / rejected.
    """
    grn_uuid = uuid.UUID(grn_id)
    inv_uuid = uuid.UUID(etims_invoice_id)

    async with AsyncSessionLocal() as db:
        grn = await db.get(GRN, grn_uuid)
        if not grn:
            logger.error("submit_to_etims: GRN %s not found", grn_id)
            return {"error": f"GRN {grn_id} not found"}

        inv = await db.get(EtimsInvoice, inv_uuid)
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
            inv.status           = EtimsStatus.rejected
            inv.rejection_reason = str(exc)
            await db.commit()
            return {"error": str(exc)}

        # ── Convert mapper dicts → ReceiptHeader / SaleItem objects ──────────
        try:
            receipt_header = invoice_dict_to_receipt(invoice_header, items_list)
        except ValueError as exc:
            logger.error("submit_to_etims: receipt conversion failed for GRN %s: %s", grn_id, exc)
            inv.status           = EtimsStatus.rejected
            inv.rejection_reason = str(exc)
            await db.commit()
            return {"error": str(exc)}

        # ── Submit to KRA with retry loop ─────────────────────────────────────
        cfg         = _get_etims_cfg()
        last_error  = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # run_fill may be a blocking HTTP call — keep the event loop free
                results = await asyncio.to_thread(run_fill, cfg, receipt_header)
                last_error = None
                break
            except KraError as exc:
                last_error = exc
                inv.retry_count = (inv.retry_count or 0) + 1
                logger.warning(
                    "submit_to_etims: KRA attempt %d/%d failed for GRN %s: %s",
                    attempt, MAX_RETRIES, grn_id, exc,
                )
                if attempt < MAX_RETRIES:
                    await db.commit()          # persist incremented retry_count
                    await asyncio.sleep(RETRY_DELAY_SECS * attempt)

        if last_error is not None:
            logger.error(
                "submit_to_etims: all %d KRA attempts exhausted for GRN %s: %s",
                MAX_RETRIES, grn_id, last_error,
            )
            inv.status           = EtimsStatus.rejected
            inv.rejection_reason = str(last_error)
            await db.commit()
            return {"error": str(last_error)}

        # ── Persist result ────────────────────────────────────────────────────
        first_result = results[0] if results else {}
        if first_result.get("status") == "ok":
            inv.status       = EtimsStatus.accepted
            inv.kra_response = first_result.get("response")
        else:
            inv.status           = EtimsStatus.rejected
            inv.rejection_reason = first_result.get("error", "unknown error")
            inv.retry_count      = (inv.retry_count or 0) + 1

        await db.commit()
        logger.info(
            "submit_to_etims: GRN %s → eTIMS %s  status=%s",
            grn_id, etims_invoice_id, inv.status,
        )
        return first_result