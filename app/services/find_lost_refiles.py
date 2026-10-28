"""
app/services/find_lost_refiles.py

READ-ONLY. Submits nothing, credits nothing.

Situation: invoices were credit-noted for "OMISSION OF BUYERS PIN", then
refiled as brand-new invoices carrying the buyer PIN — but the record of the
new invoice numbers was lost. This script finds them again on KRA.

How it searches (strongest evidence first)
───────────────────────────────────────────
  1. Pull the original invoice's popup to learn its Order No. / non-fiscal
     invoice ref. A refile copies the remark verbatim, so these are the
     fingerprint.
  2. Ask KRA's receipt list for that buyer's PIN only (custTin filter), over
     the window since the credit note, and keep positive receipts whose
     amount matches the credit note (credit notes are negative, so the
     original amount is abs(credit note amount)).
  3. Open each candidate's popup and compare Order No. / invoice ref to the
     original's.

Status per invoice
──────────────────
  CONFIRMED  candidate's Order No. or invoice ref matches the original's
  LIKELY     found under the buyer-PIN filter with matching amount, but the
             remark couldn't be compared — eyeball it on the portal
  POSSIBLE   amount matches only (found via the unfiltered list) — weak
  NOT_FOUND  nothing matching; it probably was never refiled

The amount match is against the receipt list's Summary Amount. A weaker
fallback also tries amount x 1.16 (in case the sheet figure was pre-VAT);
those hits are labelled "vat16" in the evidence.

Usage
─────
  # the five invoices from your credit-note list are built in:
  python -m app.services.find_lost_refiles

  # or paste a larger block of the same tab-separated export into a text
  # file and point at it:
  python -m app.services.find_lost_refiles --paste-file credit_notes.txt

Credentials: KRA_PIN, KRA_BRANCH (default "001"), KRA_USERNAME, KRA_PASSWORD
"""

from __future__ import annotations

import argparse
import csv
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from app.services.etims_invoice_scraper import parse_invoice_popup
from app.services.fill_kra import EtimsConfig, KraError, _make_session, login
from app.services.resubmit_wrong_pin_invoices import fetch_invoice_popup_html
from app.services.search_duplicates import ReceiptRow, fetch_all_receipts

env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

_HERE = Path(__file__).resolve().parent
REPORT_CSV = str(_HERE / "lost_refiles_report.csv")

VAT_RATE = 0.16


@dataclass
class CreditedInvoice:
    org_invc_no: str
    buyer_pin:   str
    amount:      float            # absolute value (positive)
    cn_date:     date | None      # date the credit note was filed


# From the credit-note list you pasted (all "OMISSION OF BUYERS PIN").
DEFAULT_CREDITED: list[CreditedInvoice] = [
    CreditedInvoice("638", "P042430258",   70.00, date(2026, 6, 30)),
    CreditedInvoice("636", "P042550375",  720.00, date(2026, 6, 30)),
    CreditedInvoice("644", "P042430177", 1242.50, date(2026, 7, 1)),
    CreditedInvoice("640", "P042430236", 1860.00, date(2026, 6, 30)),
    CreditedInvoice("643", "P042550280", 3207.00, date(2026, 7, 1)),
]

# <invoice>  <date> |<invoice>  <PIN>  <-amount>
_PASTE_RE = re.compile(
    r"KRACU\d+/(\d+)\s+(\d{1,2}/\d{1,2}/\d{4})\s*\|\s*KRACU\d+/\d+\s+"
    r"(P\d{9}[A-Z]?)\s+-?([\d,]+(?:\.\d+)?)",
    re.I,
)


def parse_paste(text: str) -> list[CreditedInvoice]:
    out: list[CreditedInvoice] = []
    seen: set[str] = set()
    for m in _PASTE_RE.finditer(text):
        no = m.group(1)
        if no in seen:
            continue
        seen.add(no)
        out.append(CreditedInvoice(
            org_invc_no = no,
            buyer_pin   = m.group(3).upper(),
            amount      = float(m.group(4).replace(",", "")),
            cn_date     = datetime.strptime(m.group(2), "%m/%d/%Y").date(),
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _norm(s) -> str:
    return str(s or "").strip().upper()


def _portal_date(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip()[:10], "%d/%m/%Y").date()
    except ValueError:
        return None


def _sort_key(row: ReceiptRow):
    return (_portal_date(row.rcpt_date) or date.max,
            int(row.receipt_no) if row.receipt_no.isdigit() else 0)


def read_popup(session, cfg: EtimsConfig, no: str, cache: dict) -> dict | None:
    """Order No / invoice ref / buyer PIN for a receipt. None if unreadable."""
    if no in cache:
        return cache[no]
    try:
        html = fetch_invoice_popup_html(session, cfg, no)
        h = parse_invoice_popup(html, receipt_no=no)
        info = {
            "order_no":   _norm(getattr(h, "order_no", "")),
            "invoice_no": _norm(getattr(h, "invoice_no", "")),
            "cust_tin":   _norm(getattr(h, "cust_tin", "")),
        }
    except Exception as exc:  # noqa: BLE001 — one bad popup must not kill the run
        print(f"    ! could not read popup for receipt {no}: {exc}")
        info = None
    cache[no] = info
    return info


@dataclass
class Cand:
    row:     ReceiptRow
    basis:   str                 # "exact" | "vat16"
    via_pin: bool
    hits:    list[str] = field(default_factory=list)
    note:    str = ""


def gather_candidates(
    rows: list[ReceiptRow], inv: CreditedInvoice,
    exclude: set[str], tol: float, via_pin: bool,
) -> list[Cand]:
    vat_amt = round(inv.amount * (1 + VAT_RATE), 2)
    out: list[Cand] = []
    for r in rows:
        if r.receipt_no in exclude or r.receipt_no == inv.org_invc_no:
            continue
        if r.amount <= 0:               # skips credit notes
            continue
        if abs(r.amount - inv.amount) <= tol:
            out.append(Cand(r, "exact", via_pin))
        elif abs(r.amount - vat_amt) <= tol:
            out.append(Cand(r, "vat16", via_pin))
    return sorted(out, key=lambda c: _sort_key(c.row))


def annotate(session, cfg, inv: CreditedInvoice, orig: dict | None,
             cands: list[Cand], popup_cache: dict) -> None:
    """Fill in remark-match hits and buyer-PIN sanity notes on each candidate."""
    for c in cands:
        info = read_popup(session, cfg, c.row.receipt_no, popup_cache)
        if not info:
            continue
        if orig:
            for key in ("order_no", "invoice_no"):
                if orig.get(key) and orig[key] == info.get(key):
                    c.hits.append(key)
        tin = info.get("cust_tin")
        if tin:
            if tin == _norm(inv.buyer_pin):
                c.note = "buyer PIN on KRA matches"
            else:
                c.note = f"buyer PIN on KRA is {tin}, expected {inv.buyer_pin}"
                c.via_pin = False       # PIN filter evidently didn't isolate it


def classify(cands: list[Cand]) -> tuple[str, Cand | None]:
    confirmed = [c for c in cands if c.hits]
    if confirmed:
        return "CONFIRMED", confirmed[0]
    pinned = [c for c in cands if c.via_pin]
    if pinned:
        return "LIKELY", pinned[0]
    if cands:
        return "POSSIBLE", cands[0]
    return "NOT_FOUND", None


REPORT_FIELDS = [
    "org_invc_no", "buyer_pin", "expected_amount", "status",
    "found_receipt_no", "found_date", "found_amount", "evidence", "all_candidates",
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paste-file", help="Text file containing the pasted credit-note rows "
                                         "(default: the 5 built-in invoices)")
    ap.add_argument("--days-before", type=int, default=7,
                    help="Start the KRA search this many days before the earliest credit note")
    ap.add_argument("--tolerance", type=float, default=0.5, help="Amount tolerance in KES")
    ap.add_argument("--report", default=REPORT_CSV)
    args = ap.parse_args()

    if args.paste_file:
        invoices = parse_paste(Path(args.paste_file).read_text(encoding="utf-8"))
        if not invoices:
            raise SystemExit(f"Could not parse any credit-note rows from {args.paste_file}")
    else:
        invoices = DEFAULT_CREDITED

    originals = {i.org_invc_no for i in invoices}
    dated = [i.cn_date for i in invoices if i.cn_date]
    start = (min(dated) - timedelta(days=args.days_before)) if dated else date(2026, 6, 1)
    end = date.today()
    start_s, end_s = start.strftime("%d/%m/%Y"), end.strftime("%d/%m/%Y")

    cfg = EtimsConfig(
        pin      = os.environ["KRA_PIN"],
        branch   = os.environ.get("KRA_BRANCH", "001"),
        username = os.environ["KRA_USERNAME"],
        password = os.environ["KRA_PASSWORD"],
    )
    session = _make_session()
    print("Logging in to eTIMS...")
    login(cfg, session)
    print(f"Login OK. Searching {start_s} -> {end_s} for {len(invoices)} invoice(s).\n")

    popup_cache: dict[str, dict | None] = {}
    pin_rows_cache: dict[str, list[ReceiptRow]] = {}
    unfiltered: list[list[ReceiptRow]] = []     # lazily filled, at most once

    results: list[dict] = []
    for n, inv in enumerate(invoices, start=1):
        print(f"[{n}/{len(invoices)}] invoice {inv.org_invc_no}  PIN {inv.buyer_pin}  KES {inv.amount:,.2f}")

        orig = read_popup(session, cfg, inv.org_invc_no, popup_cache)
        if orig:
            print(f"    original remark: Order No.={orig['order_no'] or '-'}  ref={orig['invoice_no'] or '-'}")

        # Pass 1: this buyer's receipts only
        if inv.buyer_pin not in pin_rows_cache:
            try:
                pin_rows_cache[inv.buyer_pin] = fetch_all_receipts(
                    session, cfg, start_s, end_s, cust_pin=inv.buyer_pin)
            except KraError as exc:
                print(f"    ! PIN-filtered search failed: {exc}")
                pin_rows_cache[inv.buyer_pin] = []
        pin_rows = pin_rows_cache[inv.buyer_pin]
        print(f"    {len(pin_rows)} receipt(s) under this PIN in the window")

        cands = gather_candidates(pin_rows, inv, originals, args.tolerance, via_pin=True)

        # Pass 2: only if pass 1 found nothing — everything in the window
        if not cands:
            if not unfiltered:
                print("    nothing under the PIN filter; pulling ALL receipts in the window (slower)...")
                unfiltered.append(fetch_all_receipts(session, cfg, start_s, end_s))
            cands = gather_candidates(unfiltered[0], inv, originals, args.tolerance, via_pin=False)

        annotate(session, cfg, inv, orig, cands, popup_cache)
        status, best = classify(cands)

        if best:
            ev = [f"{best.basis} amount match"]
            if best.hits:
                ev.append("remark matches on " + "+".join(best.hits))
            if best.note:
                ev.append(best.note)
            evidence = "; ".join(ev)
            print(f"    -> {status}: receipt {best.row.receipt_no} "
                  f"({best.row.rcpt_date}, KES {best.row.amount:,.2f}) — {evidence}")
        else:
            evidence = "no positive receipt with a matching amount in the window"
            print(f"    -> {status}: {evidence}")

        results.append({
            "org_invc_no":      inv.org_invc_no,
            "buyer_pin":        inv.buyer_pin,
            "expected_amount":  inv.amount,
            "status":           status,
            "found_receipt_no": best.row.receipt_no if best else "",
            "found_date":       best.row.rcpt_date if best else "",
            "found_amount":     best.row.amount if best else "",
            "evidence":         evidence,
            "all_candidates":   " | ".join(
                f"{c.row.receipt_no}({c.row.rcpt_date},{c.row.amount:,.2f},{c.basis}"
                f"{',remark-match' if c.hits else ''})" for c in cands),
        })

    with open(args.report, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REPORT_FIELDS)
        w.writeheader()
        w.writerows(results)

    print("\n" + "=" * 78)
    print(f"  {'ORIGINAL':<9} {'STATUS':<10} {'NEW RECEIPT':<12} {'DATE':<12} {'AMOUNT':>12}")
    print("=" * 78)
    for r in results:
        amt = f"{r['found_amount']:,.2f}" if r["found_amount"] != "" else ""
        print(f"  {r['org_invc_no']:<9} {r['status']:<10} {r['found_receipt_no']:<12} "
              f"{r['found_date']:<12} {amt:>12}")
    print("=" * 78)
    print(f"Full detail: {args.report}")

    missing = [r["org_invc_no"] for r in results if r["status"] == "NOT_FOUND"]
    if missing:
        flags = " ".join(f"--cu-invoice-no {n}" for n in missing)
        print(f"\n{len(missing)} not found on KRA — these look like they were never refiled:")
        print(" ".join(missing))
        print("\nTo refile them (test one first with --limit 1):")
        print(f"  python -m app.services.resubmit_wrong_pin_invoices --live --new-pin <PIN> {flags}")
        print("\nCAUTION: that script also files a credit note against the old invoice after")
        print("resubmitting. These already HAVE credit notes, so its second step would be a")
        print("duplicate attempt. Don't run it as-is for these — ask for a resubmit-only mode.")


if __name__ == "__main__":
    main()
