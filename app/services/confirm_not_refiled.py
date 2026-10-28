"""
app/services/confirm_not_refiled.py

PREFLIGHT for Book6.xlsx (invoices flagged "OMISSION OF BUYERS PIN").

Before resubmit_wrong_pin_invoices.py touches anything, this checks — for
real, against KRA, not just against your local results CSVs — whether each
invoice already has a corrected twin out there. It does NOT submit or credit
anything. Read-only.

Why a local CSV check alone isn't enough
─────────────────────────────────────────
resubmit_wrong_pin_invoices.py already skips org_invc_no's that show
resubmit_status == "ok" in resubmit_results.csv. That's correct, but it only
protects against THIS script re-running on itself. It says nothing about
whether that invoice was already fixed by hand, by a run whose results file
got lost, or before this tooling existed. So this script also asks KRA
directly.

How "already refiled" is detected
──────────────────────────────────
When an invoice is resubmitted, the non-fiscal remark — including
"Order No.<PURCHASEORDER>" — is copied over verbatim; only the buyer PIN
changes. So:

  1. Pull every receipt KRA has for the date window covering these invoices
     (via search_duplicates.fetch_all_receipts).
  2. For any receipt that (a) isn't the original invoice itself, (b) has a
     matching amount, and (c) was raised on/after the original's date —
     fetch ITS invoice popup and read the Order No. out of the remark.
  3. If that Order No. matches this invoice's PURCHASEORDER, this invoice
     already has a corrected twin on KRA. Skip it.

Everything else is reported SAFE_TO_REFILE, and written out to a
Book8-shaped .xlsx (Sheet1 / comment / INVOICE / TAXABLEAMOUNT /
PURCHASEORDER) so it can be fed straight into the existing script:

  python -m app.services.resubmit_wrong_pin_invoices \\
      --book8 book6_safe_to_refile.xlsx \\
      --flagged-comment "OMISSION OF BUYERS PIN" \\
      --live --new-pin <PIN>

Usage
─────
  python -m app.services.confirm_not_refiled --input Book6.xlsx

Credentials: same env vars as the rest of your codebase —
  KRA_PIN, KRA_BRANCH (default "001"), KRA_USERNAME, KRA_PASSWORD
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl
from dotenv import load_dotenv

from app.services.etims_invoice_scraper import parse_invoice_popup
from app.services.fill_kra import EtimsConfig, KraError, _make_session, login
from app.services.resubmit_wrong_pin_invoices import (
    ALREADY_DONE as RESUBMIT_ALREADY_DONE,
    fetch_invoice_popup_html,
    load_previous_results as load_resubmit_results,
)
from app.services.search_duplicates import fetch_all_receipts

env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

_HERE = Path(__file__).resolve().parent
DEFAULT_BOOK6      = str(_HERE / "Book6.xlsx")
REPORT_CSV         = str(_HERE / "confirm_not_refiled_report.csv")
SAFE_TO_REFILE_XLSX = str(_HERE / "book6_safe_to_refile.xlsx")
RESUBMIT_RESULTS   = str(_HERE / "resubmit_results.csv")

DEFAULT_FLAG_TEXT  = "OMISSION OF BUYERS PIN"
AMOUNT_TOLERANCE   = 0.5   # KES — guards against float/rounding noise


@dataclass
class PendingInvoice:
    org_invc_no:    str
    full_invc:      str
    vendor:         str
    invoice_date:   date | None
    amount:         float
    purchase_order: str
    flag_text:      str


# ─────────────────────────────────────────────────────────────────────────────
# Book6.xlsx — NOTE: different shape from Book8.xlsx
#   sheet   : "Sheet3" (not "Sheet1")
#   flag col: "Description" (not "comment")
# ─────────────────────────────────────────────────────────────────────────────

def load_book6(path: str, flag_text: str = DEFAULT_FLAG_TEXT) -> list[PendingInvoice]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]  # "Sheet3" — first (only) sheet, don't hardcode the name
    rows = list(ws.iter_rows(values_only=True))
    header, data_rows = rows[0], rows[1:]
    col = {name: idx for idx, name in enumerate(header) if name}

    flag_text_u = flag_text.strip().upper()
    pending: list[PendingInvoice] = []
    for row in data_rows:
        if row is None or row[0] is None:
            continue
        desc = str(row[col["Description"]] or "").strip().upper()
        if flag_text_u not in desc:
            continue

        full_invc = str(row[col["INVOICE"]] or "").strip()
        m = re.search(r"/(\d+)$", full_invc)
        if not m:
            print(f"  ! Skipping row — could not parse invoice number from {full_invc!r}")
            continue
        org_invc_no = m.group(1)

        raw_date = row[col.get("INVOICEDATE")]
        invoice_date = raw_date.date() if isinstance(raw_date, datetime) else None

        pending.append(PendingInvoice(
            org_invc_no    = org_invc_no,
            full_invc      = full_invc,
            vendor         = str(row[col.get("VENDORNAME")] or ""),
            invoice_date   = invoice_date,
            amount         = float(row[col["TAXABLEAMOUNT"]] or 0),
            purchase_order = str(row[col["PURCHASEORDER"]] or "").strip(),
            flag_text      = str(row[col["Description"]] or "").strip(),
        ))
    return pending


# ─────────────────────────────────────────────────────────────────────────────
# Local-records check (fast, no network)
# ─────────────────────────────────────────────────────────────────────────────

def check_local_records(pending: list[PendingInvoice], results_path: str) -> dict[str, str]:
    """org_invc_no -> reason string, for anything already-done per local records."""
    already: dict[str, str] = {}
    for p in pending:
        if p.org_invc_no in RESUBMIT_ALREADY_DONE:
            already[p.org_invc_no] = "in ALREADY_DONE set (resubmit_wrong_pin_invoices.py)"

    prior = load_resubmit_results(results_path)
    for p in pending:
        if p.org_invc_no in already:
            continue
        prev = prior.get(p.org_invc_no)
        if prev and prev.get("resubmit_status") == "ok":
            already[p.org_invc_no] = f"resubmit_results.csv shows resubmit_status=ok (resubmitted as detail: {prev.get('resubmit_detail', '')[:80]})"
    return already


# ─────────────────────────────────────────────────────────────────────────────
# Live KRA check
# ─────────────────────────────────────────────────────────────────────────────

def check_live_kra(
    session, cfg: EtimsConfig,
    pending: list[PendingInvoice],
    window_start: date,
    window_end: date,
) -> dict[str, str]:
    """
    Returns org_invc_no -> evidence string for invoices that already have a
    resubmitted twin live on KRA (matched by amount + Order No. in remark).
    """
    start_portal = window_start.strftime("%d/%m/%Y")
    end_portal   = window_end.strftime("%d/%m/%Y")
    print(f"Pulling KRA receipts {start_portal} -> {end_portal} to cross-check against...")
    all_receipts = fetch_all_receipts(session, cfg, start_portal, end_portal)
    print(f"  {len(all_receipts)} receipt(s) fetched.\n")

    org_nos = {p.org_invc_no for p in pending}
    already: dict[str, str] = {}
    popup_cache: dict[str, str | None] = {}  # receipt_no -> order_no scraped (None = failed)

    for p in pending:
        if p.org_invc_no in already:
            continue
        if not p.purchase_order:
            continue  # nothing to match against — leave for manual review

        candidates = [
            r for r in all_receipts
            if r.receipt_no != p.org_invc_no
            and r.receipt_no not in org_nos  # never compare two Book6 originals to each other
            and r.amount > 0                 # skip credit notes
            and abs(r.amount - p.amount) <= AMOUNT_TOLERANCE
        ]
        if p.invoice_date:
            candidates = [
                r for r in candidates
                if _parse_portal_date(r.rcpt_date) is None
                or _parse_portal_date(r.rcpt_date) >= p.invoice_date
            ]

        for cand in candidates:
            if cand.receipt_no not in popup_cache:
                try:
                    html = fetch_invoice_popup_html(session, cfg, cand.receipt_no)
                    header = parse_invoice_popup(html, receipt_no=cand.receipt_no)
                    popup_cache[cand.receipt_no] = header.order_no
                except Exception as exc:  # noqa: BLE001 — don't let one bad popup kill the run
                    print(f"    ! could not read popup for candidate receipt {cand.receipt_no}: {exc}")
                    popup_cache[cand.receipt_no] = None

            cand_order_no = popup_cache[cand.receipt_no]
            if cand_order_no and cand_order_no.strip().upper() == p.purchase_order.strip().upper():
                already[p.org_invc_no] = (
                    f"KRA receipt {cand.receipt_no} ({cand.rcpt_date}, "
                    f"KES {cand.amount:,.2f}) already carries Order No.{cand_order_no} "
                    f"— this invoice looks already refiled"
                )
                break

    return already


def _parse_portal_date(s: str) -> date | None:
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

REPORT_FIELDS = ["org_invc_no", "full_invc", "vendor", "purchase_order", "amount", "status", "evidence"]


def write_report(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_safe_to_refile_book8(rows: list[dict], pending_by_no: dict[str, PendingInvoice], path: str) -> None:
    """Book8-shaped .xlsx: Sheet1, header row with comment/INVOICE/TAXABLEAMOUNT/PURCHASEORDER,
    so it drops straight into resubmit_wrong_pin_invoices.py --book8."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["comment", "INVOICE", "TAXABLEAMOUNT", "PURCHASEORDER"])
    for row in rows:
        if row["status"] != "SAFE_TO_REFILE":
            continue
        p = pending_by_no[row["org_invc_no"]]
        ws.append([p.flag_text, p.full_invc, p.amount, p.purchase_order])
    wb.save(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=DEFAULT_BOOK6, help="Path to Book6.xlsx")
    ap.add_argument("--flag-text", default=DEFAULT_FLAG_TEXT,
                     help=f"Text to match in the Description column (default: {DEFAULT_FLAG_TEXT!r})")
    ap.add_argument("--resubmit-results", default=RESUBMIT_RESULTS,
                     help="Path to resubmit_results.csv to check locally first")
    ap.add_argument("--report", default=REPORT_CSV)
    ap.add_argument("--safe-book8", default=SAFE_TO_REFILE_XLSX,
                     help="Where to write the Book8-shaped .xlsx of invoices confirmed safe to refile")
    ap.add_argument("--skip-live", action="store_true",
                     help="Only do the local-records check; skip the live KRA cross-check")
    ap.add_argument("--window-days-before", type=int, default=5,
                     help="Days before the earliest Book6 invoice date to start the KRA pull from")
    args = ap.parse_args()

    pending = load_book6(args.input, flag_text=args.flag_text)
    print(f"Found {len(pending)} invoice(s) in {args.input} flagged {args.flag_text!r}\n")
    if not pending:
        return

    pending_by_no = {p.org_invc_no: p for p in pending}

    # ── Step 1: local records ────────────────────────────────────────────────
    already_local = check_local_records(pending, args.resubmit_results)
    print(f"{len(already_local)} already accounted for in local records.\n")

    remaining = [p for p in pending if p.org_invc_no not in already_local]

    # ── Step 2: live KRA cross-check ─────────────────────────────────────────
    already_live: dict[str, str] = {}
    if remaining and not args.skip_live:
        dated = [p.invoice_date for p in remaining if p.invoice_date]
        window_start = (min(dated) - timedelta(days=args.window_days_before)) if dated else date(2021, 1, 1)
        window_end = date.today()

        cfg = EtimsConfig(
            pin      = os.environ["KRA_PIN"],
            branch   = os.environ.get("KRA_BRANCH", "001"),
            username = os.environ["KRA_USERNAME"],
            password = os.environ["KRA_PASSWORD"],
        )
        session = _make_session()
        print("Logging in to eTIMS...")
        login(cfg, session)
        print("Login OK.\n")

        try:
            already_live = check_live_kra(session, cfg, remaining, window_start, window_end)
        except KraError as exc:
            print(f"! Live KRA check failed: {exc}")
            print("! Falling back to local-records-only result — treat SAFE_TO_REFILE below with caution.")
    elif args.skip_live:
        print("--skip-live set: not cross-checking against KRA. Local records only.\n")

    # ── Assemble report ───────────────────────────────────────────────────────
    report_rows = []
    for p in pending:
        if p.org_invc_no in already_local:
            status, evidence = "ALREADY_REFILED_LOCAL", already_local[p.org_invc_no]
        elif p.org_invc_no in already_live:
            status, evidence = "ALREADY_REFILED_ON_KRA", already_live[p.org_invc_no]
        elif not p.purchase_order:
            status, evidence = "NEEDS_MANUAL_CHECK", "no PURCHASEORDER value to match against — can't confirm live"
        else:
            status, evidence = "SAFE_TO_REFILE", ""

        report_rows.append({
            "org_invc_no":    p.org_invc_no,
            "full_invc":      p.full_invc,
            "vendor":         p.vendor,
            "purchase_order": p.purchase_order,
            "amount":         p.amount,
            "status":         status,
            "evidence":       evidence,
        })

    write_report(report_rows, args.report)
    write_safe_to_refile_book8(report_rows, pending_by_no, args.safe_book8)

    # ── Summary ────────────────────────────────────────────────────────────
    counts: dict[str, int] = {}
    for r in report_rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    print("\n" + "=" * 78)
    print("  SUMMARY")
    print("=" * 78)
    for status in ["SAFE_TO_REFILE", "ALREADY_REFILED_LOCAL", "ALREADY_REFILED_ON_KRA", "NEEDS_MANUAL_CHECK"]:
        if counts.get(status):
            print(f"  {status:<26} {counts[status]}")
    print("=" * 78)

    for r in report_rows:
        if r["status"] != "SAFE_TO_REFILE":
            print(f"  [{r['status']}] invc {r['org_invc_no']} (PO {r['purchase_order']}): {r['evidence']}")

    print(f"\nFull detail   : {args.report}")
    print(f"Safe-to-refile Book8-shaped file written to: {args.safe_book8}")
    if counts.get("SAFE_TO_REFILE"):
        print(
            "\nNext step, once you've eyeballed the report:\n"
            f"  python -m app.services.resubmit_wrong_pin_invoices \\\n"
            f"      --book8 {args.safe_book8} \\\n"
            f"      --flagged-comment {args.flag_text!r} \\\n"
            f"      --live --limit 3 --new-pin <PIN>   # test batch first\n"
        )


if __name__ == "__main__":
    main()