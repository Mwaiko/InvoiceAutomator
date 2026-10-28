"""
app/services/resubmit_wrong_pin_invoices.py

For every invoice in Book8.xlsx flagged "WRONG BUYERS PIN":

  1. Scrape the invoice's own content straight from the KRA portal
     (popupTrnsSalesInvoice for buyer/remark info, popupTrnsSalesReceipt
     for the authoritative item list — itemCd, itemClsCd, qty, price, tax
     type).
  2. Resubmit it as a brand-new eTIMS invoice with the CORRECT buyer PIN,
     everything else identical (same items, same non-fiscal remark).
  3. File a credit note against the OLD (wrong-PIN) invoice number.

This order matches what you asked for: fix + refill first, credit the old
one after.

Safety / idempotency
─────────────────────
Each invoice goes through two independent KRA-side actions (resubmit, then
credit-note). Both are logged separately per invoice in the results CSV.
Re-running the script is always safe:
  - If resubmit already succeeded but the credit-note step didn't, only the
    credit-note step is retried — we never resubmit the same invoice twice.
  - If both steps already succeeded, the invoice is skipped entirely.
NO automatic retries within a single run for either KRA POST — same
zero-retry philosophy as fill_kra.py. A failure is logged for manual review,
never silently retried.

Usage
─────
  # 1. Dry run — shows exactly what would happen, makes NO network calls
  python -m app.services.resubmit_wrong_pin_invoices --dry-run

  # 2. Test batch — do this for real, but only the first N pending invoices
  python -m app.services.resubmit_wrong_pin_invoices --live --limit 3 --new-pin P0XXXXXXXA

  # 3. Once you've checked the test batch on the KRA portal, do the rest
  python -m app.services.resubmit_wrong_pin_invoices --live --new-pin P0XXXXXXXA

  # 4. Skip Book8.xlsx entirely and name the invoice(s) yourself.
  #    Accepts the full CU invoice string (ABC/566) or just the number (566).
  python -m app.services.resubmit_wrong_pin_invoices --dry-run --cu-invoice-no 566
  python -m app.services.resubmit_wrong_pin_invoices --live --new-pin P0XXXXXXXA \\
      --cu-invoice-no 566 --cu-invoice-no ABC/702

Credentials: same env vars as the rest of your codebase —
  KRA_PIN, KRA_BRANCH (default "001"), KRA_USERNAME, KRA_PASSWORD
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import requests
from dotenv import load_dotenv

from app.services.etims_item_map import resolve_item
from app.services.etims_invoice_scraper import (
    ScrapeError,
    parse_invoice_popup,
    parse_receipt_popup,
)
from app.services.fill_kra import (
    EtimsConfig,
    KraError,
    ReceiptHeader,
    SaleItem,
    _make_session,
    _raise_for_kra,
    _submit_receipt,
    login,
)

env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

_HERE = Path(__file__).resolve().parent
BOOK8_PATH      = str(_HERE / "Book8.xlsx")
RESULTS_CSV     = str(_HERE / "resubmit_results.csv")
FLAGGED_COMMENT = "WRONG BUYERS PIN"

# Invoices already handled manually / via the earlier credit-note-only run.
# Add to this as you go, or just let the results CSV track it (safer).
ALREADY_DONE: set[str] = {"566", "515"}

# Confirmed from the captured trace for invoice 515 (one of these 28,
# comment = "WRONG BUYERS PIN").
RFD_RSN_CD = "07"

POPUP_INVOICE_PATH = "/app/ebm/trns/popup/popupTrnsSalesInvoice"
POPUP_RECEIPT_PATH = "/app/ebm/trns/popup/popupTrnsSalesReceipt"
POPUP_REFUND_PATH  = "/app/ebm/trns/popup/popupTrnsSalesReceiptRefund"
REFUND_POST_PATH   = "/app/ebm/trns/sales/insertTrnsSalesReceipRefund"
SALES_INDEX_PATH   = "/app/ebm/trns/sales/indexTrnsSalesReceipt"

TIMEOUT_S = 30
DELAY_S   = 2.0

RESULT_FIELDS = [
    "org_invc_no", "full_invc", "amount", "purchase_order",
    "resubmit_status", "resubmit_detail",
    "credit_note_status", "credit_note_detail",
    "timestamp",
]


@dataclass
class PendingInvoice:
    org_invc_no:    str
    full_invc:      str
    amount:         float
    purchase_order: str


# ─────────────────────────────────────────────────────────────────────────────
# Book8.xlsx / results CSV
# ─────────────────────────────────────────────────────────────────────────────

def load_pending_invoices(path: str, flagged_comment: str = FLAGGED_COMMENT) -> list[PendingInvoice]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["Sheet1"]
    rows = list(ws.iter_rows(values_only=True))
    header, data_rows = rows[0], rows[1:]
    col = {name: idx for idx, name in enumerate(header)}

    flagged_comment = flagged_comment.strip().upper()
    pending: list[PendingInvoice] = []
    for row in data_rows:
        comment = str(row[col["comment"]] or "").strip().upper()
        if flagged_comment not in comment:
            continue

        full_invc = str(row[col["INVOICE"]] or "").strip()
        m = re.search(r"/(\d+)$", full_invc)
        if not m:
            print(f"  ! Skipping row — could not parse invoice number from {full_invc!r}")
            continue
        org_invc_no = m.group(1)
        if org_invc_no in ALREADY_DONE:
            continue

        pending.append(PendingInvoice(
            org_invc_no    = org_invc_no,
            full_invc      = full_invc,
            amount         = float(row[col["TAXABLEAMOUNT"]] or 0),
            purchase_order = str(row[col["PURCHASEORDER"]] or ""),
        ))
    return pending


def parse_cu_invoice_no(raw: str) -> str:
    """
    Accepts either a full CU invoice string ("XXXX/566") or a bare number ("566")
    and returns the org_invc_no ("566"), using the same rule as the Excel loader.
    """
    raw = raw.strip()
    m = re.search(r"/(\d+)$", raw)
    if m:
        return m.group(1)
    if raw.isdigit():
        return raw
    raise SystemExit(f"Could not parse an invoice number from --cu-invoice-no {raw!r}")


def load_previous_results(results_path: str) -> dict[str, dict]:
    """org_invc_no -> last row seen for it, so we know what's left to do."""
    out: dict[str, dict] = {}
    p = Path(results_path)
    if not p.exists():
        return out
    with p.open(newline="") as f:
        for row in csv.DictReader(f):
            out[row["org_invc_no"]] = row
    return out


def append_result(results_path: str, row: dict) -> None:
    p = Path(results_path)
    is_new = not p.exists()
    with p.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


# ─────────────────────────────────────────────────────────────────────────────
# KRA calls
# ─────────────────────────────────────────────────────────────────────────────

def _common_headers(cfg: EtimsConfig) -> dict:
    return {
        "Accept":           "text/html, */*; q=0.01",
        "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin":           cfg.base_url,
        "Referer":          f"{cfg.base_url}{SALES_INDEX_PATH}",
        "Sec-Fetch-Dest":   "empty",
        "Sec-Fetch-Mode":   "cors",
        "Sec-Fetch-Site":   "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    }


# These two popup endpoints expect the literal string "00", NOT cfg.branch
# (which is "001" by default and is a different value used elsewhere).
# Confirmed against your own working download_invoice.py, which hardcodes
# bhfId="00" for the same family of popup endpoints, and against the
# captured browser trace (bhfId=00).
POPUP_BHF_ID = "00"


def fetch_invoice_popup_html(session: requests.Session, cfg: EtimsConfig, org_invc_no: str) -> str:
    r = session.post(
        f"{cfg.base_url}{POPUP_INVOICE_PATH}",
        data={
            "tin": cfg.pin, "bhfId": POPUP_BHF_ID, "invcNo": org_invc_no,
            "popId": "popupInvoice", "popTitle": "Invoice Information",
            "popSize": "XL", "multiYn": "N", "callBackFnc": "",
        },
        headers=_common_headers(cfg),
        timeout=TIMEOUT_S,
    )
    _raise_for_kra(r, context="popupTrnsSalesInvoice")
    return r.text


def fetch_receipt_popup_html(session: requests.Session, cfg: EtimsConfig, org_invc_no: str) -> str:
    r = session.post(
        f"{cfg.base_url}{POPUP_RECEIPT_PATH}",
        data={
            "tin": cfg.pin, "bhfId": POPUP_BHF_ID,
            "invcNo": org_invc_no, "curRcptNo": org_invc_no,
            "popId": "popupReceipt", "popTitle": "Receipt Information",
            "popSize": "XL", "multiYn": "N", "callBackFnc": "",
        },
        headers=_common_headers(cfg),
        timeout=TIMEOUT_S,
    )
    _raise_for_kra(r, context="popupTrnsSalesReceipt")
    return r.text


def build_corrected_header(
    org_invc_no: str,
    new_pin: str,
    session: requests.Session,
    cfg: EtimsConfig,
) -> ReceiptHeader:
    """Scrape the old invoice from KRA and rebuild it with the correct PIN."""
    inv_html = fetch_invoice_popup_html(session, cfg, org_invc_no)
    scraped_header = parse_invoice_popup(inv_html, receipt_no=org_invc_no)

    rcpt_html = fetch_receipt_popup_html(session, cfg, org_invc_no)
    scraped_items = parse_receipt_popup(rcpt_html)

    sale_items = []
    for it in scraped_items:
        catalogue_entry = resolve_item(it.item_nm)

        if catalogue_entry:
            # Catalogue is authoritative — it wins even over a scraped code,
            # since what was scraped may just be KRA's auto-assigned
            # placeholder from when this item wasn't in the catalogue yet.
            sale_items.append(SaleItem(
                item_cls_cd = catalogue_entry["item_cls_cd"],
                item_cd     = catalogue_entry["itemCd"],
                item_nm     = it.item_nm,
                qty         = it.qty,
                prc         = it.prc,
                tax_ty_cd   = catalogue_entry["tax_ty_cd"],
                qty_unit_cd = catalogue_entry["uom"],
                pkg_unit_cd = catalogue_entry["pkg_cd"],
            ))
        else:
            # No catalogue match — fall back to whatever was scraped from the
            # old invoice (may be blank; validate_header() will warn on that).
            sale_items.append(SaleItem(
                item_cls_cd = it.item_cls_cd,
                item_cd     = it.item_cd,
                item_nm     = it.item_nm,
                qty         = it.qty,
                prc         = it.prc,
                tax_ty_cd   = it.tax_ty_cd,
                # pkg_unit_cd / qty_unit_cd not exposed by either popup — use
                # the same defaults fill_kra.SaleItem / etims_mapper fallback use.
            ))

    return ReceiptHeader(
    cust_nm          = getattr(scraped_header, "cust_nm", None) or "Naivas Limited",
    cust_tin         = new_pin,
    cust_branch_nm   = getattr(scraped_header, "cust_branch_nm", None) or "Default Branch",
    cust_mbl_no      = getattr(scraped_header, "cust_mbl_no", None) or "N/A",
    pmt_ty_cd        = "07",  # display-only "OTHER" isn't scrapable as a code; matches codebase convention
    items            = sale_items,
    order_no         = getattr(scraped_header, "order_no", None) or "UNKNOWN",
    delivery_note_no = getattr(scraped_header, "delivery_note_no", None),
    grn_no           = getattr(scraped_header, "grn_no", None),
    invoice_no       = getattr(scraped_header, "invoice_no", None),
    store_no         = getattr(scraped_header, "store_no", None) or "001",
)

TAX_CODES = {"A", "B", "C", "D", "E"}


def validate_header(header: ReceiptHeader) -> tuple[list[str], list[str]]:
    """
    Sanity-check every value on a built ReceiptHeader before it goes anywhere
    near KRA. Returns (errors, warnings).

    errors   -> submission is blocked; these are things that must never go out
                (missing/"null" required fields, non-sane numbers).
    warnings -> logged but does NOT block; a blank item_cd is normal for items
                that were never in the item catalogue (KRA auto-assigns one),
                so it's flagged for visibility but not treated as a failure.
    """
    errors: list[str] = []
    warnings: list[str] = []

    def is_missing(v) -> bool:
        return v is None or (isinstance(v, str) and v.strip() == "")

    if is_missing(header.cust_tin):
        errors.append("cust_tin (buyer PIN) is missing/null")
    if is_missing(header.cust_nm):
        errors.append("cust_nm (buyer name) is missing/null")
    if is_missing(header.cust_branch_nm):
        warnings.append("cust_branch_nm is blank")
    if is_missing(header.order_no):
        warnings.append("order_no is blank")
    if is_missing(header.grn_no):
        warnings.append("grn_no is blank")
    if is_missing(header.invoice_no):
        warnings.append("invoice_no (non-fiscal ref) is blank")
    if is_missing(header.store_no):
        errors.append("store_no is missing/null")

    if not header.items:
        errors.append("no line items at all")

    for i, item in enumerate(header.items, start=1):
        tag = f"item #{i} ({item.item_nm or 'UNNAMED'})"
        if is_missing(item.item_nm):
            errors.append(f"{tag}: item_nm is missing/null")
        if is_missing(item.item_cls_cd):
            errors.append(f"{tag}: item_cls_cd is missing/null")
        if is_missing(item.item_cd):
            warnings.append(f"{tag}: item_cd is blank — KRA will auto-assign one")
        if is_missing(item.tax_ty_cd) or item.tax_ty_cd not in TAX_CODES:
            errors.append(f"{tag}: tax_ty_cd is missing/invalid ({item.tax_ty_cd!r})")
        if item.qty is None or item.qty <= 0:
            errors.append(f"{tag}: qty is missing/zero/negative ({item.qty!r})")
        if item.prc is None or item.prc < 0:
            errors.append(f"{tag}: prc is missing/negative ({item.prc!r})")

    if header.sum_tot_amt <= 0:
        errors.append(f"sum_tot_amt is zero/negative ({header.sum_tot_amt!r})")

    return errors, warnings


def file_credit_note(session: requests.Session, cfg: EtimsConfig, org_invc_no: str) -> str:
    common_hdrs = _common_headers(cfg)

    r1 = session.post(
        f"{cfg.base_url}{POPUP_INVOICE_PATH}",
        data={
            "tin": cfg.pin, "bhfId": POPUP_BHF_ID, "invcNo": org_invc_no,
            "popId": "popupInvoice", "popTitle": "Invoice Information",
            "popSize": "XL", "multiYn": "N", "callBackFnc": "",
        },
        headers=common_hdrs, timeout=TIMEOUT_S,
    )
    _raise_for_kra(r1, context="popupTrnsSalesInvoice (pre-refund)")

    r2 = session.post(
        f"{cfg.base_url}{POPUP_REFUND_PATH}",
        data={
            "invcNo": org_invc_no, "popId": "popupReceiptRefund",
            "popTitle": "Credit Note reason", "popSize": "S",
            "multiYn": "N", "callBackFnc": "",
        },
        headers=common_hdrs, timeout=TIMEOUT_S,
    )
    _raise_for_kra(r2, context="popupTrnsSalesReceiptRefund")

    r3 = session.post(
        f"{cfg.base_url}{REFUND_POST_PATH}",
        data={"orgInvcNo": org_invc_no, "rfdRsnCd": RFD_RSN_CD},
        headers={**common_hdrs, "Accept": "application/json, text/javascript, */*; q=0.01"},
        timeout=TIMEOUT_S,
    )
    _raise_for_kra(r3, context="insertTrnsSalesReceipRefund")

    try:
        return str(r3.json())[:500]
    except ValueError:
        return r3.text.strip()[:500]


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book8", default=BOOK8_PATH)
    ap.add_argument("--results", default=RESULTS_CSV)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--delay", type=float, default=DELAY_S)
    ap.add_argument("--new-pin", help="Correct buyer PIN to apply to every resubmitted invoice")
    ap.add_argument(
        "--flagged-comment", default=FLAGGED_COMMENT,
        help=f"Comment text (case-insensitive, substring match) that flags a row for "
             f"processing. Default: {FLAGGED_COMMENT!r}. Use this to reuse the script for "
             f"a different batch, e.g. --flagged-comment \"omission of buyers pin\"",
    )
    ap.add_argument(
        "--extra-invoice", action="append", default=[], dest="extra_invoices",
        help="Process an invoice NOT in Book8.xlsx (e.g. a bad resubmit that needs redoing, "
             "like 691). Repeatable: --extra-invoice 691 --extra-invoice 702",
    )
    ap.add_argument(
        "--cu-invoice-no", action="append", default=[], dest="cu_invoices",
        help="CU invoice number to resubmit, either the full string (e.g. ABC/566) or "
             "just the number (566). Repeatable. When given, Book8.xlsx is NOT read. "
             "Example: --cu-invoice-no 566 --cu-invoice-no ABC/702",
    )
    args = ap.parse_args()

    live = args.live and not args.dry_run

    if live and not args.new_pin:
        raise SystemExit("--new-pin is required for a --live run")

    if args.cu_invoices:
        # Explicit invoices: skip Excel entirely, and ignore ALREADY_DONE
        # since you've asked for these by name.
        pending: list[PendingInvoice] = []
        seen: set[str] = set()
        for raw in args.cu_invoices:
            org_no = parse_cu_invoice_no(raw)
            if org_no in seen:
                continue
            seen.add(org_no)
            pending.append(PendingInvoice(
                org_invc_no    = org_no,
                full_invc      = raw.strip(),
                amount         = 0.0,
                purchase_order = "(cli)",
            ))
    else:
        pending = load_pending_invoices(args.book8, flagged_comment=args.flagged_comment)

    for extra_no in args.extra_invoices:
        pending.append(PendingInvoice(
            org_invc_no    = str(extra_no),
            full_invc      = f"(manual) {extra_no}",
            amount         = 0.0,
            purchase_order = "(manual)",
        ))
    prior = load_previous_results(args.results)

    # Drop anything already fully done (both steps ok)
    todo: list[PendingInvoice] = []
    for p in pending:
        prev = prior.get(p.org_invc_no)
        if prev and prev.get("resubmit_status") == "ok" and prev.get("credit_note_status") == "ok":
            continue
        todo.append(p)

    if args.limit is not None:
        todo = todo[: args.limit]

    print(f"{'LIVE' if live else 'DRY RUN'} — {len(todo)} invoice(s) queued:")
    for p in todo:
        prev = prior.get(p.org_invc_no)
        state = ""
        if prev:
            state = f"  (resubmit={prev.get('resubmit_status')}, credit_note={prev.get('credit_note_status')})"
        print(f"  orgInvcNo={p.org_invc_no:>6}  amount={p.amount:>10,.2f}  PO={p.purchase_order}{state}")

    if not todo:
        print("Nothing to do.")
        return
    if not live:
        print("\nDry run only — no network calls made. Re-run with --live --new-pin <PIN> to execute.")
        return

    cfg = EtimsConfig(
        pin      = os.environ["KRA_PIN"],
        branch   = os.environ.get("KRA_BRANCH", "001"),
        username = os.environ["KRA_USERNAME"],
        password = os.environ["KRA_PASSWORD"],
    )

    session = _make_session()
    print("\nLogging in to eTIMS...")
    login(cfg, session)
    print("Login OK.\n")

    for i, inv in enumerate(todo, start=1):
        prev = prior.get(inv.org_invc_no) or {}
        resubmit_status  = prev.get("resubmit_status", "")
        resubmit_detail  = prev.get("resubmit_detail", "")
        credit_status    = prev.get("credit_note_status", "")
        credit_detail    = prev.get("credit_note_detail", "")

        print(f"[{i}/{len(todo)}] orgInvcNo={inv.org_invc_no}")

        # -- Step 1: resubmit with corrected PIN (skip if already done) -----
        if resubmit_status != "ok":
            print("  -> scraping old invoice + resubmitting with new PIN...", end=" ")
            try:
                header = build_corrected_header(inv.org_invc_no, args.new_pin, session, cfg)

                errors, warnings = validate_header(header)
                if warnings:
                    print()
                    for w in warnings:
                        print(f"     ! warning: {w}")
                if errors:
                    print("BLOCKED — validation failed, nothing was sent to KRA:")
                    for e in errors:
                        print(f"     x {e}")
                    resubmit_status = "validation_error"
                    resubmit_detail = "; ".join(errors)
                else:
                    result = _submit_receipt(session, cfg, header)
                    resubmit_status, resubmit_detail = "ok", str(result)[:500]
                    print(f"OK — {resubmit_detail[:120]}")
            except (ScrapeError, KraError) as exc:
                resubmit_status, resubmit_detail = "error", str(exc)
                print(f"FAILED — {exc}")
            except Exception as exc:  # noqa: BLE001
                resubmit_status, resubmit_detail = "error", f"Unexpected error: {exc}"
                print(f"FAILED — {exc}")
        else:
            print("  -> resubmit already OK from a previous run, skipping")

        # -- Step 2: credit-note the OLD invoice — only if resubmit succeeded
        if resubmit_status == "ok" and credit_status != "ok":
            print("  -> filing credit note against old invoice...", end=" ")
            try:
                credit_detail = file_credit_note(session, cfg, inv.org_invc_no)
                credit_status = "ok"
                print(f"OK — {credit_detail[:120]}")
            except KraError as exc:
                credit_status, credit_detail = "error", str(exc)
                print(f"FAILED — {exc}")
            except Exception as exc:  # noqa: BLE001
                credit_status, credit_detail = "error", f"Unexpected error: {exc}"
                print(f"FAILED — {exc}")
        elif resubmit_status != "ok":
            print("  -> skipping credit note — resubmit did not succeed, old invoice left standing on purpose")
        else:
            print("  -> credit note already OK from a previous run, skipping")

        append_result(args.results, {
            "org_invc_no":        inv.org_invc_no,
            "full_invc":          inv.full_invc,
            "amount":             inv.amount,
            "purchase_order":     inv.purchase_order,
            "resubmit_status":    resubmit_status,
            "resubmit_detail":    resubmit_detail,
            "credit_note_status": credit_status,
            "credit_note_detail": credit_detail,
            "timestamp":          datetime.now(timezone.utc).isoformat(),
        })

        if i < len(todo):
            time.sleep(args.delay)

    print(f"\nDone. See {args.results} for full detail.")
    print("Review any 'error' rows on the KRA portal manually before re-running.")


if __name__ == "__main__":
    main()