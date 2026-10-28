"""
app/services/file_credit_notes.py

Files eTIMS credit notes ("receipt refunds") on the KRA portal for every
invoice in Book8.xlsx flagged with comment "WRONG BUYERS PIN", so they can
be resubmitted afterwards with the correct buyer PIN.

Lives alongside fill_kra.py in app/services/ and reuses its exact login /
session machinery so behaviour matches your existing submission path (same
headers, same cookie handling, same zero-retry policy on POSTs).

Put Book8.xlsx in this same folder (app/services/), or pass --book8 with a
path of your choosing.

Flow per invoice (mirrors the captured browser trace in invoice_process.txt):
  1. GET  /app/ebm/trns/popup/popupTrnsSalesInvoice          (open invoice popup)
  2. GET  /app/ebm/trns/popup/popupTrnsSalesReceiptRefund     (open credit-note-reason popup)
  3. POST /app/ebm/trns/sales/insertTrnsSalesReceipRefund     (file the credit note)

NO RETRIES on the POST step — same rationale as fill_kra.py: KRA sometimes
accepts a request but responds slowly, and retrying could file a duplicate
credit note. Any failure is recorded and left for manual review.

Usage
─────
  # 1. Dry run — just shows what WOULD be filed, makes no network calls
  python file_credit_notes.py --dry-run

  # 2. Test batch — actually files credit notes, but only for the first N
  #    pending invoices, so you can check the KRA portal before doing the rest
  python file_credit_notes.py --live --limit 3

  # 3. Full run — everything not already done and not already recorded
  #    successful in the results file
  python file_credit_notes.py --live

Credentials are read from the same env vars fill_kra.py / etims_tasks.py use:
  KRA_PIN, KRA_BRANCH (default "001"), KRA_USERNAME, KRA_PASSWORD
(You can also point to a .env file with --env-file /path/to/.env)

Re-running is safe: results are appended to credit_note_results.csv, and any
invoice already logged there with status "ok" is skipped automatically on
the next run — so you never need to hand-maintain the "already done" list
beyond what's in ALREADY_DONE below.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import openpyxl
import requests
from dotenv import load_dotenv

# ── Reuse your existing, already-tested login/session code ──────────────────
from app.services.fill_kra import (
    EtimsConfig,
    KraError,
    _make_session,
    _raise_for_kra,
    login,
)

# Same .env loading pattern as app/workers/etims_tasks.py
env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# Book8.xlsx and the results CSV are expected in this same folder
# (app/services/) by default — override with --book8 / --results if needed.
_HERE = Path(__file__).resolve().parent
BOOK8_PATH        = str(_HERE / "Book8.xlsx")
RESULTS_CSV       = str(_HERE / "credit_note_results.csv")
FLAGGED_COMMENT   = "WRONG BUYERS PIN"

# Invoices you told me you already filed manually — always skipped.
ALREADY_DONE: set[str] = {"566", "515"}

# Confirmed from your captured browser trace (invoice 515, which is one of
# these 28, comment = "WRONG BUYERS PIN") — change here if KRA's dropdown
# uses a different code for this reason on your account.
RFD_RSN_CD = "07"

POPUP_INVOICE_PATH = "/app/ebm/trns/popup/popupTrnsSalesInvoice"
POPUP_REFUND_PATH  = "/app/ebm/trns/popup/popupTrnsSalesReceiptRefund"
REFUND_POST_PATH   = "/app/ebm/trns/sales/insertTrnsSalesReceipRefund"
SALES_INDEX_PATH   = "/app/ebm/trns/sales/indexTrnsSalesReceipt"

TIMEOUT_S     = 30
DELAY_S       = 2.0   # pause between invoices — be gentle on KRA's portal


@dataclass
class PendingInvoice:
    org_invc_no: str      # e.g. "566"  (orgInvcNo KRA expects)
    full_invc:   str      # e.g. "KRACU0200021805/566"
    amount:      float
    purchase_order: str


def load_pending_invoices(path: str, flagged_comment: str = FLAGGED_COMMENT) -> list[PendingInvoice]:
    """Read Book8.xlsx and return every row matching flagged_comment not already done."""
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


def load_already_succeeded(results_path: str) -> set[str]:
    """Invoice numbers already logged as 'ok' in a previous run."""
    done: set[str] = set()
    p = Path(results_path)
    if not p.exists():
        return done
    with p.open(newline="") as f:
        for row in csv.DictReader(f):
            if row.get("status") == "ok":
                done.add(row["org_invc_no"])
    return done


def append_result(results_path: str, row: dict) -> None:
    p = Path(results_path)
    is_new = not p.exists()
    with p.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "org_invc_no", "full_invc", "amount", "purchase_order",
            "status", "detail", "timestamp",
        ])
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def file_one_credit_note(
    session: requests.Session,
    cfg: EtimsConfig,
    inv: PendingInvoice,
) -> tuple[str, str]:
    """
    Files a single credit note. Returns (status, detail).
    status is "ok" or "error".
    Mirrors the 3-step browser flow exactly (popup, popup, POST).
    """
    base = cfg.base_url

    common_hdrs = {
        "Accept":          "text/html, */*; q=0.01",
        "Content-Type":    "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin":          base,
        "Referer":         f"{base}{SALES_INDEX_PATH}",
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "cors",
        "Sec-Fetch-Site":  "same-origin",
        "X-Requested-With": "XMLHttpRequest",
    }

    # Step 1: open the invoice info popup (as the browser does before refunding)
    r1 = session.post(
        f"{base}{POPUP_INVOICE_PATH}",
        data={
            "tin":        cfg.pin,
            "bhfId":      "00",   # popup endpoints expect "00", not cfg.branch ("001")
            "invcNo":     inv.org_invc_no,
            "popId":      "popupInvoice",
            "popTitle":   "Invoice Information",
            "popSize":    "XL",
            "multiYn":    "N",
            "callBackFnc": "",
        },
        headers=common_hdrs,
        timeout=TIMEOUT_S,
    )
    _raise_for_kra(r1, context="popupTrnsSalesInvoice")

    # Step 2: open the credit-note-reason popup
    r2 = session.post(
        f"{base}{POPUP_REFUND_PATH}",
        data={
            "invcNo":     inv.org_invc_no,
            "popId":      "popupReceiptRefund",
            "popTitle":   "Credit Note reason",
            "popSize":    "S",
            "multiYn":    "N",
            "callBackFnc": "",
        },
        headers=common_hdrs,
        timeout=TIMEOUT_S,
    )
    _raise_for_kra(r2, context="popupTrnsSalesReceiptRefund")

    # Step 3: actually file the credit note. NO retry on this call.
    r3 = session.post(
        f"{base}{REFUND_POST_PATH}",
        data={
            "orgInvcNo": inv.org_invc_no,
            "rfdRsnCd":  RFD_RSN_CD,
        },
        headers={**common_hdrs, "Accept": "application/json, text/javascript, */*; q=0.01"},
        timeout=TIMEOUT_S,
    )
    _raise_for_kra(r3, context="insertTrnsSalesReceipRefund")

    try:
        body = r3.json()
    except ValueError:
        body = {"raw": r3.text.strip()[:300]}

    return "ok", str(body)[:500]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book8", default=BOOK8_PATH, help="Path to Book8.xlsx")
    ap.add_argument("--results", default=RESULTS_CSV, help="CSV to log results / dedupe across runs")
    ap.add_argument("--live", action="store_true", help="Actually call KRA. Without this, it's a dry run.")
    ap.add_argument("--dry-run", action="store_true", help="Explicit dry run (default if --live not given)")
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N pending invoices")
    ap.add_argument("--delay", type=float, default=DELAY_S, help="Seconds to wait between invoices")
    ap.add_argument(
        "--flagged-comment", default=FLAGGED_COMMENT,
        help=f"Comment text (case-insensitive, substring match) that flags a row for "
             f"processing. Default: {FLAGGED_COMMENT!r}. Use this to reuse the script for "
             f"a different batch, e.g. --flagged-comment \"omission of buyers pin\"",
    )
    args = ap.parse_args()

    live = args.live and not args.dry_run

    pending = load_pending_invoices(args.book8, flagged_comment=args.flagged_comment)
    already_ok = load_already_succeeded(args.results)
    pending = [p for p in pending if p.org_invc_no not in already_ok]

    if args.limit is not None:
        pending = pending[: args.limit]

    print(f"{'LIVE' if live else 'DRY RUN'} — {len(pending)} invoice(s) queued:")
    for p in pending:
        print(f"  orgInvcNo={p.org_invc_no:>6}  amount={p.amount:>10,.2f}  PO={p.purchase_order}")

    if not pending:
        print("Nothing to do.")
        return

    if not live:
        print("\nDry run only — no network calls made. Re-run with --live to actually file these.")
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

    ok_count, err_count = 0, 0
    for i, inv in enumerate(pending, start=1):
        print(f"[{i}/{len(pending)}] Filing credit note for orgInvcNo={inv.org_invc_no} ...", end=" ")
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        try:
            status, detail = file_one_credit_note(session, cfg, inv)
            print(f"OK — {detail[:120]}")
            ok_count += 1
        except KraError as exc:
            status, detail = "error", str(exc)
            print(f"FAILED — {exc}")
            err_count += 1
        except Exception as exc:  # noqa: BLE001
            status, detail = "error", f"Unexpected error: {exc}"
            print(f"FAILED — {exc}")
            err_count += 1

        append_result(args.results, {
            "org_invc_no":    inv.org_invc_no,
            "full_invc":      inv.full_invc,
            "amount":         inv.amount,
            "purchase_order": inv.purchase_order,
            "status":         status,
            "detail":         detail,
            "timestamp":      ts,
        })

        if i < len(pending):
            time.sleep(args.delay)

    print(f"\nDone. {ok_count} succeeded, {err_count} failed. See {args.results} for details.")
    if err_count:
        print("Review the failures on the KRA portal manually before re-running — "
              "do NOT blindly retry a credit note that may have already gone through.")


if __name__ == "__main__":
    main()