"""
download_invoices.py  –  List and download eTIMS sales receipt PDFs.

Flow
────
1.  Login (same as fill_kra.py)
2.  POST /app/ebm/trns/sales/trnsSalesReceiptList   →  get invoice list for a date range
3.  For each invoice,
    POST /app/ebm/trns/popup/popupTrnsSalesReceiptPDF  →  download PDF via popup form (primary)
    GET  /app/ebm/trns/sales/printTrnsSalesReceipt?invcNo=<KRACU…>  →  fallback method

Usage
─────
    # Download all receipts for today
    python download_invoices.py

    # Download receipts for a specific date
    python download_invoices.py --date 11/03/2026

    # Download receipts for a date range
    python download_invoices.py --start 01/03/2026 --end 11/03/2026

    # Download a single known invoice
    python download_invoices.py --invoice KRACU0200021805/388

    # Save to a specific folder
    python download_invoices.py --date 11/03/2026 --out ./receipts

Credentials are read from environment variables (or edit the defaults below):
    KRA_PIN, KRA_USERNAME, KRA_PASSWORD
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── re-use login machinery from fill_kra ──────────────────────────────────────
try:
    from .fill_kra import EtimsConfig, login, _make_session, _raise_for_kra, KraError
except ImportError:
    # Fallback: duplicate just enough so this file is self-contained
    raise ImportError(
        "fill_kra.py must be in the same directory as download_invoices.py"
    )

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT PATHS
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL       = "https://etims.kra.go.ke"
LIST_PATH      = "/app/ebm/trns/sales/trnsSalesReceiptList"
PRINT_PATH     = "/app/ebm/trns/sales/printTrnsSalesReceipt"   # ?invcNo=KRACU…
INDEX_PATH     = "/app/ebm/trns/sales/indexTrnsSalesReceipt"
POPUP_PDF_PATH = "/app/ebm/trns/popup/popupTrnsSalesReceiptPDF"

TIMEOUT_S      = 30
DELAY_S        = 0.4   # polite delay between PDF downloads


# ─────────────────────────────────────────────────────────────────────────────
# HELPER – extract internal receipt ID from a KRACU string
# ─────────────────────────────────────────────────────────────────────────────

def _extract_internal_id(invc_no: str) -> str:
    """
    Extract the short integer receipt ID from a full KRACU invoice number.

    e.g.  "KRACU0200021805/388"  →  "388"
          "388"                  →  "388"   (already a bare ID)
          "KRACU0200021805"      →  ""      (no slash, cannot extract)
    """
    if "/" in invc_no:
        return invc_no.rsplit("/", 1)[-1].strip()
    # If it's already a bare integer string, return as-is
    if invc_no.strip().isdigit():
        return invc_no.strip()
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 – FETCH INVOICE LIST
# ─────────────────────────────────────────────────────────────────────────────

def fetch_invoice_list(
    session:  requests.Session,
    cfg:      EtimsConfig,
    start_dt: str,          # "dd/MM/yyyy"  e.g. "11/03/2026"
    end_dt:   str,          # "dd/MM/yyyy"
    invc_no:  str = "",     # filter by specific invoice number (optional)
    rcpt_ty_cd: str = "",   # filter by receipt type (optional)
    page:     int = 1,
) -> list[dict]:
    """
    POST trnsSalesReceiptList and return a list of invoice dicts.

    The portal returns HTML rows, so we parse them out.  Each dict has at
    minimum:  invcNo, internalId, rcptDt, custNm, totAmt, rcptTyCd
    """
    url = f"{cfg.base_url}{LIST_PATH}"

    payload = [
        ("page",      str(page)),
        ("dtDiv",     "D"),
        ("startDt",   start_dt),
        ("endDt",     end_dt),
        ("invcNo",    invc_no),
        ("rcptTyCd",  rcpt_ty_cd),
    ]

    hdrs = {
        "Accept":       "text/html, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin":       cfg.base_url,
        "Referer":      f"{cfg.base_url}{INDEX_PATH}",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    log.info("  → Fetching invoice list (%s → %s, page %d)…", start_dt, end_dt, page)
    r = session.post(url, data=payload, headers=hdrs, timeout=TIMEOUT_S)
    _raise_for_kra(r, context="trnsSalesReceiptList")

    return _parse_invoice_list(r.text)


def _parse_invoice_list(html: str) -> list[dict]:
    """
    Extract invoice rows from the portal's HTML response.

    The portal renders a table; each row looks like:
      <tr>
        <td>KRACU0200021805/388</td>
        <td>11/03/2026 20:44:50</td>
        <td>Naivas Limited SAFARI CENTER NAIVASHA</td>
        <td>384.00</td>
        ...
      </tr>

    Each returned dict includes:
        invcNo      – full KRACU string  e.g. "KRACU0200021805/388"
        internalId  – short integer ID  e.g. "388"  (used by the popup download)
        rcptDt, custNm, totAmt, rcptTyCd

    We also try a JSON parse first in case the portal returns JSON.
    """
    invoices: list[dict] = []

    # ── Try JSON first ────────────────────────────────────────────────────────
    try:
        import json
        data = json.loads(html)
        rows = (
            data.get("data") or
            data.get("list") or
            data.get("resultList") or
            (data if isinstance(data, list) else [])
        )
        for row in rows:
            invc_no = row.get("invcNo") or row.get("cuInvcNo") or ""
            # Prefer an explicit internalId / curRcptNo field if the portal returns one
            internal_id = (
                str(row.get("internalId") or row.get("curRcptNo") or row.get("rcptNo") or "")
                or _extract_internal_id(invc_no)
            )
            invoices.append({
                "invcNo":     invc_no,
                "internalId": internal_id,
                "rcptDt":     row.get("rcptDt")   or row.get("salesDt")   or "",
                "custNm":     row.get("custNm")   or "",
                "totAmt":     row.get("totAmt")   or row.get("sumTotAmt") or "",
                "rcptTyCd":   row.get("rcptTyCd") or "",
            })
        if invoices:
            return invoices
    except (ValueError, AttributeError):
        pass

    # ── Fall back to HTML table row scraping ──────────────────────────────────
    # Match invoice numbers like KRACU0200021805/388
    invc_pattern = re.compile(r"(KRACU\w+/\d+)", re.IGNORECASE)

    # Try to grab full <tr> blocks and extract cells
    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    td_pattern  = re.compile(r"<td[^>]*>(.*?)</td>",  re.DOTALL | re.IGNORECASE)

    for row_m in row_pattern.finditer(html):
        cells = [re.sub(r"<[^>]+>", "", td.group(1)).strip()
                 for td in td_pattern.finditer(row_m.group(1))]
        if not cells:
            continue
        # First cell that matches an invoice number pattern → that's our row
        invc_no = ""
        for cell in cells:
            if invc_pattern.search(cell):
                invc_no = invc_pattern.search(cell).group(1)
                break
        if invc_no:
            internal_id = _extract_internal_id(invc_no)
            invoices.append({
                "invcNo":     invc_no,
                "internalId": internal_id,
                "rcptDt":     cells[1] if len(cells) > 1 else "",
                "custNm":     cells[2] if len(cells) > 2 else "",
                "totAmt":     cells[3] if len(cells) > 3 else "",
                "rcptTyCd":   cells[4] if len(cells) > 4 else "",
            })

    # ── Last resort: just pull every invoice number found in the page ─────────
    if not invoices:
        for m in invc_pattern.finditer(html):
            invc_no = m.group(1)
            if not any(i["invcNo"] == invc_no for i in invoices):
                invoices.append({
                    "invcNo":     invc_no,
                    "internalId": _extract_internal_id(invc_no),
                    "rcptDt":     "",
                    "custNm":     "",
                    "totAmt":     "",
                    "rcptTyCd":   "",
                })

    return invoices


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 – DOWNLOAD ONE RECEIPT PDF
# ─────────────────────────────────────────────────────────────────────────────

def download_pdf_via_popup(
    session: requests.Session,
    cfg: EtimsConfig,
    internal_invc_no: str,
    out_dir: Path,
) -> Path | None:
    """
    Downloads the PDF using the exact form submission from the receipt popup.
    Note: internal_invc_no is the short integer ID (e.g. '439'), NOT the full KRACU string.
    """
    url = f"{cfg.base_url}{POPUP_PDF_PATH}"

    # Replicating the hidden input fields from the HTML form
    payload = {
        "tin":       cfg.pin,              # e.g., "P051621945B"
        "bhfId":     "00",                 # Usually "00" for the main branch
        "invcNo":    internal_invc_no,     # e.g., "388"
        "curRcptNo": internal_invc_no,
    }
    print("PAYLOAD        >   >   >", payload)
    hdrs = {
        "Accept":         "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Content-Type":   "application/x-www-form-urlencoded",
        "Origin":         cfg.base_url,
        "Referer":        f"{cfg.base_url}{INDEX_PATH}",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }

    log.info("  → POSTing PDF popup form for internal ID %s …", internal_invc_no)

    try:
        r = session.post(url, data=payload, headers=hdrs, timeout=TIMEOUT_S, stream=True)
        r.raise_for_status()
    except Exception as exc:
        log.warning("  ⚠️  Popup download failed for ID %s: %s", internal_invc_no, exc)
        return None

    # Reject HTML error pages masquerading as a successful response
    c_type = r.headers.get("Content-Type", "").lower()
    valid_types = ["application/pdf", "application/download", "application/octet-stream"]
        
    if not any(t in c_type for t in valid_types):
            log.warning("  ⚠️  Popup response for ID %s was not a PDF (Content-Type: %s)", internal_invc_no, c_type)
            return None

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"SalesReceipt_{internal_invc_no}.pdf"

    with open(out_path, "wb") as fh:
        for chunk in r.iter_content(chunk_size=8192):
            fh.write(chunk)

    log.info("  ✅ Saved PDF via popup → %s  (%d bytes)", out_path, out_path.stat().st_size)
    return out_path


def download_receipt_pdf(
    session:  requests.Session,
    cfg:      EtimsConfig,
    invc_no:  str,
    out_dir:  Path,
) -> Path | None:
    """
    Fallback: GET the PDF for a single invoice by full KRACU invoice number.
    Returns the saved Path, or None if the download failed.
    """
    url    = f"{cfg.base_url}{PRINT_PATH}"
    params = {"invcNo": invc_no}

    hdrs = {
        "Accept":         "application/pdf,text/html,*/*;q=0.9",
        "Referer":        f"{cfg.base_url}{INDEX_PATH}",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
    }

    log.info("  → [fallback] Downloading %s via GET …", invc_no)
    try:
        r = session.get(url, params=params, headers=hdrs,
                        timeout=TIMEOUT_S, stream=True)
        _raise_for_kra(r, context=f"printReceipt {invc_no}")
    except KraError as exc:
        log.warning("  ⚠️  Skipping %s: %s", invc_no, exc)
        return None

    # Determine file extension from Content-Type
    content_type = r.headers.get("Content-Type", "")
    ext = ".pdf" if "pdf" in content_type else ".html"

    # Sanitise invoice number for use as a filename  (/ → _)
    safe_name = invc_no.replace("/", "_").replace("\\", "_")
    out_path  = out_dir / f"SalesReceipt_{safe_name}{ext}"

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as fh:
        for chunk in r.iter_content(chunk_size=8192):
            fh.write(chunk)

    log.info("  ✅ Saved → %s  (%d bytes)", out_path, out_path.stat().st_size)
    return out_path


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def download_invoices(
    cfg:        EtimsConfig,
    start_dt:   str,          # "dd/MM/yyyy"
    end_dt:     str,          # "dd/MM/yyyy"
    out_dir:    Path = Path("./downloaded_receipts"),
    invc_no:    str = "",     # download a single invoice if set (full KRACU or bare ID)
    rcpt_ty_cd: str = "",
) -> list[Path]:
    """
    Login → list invoices → download each PDF via popup POST method.

    For each invoice the internal integer receipt ID (the trailing number in
    the KRACU string, e.g. "440" from "KRACU…/440") is used with the popup
    endpoint, which is the only method that reliably returns a PDF.

    Returns list of successfully saved file paths.
    """
    session = _make_session()

    log.info("🔑 Logging in to eTIMS portal…")
    login(cfg, session)

    # ── Fetch list ────────────────────────────────────────────────────────────
    if invc_no:
        # Single known invoice — build a minimal dict so the loop below works
        internal_id = _extract_internal_id(invc_no)
        invoices = [{
            "invcNo":     invc_no,
            "internalId": internal_id,
            "rcptDt":     "",
            "custNm":     "",
            "totAmt":     "",
            "rcptTyCd":   "",
        }]
    else:
        invoices = fetch_invoice_list(session, cfg, start_dt, end_dt,
                                      rcpt_ty_cd=rcpt_ty_cd)

    if not invoices:
        log.warning("⚠️  No invoices found for %s → %s", start_dt, end_dt)
        return []

    log.info("📋 %d invoice(s) found.  Downloading to %s …", len(invoices), out_dir)

    # ── Print summary table ───────────────────────────────────────────────────
    print(f"\n{'No.':<5} {'Invoice No.':<30} {'Date':<22} {'Customer':<35} {'Amount':>10}")
    print("─" * 105)
    for idx, inv in enumerate(invoices, 1):
        print(f"{idx:<5} {inv['invcNo']:<30} {inv['rcptDt']:<22} "
              f"{inv['custNm'][:34]:<35} {inv['totAmt']:>10}")
    print()

    # ── Download each PDF ─────────────────────────────────────────────────────
    saved: list[Path] = []
    for idx, inv in enumerate(invoices):
        path: Path | None = None

        # Primary: popup POST method using the integer internal ID
        internal_id = inv.get("internalId", "")
        if internal_id:
            path = download_pdf_via_popup(session, cfg, str(internal_id), out_dir)
        else:
            log.warning(
                "  ⚠️  No internal ID available for %s — cannot download.",
                inv["invcNo"],
            )

        if path:
            saved.append(path)

        if idx < len(invoices) - 1:
            time.sleep(DELAY_S)

    log.info("📊 Done: %d/%d receipts downloaded to %s",
             len(saved), len(invoices), out_dir.resolve())
    return saved


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _today() -> str:
    return date.today().strftime("%d/%m/%Y")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download eTIMS sales receipt PDFs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--date",    metavar="DD/MM/YYYY",
                        help="Single date (defaults to today)")
    parser.add_argument("--start",   metavar="DD/MM/YYYY",
                        help="Start date for range (use with --end)")
    parser.add_argument("--end",     metavar="DD/MM/YYYY",
                        help="End date for range (use with --start)")
    parser.add_argument("--invoice", metavar="KRACU…/NNN",
                        help="Download a single specific invoice number")
    parser.add_argument("--out",     metavar="DIR", default="./downloaded_receipts",
                        help="Output directory (default: ./downloaded_receipts)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    # ── Resolve date range ────────────────────────────────────────────────────
    if args.invoice:
        start_dt = end_dt = _today()          # dates don't matter for single-invoice fetch
    elif args.date:
        start_dt = end_dt = args.date
    elif args.start and args.end:
        start_dt, end_dt = args.start, args.end
    else:
        start_dt = end_dt = _today()

    # ── Credentials ───────────────────────────────────────────────────────────
    cfg = EtimsConfig(
        pin      = os.environ.get("KRA_PIN",      "P051621945B"),
        username = os.environ.get("KRA_USERNAME",  "P051621945B"),
        password = os.environ.get("KRA_PASSWORD",  "Nairobi@2025"),
    )

    saved = download_invoices(
        cfg      = cfg,
        start_dt = start_dt,
        end_dt   = end_dt,
        out_dir  = Path(args.out),
        invc_no  = args.invoice or "",
    )

    if saved:
        print(f"\n✅ {len(saved)} file(s) saved:")
        for p in saved:
            print(f"   {p}")
    else:
        print("\n⚠️  No files were downloaded.")
        sys.exit(1)


if __name__ == "__main__":
    main()