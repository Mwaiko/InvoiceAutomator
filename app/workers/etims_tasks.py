"""
app/workers/etims_tasks.py

Plain async function that reads a confirmed GRN, builds the eTIMS payload,
converts it to a ReceiptHeader, and submits it to the KRA portal.

Celery has been removed. This function is called via FastAPI BackgroundTasks,
so it runs in the same event loop as the request but after the response is sent.

Retry logic is handled internally with asyncio.sleep (3 attempts, 60 s apart).

Development mode
────────────────
When the APP_ENV environment variable is set to "development" (case-insensitive),
the function skips the KRA portal entirely and instead renders a beautifully
formatted PDF preview of the payload to disk.  The EtimsInvoice row is stamped
with status=submitted and the local PDF path stored in kra_response so the rest
of the app behaves identically to production.  No network calls are made.
"""

import asyncio
import os
import uuid

from app.core.logging import get_logger
from app.db.models.etims_invoice import EtimsInvoice, EtimsStatus
from app.db.models.grn import GRN,GRNStatus
from app.db.session import AsyncSessionLocal          # async session — no Celery sync needed
from app.services.etims_mapper import build_etims_payload
from app.services.fill_kra import EtimsConfig, KraError, invoice_dict_to_receipt, run_fill
from dotenv import load_dotenv
from pathlib import Path
env_path = Path(__file__).resolve().parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path)
logger = get_logger(__name__)

MAX_RETRIES       = 3
RETRY_DELAY_SECS  = 60

# Directory where development-mode PDF previews are written.
# Override by setting DEV_RECEIPT_DIR in the environment.
DEV_RECEIPT_DIR = os.environ.get("DEV_RECEIPT_DIR", "./dev_receipts")


def _is_dev() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() == "development"


def _get_etims_cfg() -> EtimsConfig:
    return EtimsConfig(
        pin    = os.environ["KRA_PIN"],
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
    4a. [PRODUCTION]  Submit to KRA portal via fill_kra.run_fill().
        Retries up to MAX_RETRIES times on KraError before giving up.
    4b. [DEVELOPMENT] Render a local PDF preview instead of calling KRA.
        No network calls are made; the PDF path is stored in kra_response.
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

        try:
            invoice_header, items_list, meta = await build_etims_payload(
                confirmed_data = grn.confirmed_data,
                invoice_no     = grn.invoice_no,
                db             = db,
                business_id    = grn.business_id,
                branch_id      = grn.branch_id,
                business_name  = inv.business_name,
                branch_name    = inv.branch_name,
            )
        except ValueError as exc:
            logger.error("submit_to_etims: payload build failed for GRN %s: %s", grn_id, exc)
            inv.status        = EtimsStatus.rejected
            inv.error_message = str(exc)
            await db.commit()
            return {"error": str(exc)}

        # ── Lock in the invoice number BEFORE hitting KRA ─────────────────────
        # Persisting here means a crash / KRA timeout can never cause the same
        # number to be reused — the next retry will get max + 1 instead.
        inv.store_number   = meta["store_number"]
        inv.invoice_number = meta["invoice_number"]
        await db.commit()
        logger.info(
            "submit_to_etims: locked invoice_number=%s  store_number=%s  for EtimsInvoice %s",
            meta["invoice_number"], meta["store_number"], etims_invoice_id,
        )

        try:
            receipt_header = invoice_dict_to_receipt(invoice_header, items_list)
        except ValueError as exc:
            logger.error("submit_to_etims: receipt conversion failed for GRN %s: %s", grn_id, exc)
            inv.status        = EtimsStatus.rejected
            inv.error_message = str(exc)
            await db.commit()
            return {"error": str(exc)}

        # ── Development mode: render PDF preview, skip KRA ───────────────────
        if _is_dev():
            return await _dev_mode_pdf(grn_id, etims_invoice_id, receipt_header, inv, grn, db)

        # ── Production: submit to KRA portal ─────────────────────────────────
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
            inv.status        = EtimsStatus.rejected
            
            inv.error_message = str(last_error)
            await db.commit()
            return {"error": str(last_error)}

        # ── Persist result ────────────────────────────────────────────────────
        first_result = results[0] if results else {}
        if first_result.get("status") == "ok":
            inv.status       = EtimsStatus.submitted
            grn.status       = GRNStatus.invoiced
            inv.kra_response = first_result.get("response")
        else:
            inv.status        = EtimsStatus.rejected
            inv.error_message = first_result.get("error", "unknown error")
            inv.retry_count      = (inv.retry_count or 0) + 1

        await db.commit()
        logger.info(
            "submit_to_etims: GRN %s → eTIMS %s  status=%s",
            grn_id, etims_invoice_id, inv.status,
        )
        return first_result


# ── Development-mode helper ───────────────────────────────────────────────────

async def _dev_mode_pdf(
    grn_id: str,
    etims_invoice_id: str,
    receipt_header,
    inv: "EtimsInvoice",
    grn: "GRN",
    db,
) -> dict:
    """
    Render a local PDF preview of the eTIMS payload and update the
    EtimsInvoice row as if the submission succeeded.  Called only when
    APP_ENV=development.

    The PDF path is stored in inv.kra_response so it surfaces in API
    responses and can be retrieved by developers without any extra tooling.
    """
    from app.services.etims_dev_pdf import render_dev_receipt_pdf

    logger.info(
        "submit_to_etims [DEV]: APP_ENV=development — skipping KRA portal for GRN %s",
        grn_id,
    )

    try:
        # render_dev_receipt_pdf is CPU-bound (ReportLab); run off the event loop
        pdf_path = await asyncio.to_thread(
            render_dev_receipt_pdf,
            receipt_header,
            DEV_RECEIPT_DIR,
        )
        logger.info(
            "submit_to_etims [DEV]: PDF preview written to %s for EtimsInvoice %s",
            pdf_path, etims_invoice_id,
        )
        result = {"status": "ok", "dev_mode": True, "pdf_path": pdf_path}
        inv.status       = EtimsStatus.submitted
        inv.kra_response = pdf_path          # store path so it's visible via API
        grn.status       = GRNStatus.invoiced

    except Exception as exc:                 # never let a PDF error block the flow
        logger.error(
            "submit_to_etims [DEV]: PDF render failed for GRN %s: %s", grn_id, exc
        )
        result = {"status": "ok", "dev_mode": True, "pdf_path": None, "pdf_error": str(exc)}
        inv.status       = EtimsStatus.submitted
        inv.kra_response = f"[DEV] PDF render failed: {exc}"

    await db.commit()
    logger.info(
        "submit_to_etims [DEV]: GRN %s → eTIMS %s  status=%s",
        grn_id, etims_invoice_id, inv.status,
    )
    return result