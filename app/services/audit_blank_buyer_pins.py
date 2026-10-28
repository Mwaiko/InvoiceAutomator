"""
app/services/audit_blank_buyer_pins.py

Finds every eTIMS invoice whose buyer PIN (custTin / "custKraNo") is blank on
KRA's side, and lists them in an Excel file.

Read-only: nothing is submitted, credited or changed on KRA.

How it works
────────────
  1. Logs in and pages through the receipt list for a date range
     (reuses search_duplicates.fetch_all_receipts).
  2. Keeps invoices only — credit notes (amount < 0 / type "Credit...") are
     skipped, since they inherit the original's PIN.
  3. Opens popupTrnsSalesInvoice for each one and reads the buyer PIN.
  4. Classifies each invoice:
        BLANK    buyer PIN field exists and is empty / "null" / "-" etc.
        HAS_PIN  buyer PIN present
        ERROR    could not check (timeout, HTTP error, field not in the
                 popup). NEVER counted as blank — an ERROR row means "go
                 look", not "PIN is missing".
  5. Writes progress to a CSV after every invoice, so a crash / Ctrl-C loses
     nothing; re-running skips everything already checked (ERROR rows are
     retried). The Excel file is built from that CSV at the end.

Output workbook
───────────────
  "Blank Buyer PIN"  – the invoices you need to act on
  "All Checked"      – every invoice audited, with its status
  "Summary"          – counts + totals (live formulas)

Usage
─────
  python -m app.services.audit_blank_buyer_pins --start 20260101 --end 20260630
  python -m app.services.audit_blank_buyer_pins --date 20260807
  python -m app.services.audit_blank_buyer_pins --start 20260101 --end 20260630 --out blank_pins.xlsx

  # Rebuild the Excel from the checkpoint CSV without touching KRA:
  python -m app.services.audit_blank_buyer_pins --start 20260101 --end 20260630 --excel-only

Credentials: KRA_PIN, KRA_BRANCH (default "001"), KRA_USERNAME, KRA_PASSWORD
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.services.fill_kra import EtimsConfig, KraError, _make_session, login
import app.services.resubmit_wrong_pin_invoices as _rw
from app.services.resubmit_wrong_pin_invoices import fetch_invoice_popup_html
from app.services.search_duplicates import ReceiptRow, fetch_all_receipts

# KRA's popup endpoint gets slow under load. fetch_invoice_popup_html reads
# TIMEOUT_S from its own module at call time, so raise it there (30s -> 90s).
# This only affects this audit run's process; the resubmit/credit-note
# scripts are untouched.
_rw.TIMEOUT_S = 90

env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

_HERE = Path(__file__).resolve().parent
CHECKPOINT_CSV = str(_HERE / "blank_pin_audit.csv")

RETRY_ATTEMPTS   = 3        # read-only POST, so retrying a timeout is safe
RETRY_DELAYS_S   = (10, 30, 60)   # backoff between attempts (was a flat 5s)
DETAIL_DELAY_S   = 1.0            # was 0.3 — gentler on KRA's portal
MAX_CONSEC_FAILS = 8        # stop early if the session has clearly died

# Input names KRA has used / could use for the buyer PIN in the popup.
# custTin is confirmed from export_original_invoices.py; the others are
# fallbacks so a rename doesn't silently turn every invoice into "blank".
PIN_FIELD_NAMES = ("custTin", "custKraNo", "custPin")

# KRA sometimes renders literal placeholder text instead of an empty value.
_NULLISH = {"", "null", "none", "n/a", "na", "-", "--", "nil", "undefined"}


class PinFieldMissing(Exception):
    """Popup came back but has no buyer-PIN input at all (layout change / wrong page)."""


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _input_val(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("input", attrs={"name": name})
    return _clean(tag.get("value", "") if tag else "")


def _amount(raw: str) -> float:
    try:
        return float(raw.replace(",", "").replace(" ", ""))
    except (ValueError, AttributeError):
        return 0.0


def parse_buyer_pin(html: str) -> dict:
    """
    Returns {cust_pin, taxable_amount, vat_amount, total_amount, non_fiscal}.
    cust_pin is "" when the field exists but is blank/placeholder.
    Raises PinFieldMissing if no PIN input exists in the popup at all.
    """
    soup = BeautifulSoup(html, "html.parser")

    pin_tag = None
    for name in PIN_FIELD_NAMES:
        pin_tag = soup.find("input", attrs={"name": name})
        if pin_tag is not None:
            break
    if pin_tag is None:
        raise PinFieldMissing(
            f"no <input> named any of {PIN_FIELD_NAMES} in popupTrnsSalesInvoice "
            f"(response length {len(html)})"
        )

    raw_pin = _clean(pin_tag.get("value", ""))
    pin = "" if raw_pin.lower() in _NULLISH else raw_pin

    return {
        "cust_pin":       pin,
        "taxable_amount": _amount(_input_val(soup, "totTaxblAmt")),
        "vat_amount":     _amount(_input_val(soup, "totTaxAmt")),
        "total_amount":   _amount(_input_val(soup, "sumTtAmt") or _input_val(soup, "sumTotAmt")),
        "non_fiscal":     _input_val(soup, "remark"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# KRA fetch (with safe retry — this endpoint only reads)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_with_retry(session, cfg: EtimsConfig, receipt_no: str) -> str:
    last: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fetch_invoice_popup_html(session, cfg, receipt_no)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last = exc
            if attempt < RETRY_ATTEMPTS:
                wait = RETRY_DELAYS_S[min(attempt - 1, len(RETRY_DELAYS_S) - 1)]
                print(f"(timeout, waiting {wait}s, retry {attempt}/{RETRY_ATTEMPTS - 1}...) ",
                      end="", flush=True)
                time.sleep(wait)
                if attempt >= 2:
                    # The connection/session may be wedged on KRA's side —
                    # log in again before the final attempt. Login is
                    # idempotent and read-only for our purposes.
                    try:
                        login(cfg, session)
                    except Exception:  # noqa: BLE001 — best effort; final attempt decides
                        pass
    raise last  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# Which receipts to audit
# ─────────────────────────────────────────────────────────────────────────────

def select_invoices(all_rows: list[ReceiptRow]) -> list[ReceiptRow]:
    """Invoices only: drop credit notes (negative amount or type containing 'credit')."""
    return [r for r in all_rows if r.amount > 0 and "credit" not in r.rcpt_type.lower()]


def credit_note_hints(all_rows: list[ReceiptRow]) -> dict[tuple, int]:
    """
    (abs amount, buyer name) -> number of credit notes seen for it.
    Date is deliberately NOT part of the key: a credit note is usually filed
    days after the invoice. This is only a hint that the invoice may already
    have been credited — it is not proof.
    """
    hints: Counter = Counter()
    for r in all_rows:
        if r.amount < 0 or "credit" in r.rcpt_type.lower():
            hints[(round(abs(r.amount), 2), r.cust_name.lower().strip())] += 1
    return dict(hints)


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint CSV
# ─────────────────────────────────────────────────────────────────────────────

FIELDS = [
    "receipt_no", "rcpt_date", "rcpt_type", "cust_name",
    "pin_status", "cust_pin",
    "taxable_amount", "vat_amount", "total_amount", "non_fiscal",
    "credit_notes_same_buyer_amount", "error", "checked_at",
]


def load_checkpoint(path: str) -> dict[str, dict]:
    """receipt_no -> latest row. ERROR rows are returned too (caller decides)."""
    out: dict[str, dict] = {}
    p = Path(path)
    if p.exists():
        with p.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                out[row["receipt_no"]] = row   # later rows win
    return out


def append_checkpoint(path: str, row: dict) -> None:
    p = Path(path)
    is_new = not p.exists()
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            w.writeheader()
        w.writerow(row)


# ─────────────────────────────────────────────────────────────────────────────
# Excel
# ─────────────────────────────────────────────────────────────────────────────

HDR_BG, HDR_FG, ALT_BG, BORDER = "1F4E79", "FFFFFF", "EBF3FB", "AABDD6"
RED_BG, AMBER_BG = "FDE2E1", "FFF2CC"
THIN = Side(style="thin", color=BORDER)
TBDR = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
AMT = "#,##0.00"

COLS = [  # (header, width, key, number_format)
    ("Receipt No",                 12, "receipt_no",     None),
    ("Date",                       12, "rcpt_date",      None),
    ("Buyer Name",                 34, "cust_name",      None),
    ("Buyer PIN",                  16, "cust_pin",       None),
    ("PIN Status",                 12, "pin_status",     None),
    ("Taxable Amt (KES)",          18, "taxable_amount", AMT),
    ("VAT (KES)",                  14, "vat_amount",     AMT),
    ("Total Amt (KES)",            18, "total_amount",   AMT),
    ("Credit notes, same buyer+amt", 16, "credit_notes_same_buyer_amount", "0"),
    ("Non-Fiscal Data",            55, "non_fiscal",     None),
    ("Error",                      40, "error",          None),
]
NUMERIC = {"taxable_amount", "vat_amount", "total_amount", "credit_notes_same_buyer_amount"}


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _write_table(ws, rows: list[dict], title: str, drop_error_col: bool) -> tuple[int, int]:
    """Writes title/header/data (+ totals). Returns (first_data_row, last_data_row)."""
    cols = [c for c in COLS if not (drop_error_col and c[2] == "error")]
    ws.sheet_view.showGridLines = False
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(cols))
    t = ws.cell(row=1, column=1, value=title)
    t.font = Font(name="Arial", bold=True, size=13, color=HDR_BG)
    t.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 28

    for ci, (name, width, _k, _f) in enumerate(cols, 1):
        c = ws.cell(row=2, column=ci, value=name)
        c.font = Font(name="Arial", bold=True, color=HDR_FG, size=10)
        c.fill = PatternFill("solid", start_color=HDR_BG)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = TBDR
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.row_dimensions[2].height = 36
    ws.freeze_panes = "A3"

    first = 3
    for i, row in enumerate(rows):
        r = first + i
        status = row.get("pin_status", "")
        fill = None
        if status == "ERROR":
            fill = AMBER_BG
        elif i % 2 == 1:
            fill = ALT_BG
        for ci, (_n, _w, key, fmt) in enumerate(cols, 1):
            val = _num(row.get(key)) if key in NUMERIC else row.get(key, "")
            c = ws.cell(row=r, column=ci, value=val)
            c.font = Font(name="Arial", size=10)
            c.border = TBDR
            c.alignment = Alignment(vertical="center", wrap_text=(key in ("non_fiscal", "error")))
            if fill:
                c.fill = PatternFill("solid", start_color=fill)
            if fmt:
                c.number_format = fmt
        if status == "BLANK":
            pc = ws.cell(row=r, column=[c[2] for c in cols].index("cust_pin") + 1)
            pc.value = "(blank)"
            pc.font = Font(name="Arial", size=10, bold=True, color="C00000")

    last = first + len(rows) - 1
    if rows:
        tot = last + 1
        ws.merge_cells(start_row=tot, start_column=1, end_row=tot, end_column=5)
        lbl = ws.cell(row=tot, column=1, value="TOTALS")
        lbl.font = Font(name="Arial", bold=True, size=10)
        lbl.alignment = Alignment(horizontal="right")
        for ci, (_n, _w, key, _f) in enumerate(cols, 1):
            if key in ("taxable_amount", "vat_amount", "total_amount"):
                L = get_column_letter(ci)
                c = ws.cell(row=tot, column=ci, value=f"=SUM({L}{first}:{L}{last})")
                c.number_format = AMT
                c.font = Font(name="Arial", bold=True, size=10)
        for ci in range(1, len(cols) + 1):
            c = ws.cell(row=tot, column=ci)
            c.fill = PatternFill("solid", start_color="D6E4F0")
            c.border = TBDR
    ws.auto_filter.ref = f"A2:{get_column_letter(len(cols))}{max(last, 2)}"
    return first, last


def build_excel(rows: list[dict], period: str, out_path: str) -> None:
    rows = sorted(rows, key=lambda r: int(r["receipt_no"]) if str(r["receipt_no"]).isdigit() else 0)
    blank = [r for r in rows if r["pin_status"] == "BLANK"]
    errors = [r for r in rows if r["pin_status"] == "ERROR"]

    wb = Workbook()
    ws_blank = wb.active
    ws_blank.title = "Blank Buyer PIN"
    _write_table(ws_blank, blank, f"eTIMS invoices with BLANK buyer PIN   |   Period: {period}", drop_error_col=True)

    ws_all = wb.create_sheet("All Checked")
    a_first, a_last = _write_table(
        ws_all, rows, f"All invoices audited   |   Period: {period}", drop_error_col=False
    )

    ws_s = wb.create_sheet("Summary")
    ws_s.sheet_view.showGridLines = False
    ws_s.column_dimensions["A"].width = 38
    ws_s.column_dimensions["B"].width = 26
    status_rng = f"'All Checked'!$E${a_first}:$E${max(a_last, a_first)}"
    total_rng = f"'All Checked'!$H${a_first}:$H${max(a_last, a_first)}"
    lines = [
        ("Report generated",              datetime.now().strftime("%d/%m/%Y %H:%M")),
        ("Period",                        period),
        ("Invoices audited",              f"=COUNTA('All Checked'!$A${a_first}:$A${max(a_last, a_first)})" if rows else 0),
        ("With buyer PIN",                f'=COUNTIF({status_rng},"HAS_PIN")'),
        ("BLANK buyer PIN",               f'=COUNTIF({status_rng},"BLANK")'),
        ("Could not check (ERROR)",       f'=COUNTIF({status_rng},"ERROR")'),
        ("Total value of BLANK-PIN (KES)", f'=SUMIF({status_rng},"BLANK",{total_rng})'),
    ]
    for ri, (k, v) in enumerate(lines, 2):
        kc = ws_s.cell(row=ri, column=1, value=k)
        kc.font = Font(name="Arial", bold=True, size=10, color=HDR_BG)
        kc.border = TBDR
        vc = ws_s.cell(row=ri, column=2, value=v)
        vc.font = Font(name="Arial", size=10)
        vc.border = TBDR
        vc.alignment = Alignment(horizontal="right")
        if k.startswith("Total value"):
            vc.number_format = AMT
    note_r = len(lines) + 3
    notes = [
        "Notes",
        "• BLANK = buyer PIN field present on KRA but empty / 'null' / '-'.",
        "• ERROR = could not be checked (timeout, HTTP error, PIN field absent from popup).",
        "  These are NOT counted as blank. Re-run the script to retry them.",
        "• 'Credit notes, same buyer+amt' is a hint only (date is not matched):",
        "  a non-zero value means the invoice may already have been credited.",
    ]
    for i, txt in enumerate(notes):
        c = ws_s.cell(row=note_r + i, column=1, value=txt)
        c.font = Font(name="Arial", size=10, bold=(i == 0))

    wb.save(out_path)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _portal_date(s: str) -> str:
    try:
        return datetime.strptime(s, "%Y%m%d").strftime("%d/%m/%Y")
    except ValueError:
        sys.exit(f"Invalid date {s!r}. Use YYYYMMDD, e.g. 20260807")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=date.today().strftime("%Y%m%d"), help="Single date YYYYMMDD (default today)")
    ap.add_argument("--start", help="Range start YYYYMMDD (use with --end)")
    ap.add_argument("--end", help="Range end YYYYMMDD")
    ap.add_argument("--out", help="Output .xlsx (default blank_buyer_pins_<start>_<end>.xlsx)")
    ap.add_argument("--checkpoint", default=CHECKPOINT_CSV, help="Progress CSV (enables resume)")
    ap.add_argument("--excel-only", action="store_true", help="Rebuild Excel from the checkpoint CSV; no KRA calls")
    args = ap.parse_args()

    if args.start and args.end:
        lbl_s, lbl_e = args.start, args.end
    else:
        lbl_s = lbl_e = args.date
    start_p, end_p = _portal_date(lbl_s), _portal_date(lbl_e)
    period = start_p if start_p == end_p else f"{start_p} -> {end_p}"
    out_path = args.out or f"blank_buyer_pins_{lbl_s}_{lbl_e}.xlsx"

    # The checkpoint may hold several periods; this run's receipts decide what
    # goes into the Excel, so it is always scoped to the requested range.
    scope: set[str] = set()

    if not args.excel_only:
        cfg = EtimsConfig(
            pin=os.environ["KRA_PIN"],
            branch=os.environ.get("KRA_BRANCH", "001"),
            username=os.environ["KRA_USERNAME"],
            password=os.environ["KRA_PASSWORD"],
        )
        session = _make_session()
        print("Logging in to eTIMS...")
        login(cfg, session)
        print("Login OK.\n")

        print(f"Fetching receipt list {start_p} -> {end_p} ...")
        all_rows = fetch_all_receipts(session=session, cfg=cfg, start_dt=start_p, end_dt=end_p)
        invoices = select_invoices(all_rows)
        hints = credit_note_hints(all_rows)
        scope = {r.receipt_no for r in invoices}
        print(f"{len(all_rows)} receipt(s) in range -> {len(invoices)} invoice(s) (credit notes excluded)\n")

        done = load_checkpoint(args.checkpoint)
        todo = [r for r in invoices if r.receipt_no not in done or done[r.receipt_no]["pin_status"] == "ERROR"]
        print(f"{len(invoices) - len(todo)} already checked, {len(todo)} to check.\n")

        consec_fail = 0
        for i, r in enumerate(todo, 1):
            print(f"[{i}/{len(todo)}] receipt {r.receipt_no} ...", end=" ", flush=True)
            row = {
                "receipt_no": r.receipt_no, "rcpt_date": r.rcpt_date, "rcpt_type": r.rcpt_type,
                "cust_name": r.cust_name, "pin_status": "", "cust_pin": "",
                "taxable_amount": "", "vat_amount": "", "total_amount": r.amount, "non_fiscal": "",
                "credit_notes_same_buyer_amount": hints.get((round(abs(r.amount), 2), r.cust_name.lower().strip()), 0),
                "error": "", "checked_at": datetime.now().isoformat(timespec="seconds"),
            }
            try:
                d = parse_buyer_pin(fetch_with_retry(session, cfg, r.receipt_no))
                row.update(d)
                row["total_amount"] = d["total_amount"] or r.amount
                row["pin_status"] = "BLANK" if not d["cust_pin"] else "HAS_PIN"
                print("BLANK PIN  <--" if row["pin_status"] == "BLANK" else f"ok ({d['cust_pin']})")
                consec_fail = 0
            except Exception as exc:  # noqa: BLE001 — never lose progress to one bad invoice
                row["pin_status"], row["error"] = "ERROR", f"{type(exc).__name__}: {exc}"[:300]
                print(f"ERROR — {exc}")
                consec_fail += 1

            append_checkpoint(args.checkpoint, row)

            if consec_fail >= MAX_CONSEC_FAILS:
                print(f"\n{MAX_CONSEC_FAILS} failures in a row — session has probably expired or KRA is down.")
                print("Progress is saved. Re-run the same command to resume.")
                break
            time.sleep(DETAIL_DELAY_S)

    latest = load_checkpoint(args.checkpoint)
    rows = [v for k, v in latest.items() if (not scope or k in scope)]
    if not rows:
        sys.exit("No audited invoices to write.")
    build_excel(rows, period, out_path)

    c = Counter(r["pin_status"] for r in rows)
    print(f"\nWrote {out_path}")
    print(f"  audited: {len(rows)}   blank PIN: {c['BLANK']}   with PIN: {c['HAS_PIN']}   could not check: {c['ERROR']}")
    if c["ERROR"]:
        print("  Re-run the same command to retry the ERROR rows.")


if __name__ == "__main__":
    main()