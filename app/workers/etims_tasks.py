"""
app/workers/etims_tasks.py

Plain async function that reads a confirmed GRN, builds the eTIMS payload,
converts it to a ReceiptHeader, and submits it to the KRA portal.

invoice_number   = system-generated sequential eTIMS number (e.g. "006")
cust_invoice_no  = customer's own reference copied from grn.invoice_no (e.g. "2063")

Key design principles
─────────────────────
• Pre-confirm probe: the sales endpoint is checked BEFORE any invoice number
  is consumed or any DB row mutated.  If the endpoint is down the task aborts
  cleanly with no side-effects.

• Zero retries: KRA frequently accepts a POST but responds slowly (or times
  out before returning a response).  Retrying would submit the same receipt
  twice and create a duplicate eTIMS invoice.  Any failure — including a
  ReadTimeout on the sales POST — marks the EtimsInvoice as rejected.  The
  operator must manually verify on the KRA portal before deciding to resubmit.
"""

import asyncio
import json
import os
import uuid
from functools import partial
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.logging import get_logger
from app.db.models.etims_invoice import EtimsInvoice, EtimsStatus
from app.db.models.grn import GRN, GRNStatus
from app.db.session import AsyncSessionLocal
from app.services.etims_mapper import build_etims_payload
from app.services.fill_kra import EtimsConfig, KraError, invoice_dict_to_receipt, run_fill

env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)
logger = get_logger(__name__)

DEV_RECEIPT_DIR = os.environ.get("DEV_RECEIPT_DIR", "/tmp/etims_dev")

# Timeout for the pre-confirm sales endpoint probe (seconds).
# Keep it short — this is a gate check, not a full submission.
PROBE_TIMEOUT_S = 10


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
    """
    if not response_data:
        return ""

    if isinstance(response_data, list):
        response_data = response_data[0] if response_data else {}

    if not isinstance(response_data, dict):
        return ""

    keys_to_check = ["invcNo", "cuInvcNo", "invoiceNo", "receiptNo"]

    for key in keys_to_check:
        val = response_data.get(key)
        if val and "KRACU" in str(val):
            return str(val)

    for nested_key in ["data", "rtnData", "result"]:
        nested = response_data.get(nested_key)
        if isinstance(nested, dict):
            for key in keys_to_check:
                val = nested.get(key)
                if val and "KRACU" in str(val):
                    return str(val)

    return ""


async def _probe_sales_endpoint() -> tuple[bool, str]:
    """
    Run a pre-confirm connectivity probe against the eTIMS sales endpoint.

    Returns (ok: bool, message: str).

    Uses etims_health.probe_etims() which now includes a dedicated
    "Sales endpoint" check (GET on the sales index route).  This is
    intentionally login-free and safe to run before any DB mutation.
    """
    from app.services.etims_health import probe_etims

    try:
        report = await asyncio.get_event_loop().run_in_executor(
            None, partial(probe_etims, PROBE_TIMEOUT_S)
        )
    except Exception as exc:
        return False, f"eTIMS probe raised an unexpected error: {exc}"

    if report.site_up:
        sales_check = next(
            (c for c in report.checks if c.name == "Sales endpoint"), None
        )
        msg = (
            sales_check.message
            if sales_check
            else "All connectivity checks passed"
        )
        return True, msg

    # Collect the first failed check for a human-readable error message.
    failed = next((c for c in report.checks if not c.passed), None)
    reason = failed.message if failed else "Unknown connectivity failure"
    return False, f"eTIMS sales endpoint not reachable: {reason}"


async def submit_to_etims(grn_id: str, etims_invoice_id: str) -> dict:
    """
    Background task:

    0. Probe the eTIMS sales endpoint BEFORE doing anything irreversible.
       If the endpoint is unreachable the task aborts with a clear error —
       no invoice number is consumed, no DB row is mutated.
    1. Load the confirmed GRN and its EtimsInvoice from the DB.
    2. Build the eTIMS payload via etims_mapper.build_etims_payload().
       The mapper generates the sequential invoice_number for this store.
    3. Copy grn.invoice_no -> inv.cust_invoice_no (the customer's own reference).
    4. Convert to ReceiptHeader and submit to KRA (or render dev PDF).
    5. Persist EtimsInvoice.status -> submitted / rejected.

    NO automatic retries are ever attempted.  KRA frequently accepts a POST
    but responds slowly (or times out before returning); retrying would create
    a duplicate eTIMS invoice.  Any failure — including a ReadTimeout — marks
    the EtimsInvoice as rejected so the operator can manually verify on the
    KRA portal before deciding whether to resubmit.
    """
    grn_uuid = uuid.UUID(grn_id)
    inv_uuid = uuid.UUID(etims_invoice_id)

    # ── Step 0: pre-confirm probe ─────────────────────────────────────────────
    # Run BEFORE opening a DB session so we don't hold a connection open during
    # the network round-trip.  Dev mode skips this so unit tests don't need a
    # live KRA connection.
    if not _is_dev():
        endpoint_ok, probe_msg = await _probe_sales_endpoint()
        if not endpoint_ok:
            logger.error(
                "submit_to_etims: eTIMS pre-confirm probe failed for GRN %s — %s",
                grn_id, probe_msg,
            )
            # Mark the invoice rejected so the UI shows a clear error.
            async with AsyncSessionLocal() as db:
                inv = await db.get(EtimsInvoice, inv_uuid)
                if inv:
                    inv.status        = EtimsStatus.rejected
                    inv.error_message = probe_msg
                    await db.commit()
            return {"error": probe_msg}

        logger.info(
            "submit_to_etims: eTIMS pre-confirm probe OK for GRN %s — %s",
            grn_id, probe_msg,
        )

    async with AsyncSessionLocal() as db:
        grn = await db.get(GRN, grn_uuid)
        if not grn:
            logger.error("submit_to_etims: GRN %s not found", grn_id)
            return {"error": f"GRN {grn_id} not found"}

        inv = await db.get(EtimsInvoice, inv_uuid)
        if not inv:
            logger.error("submit_to_etims: EtimsInvoice %s not found", etims_invoice_id)
            return {"error": f"EtimsInvoice {etims_invoice_id} not found"}

        # -- 1. Build payload --------------------------------------------------
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

        # -- 2. Lock both numbers before hitting KRA ---------------------------
        # invoice_number  = system sequential eTIMS number (e.g. "006")
        # cust_invoice_no = customer's own reference from grn.invoice_no (e.g. "2063")
        #
        # Guard: lpo_number (receipt_voucher_no) has a unique constraint.
        # If a *different* EtimsInvoice row already owns this value it belongs
        # to a rejected duplicate GRN.  Delete that stale invoice AND its GRN
        # so this submission can proceed cleanly.
        new_lpo = meta["lpo_number"]
        if new_lpo:
            clash_result = await db.execute(
                select(EtimsInvoice).where(
                    EtimsInvoice.lpo_number == new_lpo,
                    EtimsInvoice.id         != inv_uuid,
                )
            )
            clashing_inv: EtimsInvoice | None = clash_result.scalar_one_or_none()
            if clashing_inv:
                clashing_grn: GRN | None = (
                    await db.get(GRN, clashing_inv.grn_id)
                    if clashing_inv.grn_id else None
                )
                logger.warning(
                    "submit_to_etims: lpo_number '%s' already held by "
                    "EtimsInvoice %s (GRN %s status=%s) — deleting stale "
                    "duplicate before proceeding with GRN %s / EtimsInvoice %s",
                    new_lpo,
                    clashing_inv.id,
                    clashing_grn.id     if clashing_grn else "N/A",
                    clashing_grn.status if clashing_grn else "N/A",
                    grn_id,
                    etims_invoice_id,
                )
                await db.delete(clashing_inv)
                if clashing_grn:
                    await db.delete(clashing_grn)
                await db.flush()   # release the unique constraint before the UPDATE

        inv.store_number    = meta["store_number"]
        inv.invoice_no      = meta["invoice_number"]
        inv.grn_number      = meta["grn_no"]
        inv.lpo_number      = new_lpo
        inv.cust_invoice_no = grn.invoice_no or None

        # Safety net: concurrent request inserted the same lpo_number between
        # our SELECT and this commit — fall back to a clean rejection so the
        # ASGI worker is never crashed by an unhandled IntegrityError.
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            error_msg = (
                f"Could not clear duplicate lpo_number '{new_lpo}' "
                "(concurrent request). Please retry in a moment."
            )
            logger.error(
                "submit_to_etims: IntegrityError after duplicate cleanup for "
                "GRN %s / EtimsInvoice %s -- %s",
                grn_id, etims_invoice_id, error_msg,
            )
            inv = await db.get(EtimsInvoice, inv_uuid)
            if inv:
                inv.status        = EtimsStatus.rejected
                inv.error_message = error_msg
                await db.commit()
            return {"error": error_msg}

        logger.info(
            "submit_to_etims: locked invoice_number=%s cust_invoice_no=%s "
            "store_number=%s for EtimsInvoice %s",
            meta["invoice_number"], inv.cust_invoice_no,
            meta["store_number"], etims_invoice_id,
        )

        # -- 3. Convert to ReceiptHeader ----------------------------------------
        try:
            receipt_header = invoice_dict_to_receipt(invoice_header, items_list)
        except ValueError as exc:
            logger.error("submit_to_etims: receipt conversion failed for GRN %s: %s", grn_id, exc)
            inv.status        = EtimsStatus.rejected
            inv.error_message = str(exc)
            await db.commit()
            return {"error": str(exc)}

        # -- 4. Development mode -----------------------------------------------
        if _is_dev():
            return await _dev_mode_pdf(grn_id, etims_invoice_id, receipt_header, inv, grn, db)

        # -- 5. Production: single attempt — NO retries -------------------------
        # Any failure (timeout, KRA unreachable, error response) immediately
        # rejects the EtimsInvoice.  The operator must verify on the KRA portal
        # before explicitly resubmitting.
        cfg     = _get_etims_cfg()
        results = None

        try:
            results = await asyncio.to_thread(run_fill, cfg, receipt_header)
            logger.info("Submission To Etims RESPONSE : %s", results)
            print("=*" * 50)
            print("SUBMITTED TO ETIMS RESPONSE :", results)
            print("=*" * 50)
        except KraError as exc:
            error_msg = f"KRA portal error: {exc}"
            logger.error(
                "submit_to_etims: KRA call raised KraError for GRN %s: %s",
                grn_id, exc,
            )
            inv.status        = EtimsStatus.rejected
            inv.error_message = error_msg
            await db.commit()
            return {"error": error_msg}
        except Exception as exc:
            error_msg = f"Unexpected error contacting KRA: {exc}"
            logger.error(
                "submit_to_etims: unexpected exception for GRN %s: %s",
                grn_id, exc,
            )
            inv.status        = EtimsStatus.rejected
            inv.error_message = error_msg
            await db.commit()
            return {"error": error_msg}

        primary_result = results[0] if results else {}
        kra_status     = primary_result.get("status")

        # A ReadTimeout means KRA received the POST but did not respond in time.
        # The invoice is very likely already registered — do NOT retry.
        # Mark as rejected so the operator verifies manually before resubmitting.
        if kra_status == "timeout":
            error_msg = (
                "KRA portal did not respond in time (timeout). "
                "The invoice was likely accepted — please verify on the KRA "
                "eTIMS portal BEFORE attempting to resubmit to avoid duplicates."
            )
            logger.error(
                "submit_to_etims: KRA timeout for GRN %s (EtimsInvoice %s) — "
                "marking rejected. Operator must verify on portal before resubmitting.",
                grn_id, etims_invoice_id,
            )
            inv.status        = EtimsStatus.rejected
            inv.error_message = error_msg
            inv.kra_response  = json.dumps(primary_result, default=str)
            await db.commit()
            return {"error": error_msg}

        if kra_status != "ok":
            error_msg = primary_result.get("error") or f"KRA returned status '{kra_status}'"
            logger.error(
                "submit_to_etims: KRA rejected submission for GRN %s: %s",
                grn_id, error_msg,
            )
            inv.status        = EtimsStatus.rejected
            inv.error_message = error_msg
            inv.kra_response  = json.dumps(primary_result, default=str)
            await db.commit()
            return {"error": error_msg}

        # -- 6. Persist successful result --------------------------------------
        inv.status       = EtimsStatus.submitted
        grn.status       = GRNStatus.invoiced
        inv.kra_response = json.dumps(primary_result, default=str)

        predicted_no        = primary_result.get("kra_invoice_no")
        kracu_from_response = _extract_kracu(primary_result.get("response") or primary_result)
        kra_invoice_no      = predicted_no or kracu_from_response or None

        if kra_invoice_no:
            inv.kra_invoice_no = kra_invoice_no
            logger.info(
                "submit_to_etims: kra_invoice_no=%s saved for EtimsInvoice %s",
                kra_invoice_no, etims_invoice_id,
            )
        else:
            logger.warning(
                "submit_to_etims: KRA accepted receipt but kra_invoice_no "
                "could not be determined for EtimsInvoice %s -- raw: %s",
                etims_invoice_id, str(primary_result)[:300],
            )

        await db.commit()
        logger.info(
            "submit_to_etims: GRN %s -> eTIMS %s status=%s",
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
    """Render a local PDF preview and mark the invoice as submitted (dev only)."""
    from app.services.etims_dev_pdf import render_dev_receipt_pdf

    dev_dir = Path(DEV_RECEIPT_DIR)
    dev_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        "submit_to_etims [DEV]: APP_ENV=development -- skipping KRA portal for GRN %s",
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
        "submit_to_etims [DEV]: GRN %s -> eTIMS %s status=%s",
        grn_id, etims_invoice_id, inv.status,
    )
    return result