"""
app/workers/etims_tasks.py

Plain async function that reads a confirmed GRN, builds the eTIMS payload,
converts it to a ReceiptHeader, and submits it to the KRA portal.

Retry logic is handled internally with asyncio.sleep (MAX_RETRIES attempts,
RETRY_DELAY seconds apart, with linear back-off).

Development mode
────────────────
When APP_ENV=development the function skips the KRA portal and instead renders
a local PDF preview.  The EtimsInvoice row is stamped with status=submitted and
the local PDF path stored in kra_response so the rest of the app behaves
identically to production.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv

from app.core.logging import get_logger
from app.db.models.etims_invoice import EtimsInvoice, EtimsStatus
from app.db.models.grn import GRN, GRNStatus
from app.db.session import AsyncSessionLocal
from app.services.etims_mapper import build_etims_payload
from app.services.fill_kra import EtimsConfig, KraError, invoice_dict_to_receipt, run_fill

env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)
logger = get_logger(__name__)

MAX_RETRIES      = 3
RETRY_DELAY_SECS = 60

DEV_RECEIPT_DIR = os.environ.get("DEV_RECEIPT_DIR", "/tmp/etims_dev")


def _is_dev() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() == "development"


def _get_etims_cfg() -> EtimsConfig:
    return EtimsConfig(
        pin      = os.environ["KRA_PIN"],
        branch   = os.environ.get("KRA_BRANCH", "001"),
        username = os.environ["KRA_USERNAME"],
        password = os.environ["KRA_PASSWORD"],
    )


def _extract_kracu(response_data) -> str:
    """
    Robustly extract the KRACU invoice number from a KRA response dict.
    Handles lists (run_fill returns a list), top-level keys, and common
    nested containers ('data', 'rtnData', 'result').
    """
    if not response_data:
        return ""

    # run_fill returns a list — unwrap to the first element
    if isinstance(response_data, list):
        response_data = response_data[0] if response_data else {}

    if not isinstance(response_data, dict):
        return ""

    keys_to_check = ["invcNo", "cuInvcNo", "invoiceNo", "receiptNo"]

    # 1. Top-level keys
    for key in keys_to_check:
        val = response_data.get(key)
        if val and "KRACU" in str(val):
            return str(val)

    # 2. Common nested containers
    for nested_key in ["data", "rtnData", "result"]:
        nested = response_data.get(nested_key)
        if isinstance(nested, dict):
            for key in keys_to_check:
                val = nested.get(key)
                if val and "KRACU" in str(val):
                    return str(val)

    return ""


async def submit_to_etims(grn_id: str, etims_invoice_id: str) -> dict:
    """
    Background task (no Celery):

    1. Load the confirmed GRN and its EtimsInvoice from the DB.
    2. Build the eTIMS payload via etims_mapper.build_etims_payload().
    3. Convert to ReceiptHeader via fill_kra.invoice_dict_to_receipt().
    4a. [PRODUCTION]  Submit to KRA portal via fill_kra.run_fill().
        Retries up to MAX_RETRIES times on KraError before giving up.
    4b. [DEVELOPMENT] Render a local PDF preview instead of calling KRA.
    5. Persist EtimsInvoice.status → submitted / rejected.
    """
    # FIX: IDs arrive as strings from BackgroundTasks — convert to UUID first.
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

        # ── 1. Build payload ──────────────────────────────────────────────────
        # FIX: build_etims_payload requires explicit keyword arguments, not (db, grn).
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
            inv.status        = EtimsStatus.rejected   # FIX: EtimsStatus.failed does not exist
            inv.error_message = str(exc)
            await db.commit()
            return {"error": str(exc)}

        # Lock invoice number before hitting KRA so retries never reuse it.
        inv.store_number   = meta["store_number"]
        inv.invoice_number = meta["invoice_number"]
        await db.commit()
        logger.info(
            "submit_to_etims: locked invoice_number=%s store_number=%s for EtimsInvoice %s",
            meta["invoice_number"], meta["store_number"], etims_invoice_id,
        )

        # ── 2. Convert to ReceiptHeader ───────────────────────────────────────
        try:
            receipt_header = invoice_dict_to_receipt(invoice_header, items_list)
        except ValueError as exc:
            logger.error("submit_to_etims: receipt conversion failed for GRN %s: %s", grn_id, exc)
            inv.status        = EtimsStatus.rejected   # FIX: was EtimsStatus.failed
            inv.error_message = str(exc)
            await db.commit()
            return {"error": str(exc)}

        # ── 3. Development mode ───────────────────────────────────────────────
        if _is_dev():
            return await _dev_mode_pdf(grn_id, etims_invoice_id, receipt_header, inv, grn, db)

        # ── 4. Production: submit to KRA with retry ───────────────────────────
        cfg        = _get_etims_cfg()
        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                results    = await asyncio.to_thread(run_fill, cfg, receipt_header)
                logger.info("Submission To Etims RESPONSE : %s",results)
                print("=*"*50)
                print("SUBMITTED TO ETIMS RESPONSE :",results)
                print("=*"*50)
                last_error = None
                break
            except KraError as exc:
                last_error      = exc
                inv.retry_count = (inv.retry_count or 0) + 1
                logger.warning(
                    "submit_to_etims: KRA attempt %d/%d failed for GRN %s: %s",
                    attempt, MAX_RETRIES, grn_id, exc,
                )
                if attempt < MAX_RETRIES:
                    await db.commit()   # persist incremented retry_count
                    await asyncio.sleep(RETRY_DELAY_SECS * attempt)

        if last_error is not None:
            logger.error(
                "submit_to_etims: all %d KRA attempts exhausted for GRN %s: %s",
                MAX_RETRIES, grn_id, last_error,
            )
            inv.status        = EtimsStatus.rejected   # FIX: was EtimsStatus.failed
            inv.error_message = str(last_error)
            await db.commit()
            return {"error": str(last_error)}

        # ── 5. Persist result ─────────────────────────────────────────────────
        primary_result = results[0] if results else {}

        if primary_result.get("status") == "ok":
            inv.status       = EtimsStatus.submitted
            grn.status       = GRNStatus.invoiced
            inv.kra_response = json.dumps(primary_result, default=str)
            kracu = _extract_kracu(primary_result)
            if kracu:
                inv.kra_invoice_no = kracu
                logger.info(
                    "submit_to_etims: KRA invoice number saved kra_invoice_no=%s for EtimsInvoice %s",
                    kracu, etims_invoice_id,
                )
            else:
                logger.warning(
                    "submit_to_etims: KRA accepted receipt but returned no cuInvcNo "
                    "for EtimsInvoice %s — raw response: %s",
                    etims_invoice_id, str(primary_result)[:300],
                )
        else:
            inv.status        = EtimsStatus.rejected   # FIX: was EtimsStatus.failed
            inv.error_message = primary_result.get("error", "unknown error")
            inv.retry_count   = (inv.retry_count or 0) + 1

        await db.commit()
        logger.info(
            "submit_to_etims: GRN %s → eTIMS %s status=%s",
            grn_id, etims_invoice_id, inv.status,
        )
        return primary_result


async def _dev_mode_pdf(
    grn_id: str,
    etims_invoice_id: str,
    receipt_header,
    inv: "EtimsInvoice",
    grn: "GRN",
    db,
) -> dict:
    """
    Render a local PDF preview and mark the invoice as submitted.
    Called only when APP_ENV=development.
    """
    from app.services.etims_dev_pdf import render_dev_receipt_pdf

    dev_dir = Path(DEV_RECEIPT_DIR)
    dev_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "submit_to_etims [DEV]: APP_ENV=development — skipping KRA portal for GRN %s",
        grn_id,
    )

    try:
        pdf_path = await asyncio.to_thread(render_dev_receipt_pdf, receipt_header, dev_dir)
        logger.info(
            "submit_to_etims [DEV]: PDF preview written to %s for EtimsInvoice %s",
            pdf_path, etims_invoice_id,
        )
        result             = {"status": "ok", "dev_mode": True, "pdf_path": str(pdf_path)}
        inv.status         = EtimsStatus.submitted
        inv.kra_invoice_no = f"DEV-{uuid.uuid4().hex[:8].upper()}"
        inv.kra_response   = f"DEV_PDF_PATH: {pdf_path}"
        grn.status         = GRNStatus.invoiced

    except Exception as exc:
        logger.error(
            "submit_to_etims [DEV]: PDF render failed for GRN %s: %s", grn_id, exc
        )
        result           = {"status": "ok", "dev_mode": True, "pdf_path": None, "pdf_error": str(exc)}
        inv.status       = EtimsStatus.submitted
        inv.kra_response = f"[DEV] PDF render failed: {exc}"

    await db.commit()
    logger.info(
        "submit_to_etims [DEV]: GRN %s → eTIMS %s status=%s",
        grn_id, etims_invoice_id, inv.status,
    )
    return result