"""
app/workers/etims_tasks.py
Updated to ensure kra_invoice_no and kra_response are persisted to the DB.
"""

import asyncio
import os
import json
import uuid
from pathlib import Path
from dotenv import load_dotenv

from app.core.logging import get_logger
from app.db.models.etims_invoice import EtimsInvoice, EtimsStatus
from app.db.models.grn import GRN, GRNStatus
from app.db.session import AsyncSessionLocal
from app.services.etims_mapper import build_etims_payload
from app.services.fill_kra import EtimsConfig, KraError, invoice_dict_to_receipt, run_fill

# Load environment
env_path = Path(__file__).resolve().parent.parent.parent / '.env'
load_dotenv(dotenv_path=env_path)
logger = get_logger(__name__)

APP_ENV = os.environ.get("APP_ENV", "production").lower()
MAX_RETRIES = 3
RETRY_DELAY = 60  # seconds

def _extract_kracu(response_data):
    """
    Robustly extracts the KRACU number from KRA response.
    Checks top-level and nested 'data' or 'rtnData' objects.
    """
    if not response_data:
        return None
        
    # If response_data is a list (common from run_fill), take first element
    if isinstance(response_data, list) and len(response_data) > 0:
        response_data = response_data[0]
    
    if not isinstance(response_data, dict):
        return None

    # Keys KRA typically uses for the official Invoice Number
    keys_to_check = ["invcNo", "cuInvcNo", "invoiceNo", "receiptNo"]
    
    # 1. Check top level
    for key in keys_to_check:
        val = response_data.get(key)
        if val and "KRACU" in str(val):
            return str(val)
            
    # 2. Check common nested objects like 'data' or 'rtnData'
    for nested_key in ["data", "rtnData", "result"]:
        nested = response_data.get(nested_key)
        if isinstance(nested, dict):
            for key in keys_to_check:
                val = nested.get(key)
                if val and "KRACU" in str(val):
                    return str(val)
                    
    return None

async def submit_to_etims(grn_id: int, etims_invoice_id: int):
    """
    Background task: 
    1. Fetches GRN and EtimsInvoice from DB.
    2. Submits to KRA (or renders PDF in dev).
    3. UPDATES kra_invoice_no and kra_response in DB.
    """
    async with AsyncSessionLocal() as db:
        # ── 1. Fetch Records ──────────────────────────────────────────────────
        grn = await db.get(GRN, grn_id)
        inv = await db.get(EtimsInvoice, etims_invoice_id)

        if not grn or not inv:
            logger.error("submit_to_etims: GRN %s or Invoice %s not found", grn_id, etims_invoice_id)
            return

        try:
            # ── 2. Build Payload ──────────────────────────────────────────────
            header_dict, items_list, meta = await build_etims_payload(db, grn)
            receipt_header = invoice_dict_to_receipt(header_dict, items_list)

            # ── 3. Handle Development Mode ────────────────────────────────────
            if APP_ENV == "development":
                await _handle_dev_mode(db, grn, inv, receipt_header)
                return

            # ── 4. Production Submission ──────────────────────────────────────
            cfg = EtimsConfig(
                pin=os.environ.get("KRA_PIN"),
                username=os.environ.get("KRA_USERNAME"),
                password=os.environ.get("KRA_PASSWORD")
            )

            results = await asyncio.to_thread(run_fill, cfg, receipt_header)
            
            # ── 5. PERSISTENCE: Extract and Save ──────────────────────────────
            # run_fill returns a list of result dicts
            primary_result = results[0] if results else {}
            
            # Capture the KRACU number (e.g. KRACU0200021805/388)
            kracu_no = _extract_kracu(primary_result)
            
            # Update the record
            inv.kra_invoice_no = kracu_no
            # Store the full JSON response as a string for auditing
            inv.kra_response = json.dumps(primary_result, default=str)
            inv.status = EtimsStatus.submitted
            grn.status = GRNStatus.invoiced

            await db.commit()
            logger.info("Successfully filed Invoice %s (KRACU: %s)", etims_invoice_id, kracu_no)

        except Exception as exc:
            logger.error("Failed to submit Invoice %s: %s", etims_invoice_id, exc)
            inv.status = EtimsStatus.failed
            inv.kra_response = str(exc)
            await db.commit()

async def _handle_dev_mode(db, grn, inv, receipt_header):
    """Helper for APP_ENV=development"""
    from app.services.etims_dev_pdf import render_dev_receipt_pdf
    DEV_RECEIPT_DIR = Path("/tmp/etims_dev")
    DEV_RECEIPT_DIR.mkdir(parents=True, exist_ok=True)

    pdf_path = await asyncio.to_thread(render_dev_receipt_pdf, receipt_header, DEV_RECEIPT_DIR)
    
    inv.status = EtimsStatus.submitted
    inv.kra_invoice_no = f"DEV-MODE-{uuid.uuid4().hex[:8].upper()}"
    inv.kra_response = f"DEV_PDF_PATH: {pdf_path}"
    grn.status = GRNStatus.invoiced
    
    await db.commit()
    logger.info("DEV MODE: Mocked submission for Invoice %s", inv.id)