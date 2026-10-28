"""
app/services/audit_item_codes.py

Reads a "KRA eTIMS — Original Invoices" export (the sheet you download from
the portal listing invoices for a period) and checks EVERY invoice on it
directly against KRA — not against old terminal logs — to see whether its
line items actually have real registered item codes, or are sitting on a
blank/auto-assigned one.

This is the authoritative way to answer "which of these need a credit note
+ regenerate", since:
  - the resubmit script's warnings were only ever printed to your terminal,
    never saved anywhere durable
  - some of these invoices went out BEFORE the SUKUMAWIKI / KAHURURAH /
    BROCOLLI catalogue fixes landed, so even ones that looked "fine" at the
    time may now be resolvable and worth redoing

Nothing here submits or credits anything — it's read-only. Output is a CSV
you can review, and a plain list of invoice numbers ready to feed straight
into --extra-invoice on resubmit_wrong_pin_invoices.py.

Usage
─────
  python -m app.services.audit_item_codes --input original_invoices_20260806_20260807.xlsx

Credentials: same env vars as the rest of your codebase —
  KRA_PIN, KRA_USERNAME, KRA_PASSWORD
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from pathlib import Path

import openpyxl
import requests
from dotenv import load_dotenv

from app.services.etims_item_map import resolve_item
from app.services.etims_invoice_scraper import parse_receipt_popup
from app.services.fill_kra import EtimsConfig, KraError, _make_session, login
from app.services.resubmit_wrong_pin_invoices import fetch_receipt_popup_html

RETRY_ATTEMPTS = 3
RETRY_DELAY_S  = 5.0

env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

_HERE = Path(__file__).resolve().parent
RESULTS_CSV = str(_HERE / "item_code_audit.csv")


def load_receipt_numbers(path: str) -> list[str]:
    """
    Reads the 'Receipt No' column from the first sheet of the export.
    Skips the title row and any non-numeric receipt numbers (e.g. 'TOTALS').
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))

    # first row is a merged title, second row is the real header
    header_row_idx = next(
        i for i, row in enumerate(rows) if row and row[0] == "Receipt No"
    )
    header = rows[header_row_idx]
    col = {name: idx for idx, name in enumerate(header) if name}

    receipt_nos: list[str] = []
    for row in rows[header_row_idx + 1:]:
        val = row[col["Receipt No"]]
        if val is None:
            continue
        s = str(val).strip()
        if not s.isdigit():
            continue
        receipt_nos.append(s)
    return receipt_nos


def _fetch_receipt_popup_with_retry(session, cfg: EtimsConfig, invc_no: str) -> str:
    """
    This is a read-only GET/POST (no state changes on KRA's side), so unlike
    the resubmit/credit-note scripts, retrying a timeout here is safe — it
    can't create a duplicate invoice or credit note.
    """
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fetch_receipt_popup_html(session, cfg, invc_no)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < RETRY_ATTEMPTS:
                print(f"(timeout, retry {attempt}/{RETRY_ATTEMPTS - 1}...) ", end="", flush=True)
                time.sleep(RETRY_DELAY_S)
    raise last_exc


def audit_one(session, cfg: EtimsConfig, invc_no: str) -> dict:
    """
    Returns a dict summarising this invoice's item-code health:
      invc_no, item_count, blank_count, mismatch_count,
      now_resolvable_count, needs_fix, items_detail

    An item counts as needing a fix if either:
      - its item_cd is blank (KRA never assigned/registered one), or
      - it HAS a code, but your catalogue would now assign a DIFFERENT one
        (i.e. it's sitting on KRA's generic auto-assigned placeholder from
        before this item was in your catalogue, not your registered code).
    """
    html = _fetch_receipt_popup_with_retry(session, cfg, invc_no)
    items = parse_receipt_popup(html)

    blank_count = 0
    mismatch_count = 0
    now_resolvable_count = 0
    details = []
    for it in items:
        is_blank = not it.item_cd
        catalogue_entry = resolve_item(it.item_nm)
        catalogue_cd = catalogue_entry["itemCd"] if catalogue_entry else None

        is_mismatch = bool(catalogue_cd) and not is_blank and catalogue_cd != it.item_cd
        would_resolve_now = bool(catalogue_cd) and is_blank

        if is_blank:
            blank_count += 1
        if is_mismatch:
            mismatch_count += 1
        if would_resolve_now:
            now_resolvable_count += 1

        tag = ""
        if is_blank:
            tag = ",NOW_RESOLVABLE" if would_resolve_now else ",BLANK_NO_MATCH"
        elif is_mismatch:
            tag = f",MISMATCH(catalogue={catalogue_cd})"

        details.append(f"{it.item_nm}[cd={it.item_cd or 'BLANK'}{tag}]")

    needs_fix = (blank_count + mismatch_count) > 0

    return {
        "invc_no":              invc_no,
        "item_count":           len(items),
        "blank_count":          blank_count,
        "mismatch_count":       mismatch_count,
        "now_resolvable_count": now_resolvable_count,
        "needs_fix":            needs_fix,
        "items_detail":         "; ".join(details),
    }


AUDIT_FIELDS = [
    "invc_no", "item_count", "blank_count", "mismatch_count",
    "now_resolvable_count", "needs_fix", "items_detail",
]


def load_already_checked(results_path: str) -> dict[str, dict]:
    """invc_no -> last row seen for it, so a crash never loses prior progress."""
    out: dict[str, dict] = {}
    p = Path(results_path)
    if not p.exists():
        return out
    with p.open(newline="") as f:
        for row in csv.DictReader(f):
            # Only count it as "done" if it wasn't itself an error row —
            # a prior timeout/failure should be retried, not skipped forever.
            if row.get("blank_count") != "ERROR":
                out[row["invc_no"]] = row
    return out


def append_result(results_path: str, row: dict) -> None:
    p = Path(results_path)
    is_new = not p.exists()
    with p.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=AUDIT_FIELDS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="Path to the exported invoices .xlsx")
    ap.add_argument("--results", default=RESULTS_CSV)
    args = ap.parse_args()

    receipt_nos = load_receipt_numbers(args.input)
    already = load_already_checked(args.results)
    todo = [n for n in receipt_nos if n not in already]

    print(f"Found {len(receipt_nos)} invoice(s) in {args.input}")
    if already:
        print(f"{len(already)} already checked in a previous run (in {args.results}), skipping those.")
    print(f"{len(todo)} left to check.\n")

    if not todo:
        print("Nothing left to do.")
    else:
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

        for i, invc_no in enumerate(todo, start=1):
            print(f"[{i}/{len(todo)}] checking invoice {invc_no}...", end=" ")
            try:
                result = audit_one(session, cfg, invc_no)
                if result["needs_fix"]:
                    bits = []
                    if result["blank_count"]:
                        bits.append(f"{result['blank_count']} blank")
                    if result["mismatch_count"]:
                        bits.append(f"{result['mismatch_count']} mismatched vs catalogue")
                    flag = " <-- NEEDS FIX (" + ", ".join(bits) + ")"
                else:
                    flag = " — all items match catalogue"
                print(f"{result['item_count']} items{flag}")
            except Exception as exc:  # noqa: BLE001 — broad on purpose: any failure here
                # (KraError, requests exceptions after retries exhausted, parse
                # errors, etc.) must be logged and moved past, never crash the
                # whole run and lose everything checked so far.
                print(f"FAILED — {exc}")
                result = {
                    "invc_no": invc_no, "item_count": "", "blank_count": "ERROR",
                    "mismatch_count": "", "now_resolvable_count": "",
                    "needs_fix": "", "items_detail": str(exc),
                }

            append_result(args.results, result)

    # Summarise from the full results file (covers this run + any prior runs).
    # A retried invoice leaves BOTH its old error row and new success row in
    # the CSV (append-only, by design, so nothing is ever silently lost) — so
    # dedupe here, keeping only the latest row per invoice, or a stale error
    # would still show up in the summary after a successful retry.
    latest_by_invc: dict[str, dict] = {}
    with open(args.results, newline="") as f:
        for row in csv.DictReader(f):
            latest_by_invc[row["invc_no"]] = row
    all_rows = list(latest_by_invc.values())

    needs_redo = [r["invc_no"] for r in all_rows if r.get("needs_fix") == "True"]
    errored = [r["invc_no"] for r in all_rows if r.get("blank_count") == "ERROR"]

    print(f"\nFull detail in {args.results}")
    if errored:
        print(f"\n{len(errored)} invoice(s) failed to check (network/KRA errors) — rerun the same command to retry just these:")
        print(" ".join(errored))
    print(f"\n{len(needs_redo)} of {len(receipt_nos)} invoice(s) need a redo:")
    print(" ".join(needs_redo))
    if needs_redo:
        print("\nTo redo just these with the current script:")
        extra_flags = " ".join(f"--extra-invoice {n}" for n in needs_redo)
        print(f"  python -m app.services.resubmit_wrong_pin_invoices --live --new-pin <PIN> {extra_flags}")


if __name__ == "__main__":
    main()