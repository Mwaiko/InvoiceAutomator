"""
search_duplicates.py  –  Phase 1: Search & List duplicate KRA eTIMS receipts.

Connects to the KRA eTIMS portal, pages through ALL receipts for a given date
range, groups them by (amount + customer + date), and flags every receipt that
appears more than once.

SMART CREDIT NOTE LOGIC:
  If you have 12 positive receipts of 1,800 KES and 6 credit notes of -1,800 KES:
    - You need to keep: 1 receipt
    - You need to void: 11 receipts
    - Credit notes already issued: 6
    - New credit notes needed: 11 - 6 = 5

Usage
─────
  # Search all time:
  python search_duplicates.py --start 20210101 --end 20260429

  # Search a specific date:
  python search_duplicates.py --date 20260429

  # Search today:
  python search_duplicates.py

  # Filter by customer PIN:
  python search_duplicates.py --start 20210101 --end 20260429 --cust-pin P000111222C

  # Show ALL receipts (not just duplicates):
  python search_duplicates.py --start 20210101 --end 20260429 --all

  # Save raw HTML from page 1 for debugging:
  python search_duplicates.py --start 20210101 --end 20260429 --save-html page1.html
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from typing import NamedTuple

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ── reuse login + session machinery from fill_kra.py ──────────────────────────
try:
    from fill_kra import EtimsConfig, _make_session, login, KraError, BASE_URL
except ImportError:
    sys.exit(
        "❌  fill_kra.py not found in the same directory.\n"
        "    Place search_duplicates.py alongside fill_kra.py and try again."
    )

log = logging.getLogger(__name__)

LIST_PATH  = "/app/ebm/trns/sales/trnsSalesReceiptList"
INDEX_PATH = "/app/ebm/trns/sales/indexTrnsSalesReceipt"
TIMEOUT_S  = 30


# ─────────────────────────────────────────────────────────────────────────────
# DATA
# ─────────────────────────────────────────────────────────────────────────────

class ReceiptRow(NamedTuple):
    receipt_no: str    # KRA sequential ID, e.g. "534"
    invoice_no: str    # same as receipt_no from this list view
    cust_name:  str    # buyer name
    amount:     float  # Summary Amount (col 8)
    rcpt_date:  str    # e.g. "27/04/2026"
    rcpt_type:  str    # "Invoice", "Credit Note", etc.
    row_index:  int    # 0-based position across all pages (for ordering)


# ─────────────────────────────────────────────────────────────────────────────
# FETCH  (paginated)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_all_receipts(
    session:  requests.Session,
    cfg:      EtimsConfig,
    start_dt: str,
    end_dt:   str,
    cust_pin: str = "",
    save_html: str | None = None,
) -> list[ReceiptRow]:
    """
    Page through trnsSalesReceiptList (10 rows per page) and return every row.
    Stops when a page returns fewer than 10 rows.
    """
    url  = f"{cfg.base_url}{LIST_PATH}"
    hdrs = {
        "Accept":         "text/html, */*; q=0.01",
        "Content-Type":   "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin":         cfg.base_url,
        "Referer":        f"{cfg.base_url}{INDEX_PATH}",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    all_rows: list[ReceiptRow] = []
    page = 1

    while True:
        payload = [
            ("page",     str(page)),
            ("dtDiv",    "D"),
            ("startDt",  start_dt),
            ("endDt",    end_dt),
            ("invcNo",   ""),
            ("rcptTyCd", ""),
            ("custTin",  cust_pin),
        ]

        log.info("  -> Fetching page %d  (rows so far: %d)", page, len(all_rows))
        r = session.post(url, data=payload, headers=hdrs, timeout=TIMEOUT_S)

        if r.status_code == 401:
            raise KraError("Session expired (HTTP 401). Re-login required.")
        if not r.ok:
            raise KraError(f"HTTP {r.status_code}: {r.text[:200]}")

        if page == 1 and save_html:
            with open(save_html, "w", encoding="utf-8") as fh:
                fh.write(r.text)
            log.info("  -> Page 1 HTML saved to %s", save_html)

        page_rows = _parse_page(r.text, row_offset=len(all_rows))
        log.info("  -> Page %d: got %d row(s)", page, len(page_rows))

        all_rows.extend(page_rows)

        if len(page_rows) < 10:
            break

        page += 1

    log.info("  Total rows fetched: %d", len(all_rows))
    return all_rows


# ─────────────────────────────────────────────────────────────────────────────
# PARSE
# ─────────────────────────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_amount(raw: str) -> float:
    try:
        return float(raw.replace(",", "").replace(" ", ""))
    except ValueError:
        return 0.0


def _parse_page(html: str, row_offset: int = 0) -> list[ReceiptRow]:
    """
    Parse one page of trnsSalesReceiptList HTML.

    Column order confirmed from portal HTML:
      0: Invoice number  (KRA seq ID)
      1: Receipt number
      2: Buyer Name
      3: Sale date
      4: Receipt type
      5: Total Item Count
      6: Total Taxable Amount
      7: VAT
      8: Summary Amount
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[ReceiptRow] = []

    table = (
        soup.find("table", summary=re.compile(r"Invoice number", re.I)) or
        soup.find("table")
    )
    if not table:
        log.warning("No table found in page HTML.")
        return rows

    tbody = table.find("tbody") or table
    for idx, tr in enumerate(tbody.find_all("tr")):
        cells = tr.find_all("td")
        if len(cells) < 9:
            continue

        invoice_no = _clean(cells[0].get_text())
        receipt_no = _clean(cells[1].get_text())
        cust_name  = _clean(cells[2].get_text())
        rcpt_date  = _clean(cells[3].get_text())
        rcpt_type  = _clean(cells[4].get_text())
        amount     = _parse_amount(_clean(cells[8].get_text()))

        if not receipt_no:
            continue

        rows.append(ReceiptRow(
            receipt_no = receipt_no,
            invoice_no = invoice_no,
            cust_name  = cust_name,
            amount     = amount,
            rcpt_date  = rcpt_date,
            rcpt_type  = rcpt_type,
            row_index  = row_offset + idx,
        ))

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# DUPLICATE DETECTION (WITH SMART CREDIT NOTE LOGIC)
# ─────────────────────────────────────────────────────────────────────────────

def find_duplicates(rows: list[ReceiptRow]) -> dict[tuple, dict]:
    """
    Group by (absolute amount, customer name, date).
    Any group with more than one receipt (combining positive + negative) is flagged.
    
    Returns a dict where:
      key = (absolute_amount, customer_name, date)
      value = {
          'positive': [ReceiptRow, ...],  # regular receipts
          'negative': [ReceiptRow, ...],  # credit notes already issued
          'abs_amount': float,
          'cust_name': str,
          'rcpt_date': str,
      }
    """
    groups: dict[tuple, dict] = defaultdict(lambda: {
        'positive': [],
        'negative': [],
        'abs_amount': 0.0,
        'cust_name': '',
        'rcpt_date': '',
    })
    
    for row in rows:
        abs_amount = abs(row.amount)
        key = (abs_amount, row.cust_name.lower().strip(), row.rcpt_date)
        
        groups[key]['abs_amount'] = abs_amount
        groups[key]['cust_name'] = row.cust_name
        groups[key]['rcpt_date'] = row.rcpt_date
        
        if row.amount >= 0:
            groups[key]['positive'].append(row)
        else:
            groups[key]['negative'].append(row)
    
    # Keep only groups that have duplicates (multiple positive receipts)
    return {
        k: v for k, v in groups.items() 
        if len(v['positive']) > 1
    }


# ─────────────────────────────────────────────────────────────────────────────
# DISPLAY
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_amount(v: float) -> str:
    return f"KES {v:>12,.2f}"


def print_report(
    duplicates: dict[tuple, dict],
    all_rows:   list[ReceiptRow],
    show_all:   bool = False,
) -> None:

    # Calculate how many credit notes we actually need to issue
    total_dupes = 0
    credit_notes_needed = 0
    
    for group in duplicates.values():
        pos_count = len(group['positive'])
        neg_count = len(group['negative'])
        dupes_in_group = pos_count - 1  # Keep 1, void the rest
        new_cn_needed = max(0, dupes_in_group - neg_count)  # Already have some credit notes
        
        total_dupes += dupes_in_group
        credit_notes_needed += new_cn_needed

    print("\n" + "=" * 100)
    print("  KRA eTIMS  -  DUPLICATE RECEIPT FINDER  (WITH SMART CREDIT NOTE LOGIC)")
    print("=" * 100)
    print(f"  Total receipts fetched        : {len(all_rows)}")
    print(f"  Duplicate groups found        : {len(duplicates)}")
    print(f"  Extra receipts to void        : {total_dupes}  (these need Credit Notes)")
    print(f"  Existing credit notes issued  : {sum(len(g['negative']) for g in duplicates.values())}")
    print(f"  NEW credit notes to issue     : {credit_notes_needed}  ⭐ ONLY THESE")
    print("=" * 100)

    if not duplicates:
        print("\n  No duplicates detected for this date range.\n")
        return

    # ── Per-group detail ──────────────────────────────────────────────────────
    for group_idx, ((amount, cust, dt), group_data) in enumerate(
        sorted(duplicates.items(), key=lambda x: -x[0][0]), start=1
    ):
        pos_rows = group_data['positive']
        neg_rows = group_data['negative']
        dupes_to_void = len(pos_rows) - 1
        new_cn_needed = max(0, dupes_to_void - len(neg_rows))

        keep_row    = pos_rows[0]
        remove_rows = pos_rows[1:]

        print(f"\n  +-- Group {group_idx:>2}  |  {_fmt_amount(group_data['abs_amount'])}  |  {cust}  |  {dt}")
        print(f"  |   {len(pos_rows)} receipts  |  Credit notes already issued: {len(neg_rows)}  |  NEW credit notes to issue: {new_cn_needed}")
        print(f"  |")
        print(f"  |   ✅  KEEP    → Receipt No: {keep_row.receipt_no:<12}  {keep_row.rcpt_date}  {_fmt_amount(keep_row.amount)}")

        for row in remove_rows:
            already_covered = neg_rows and remove_rows.index(row) < len(neg_rows)
            cn_note = "  (credit note already issued)" if already_covered else "  ⭐ NEW credit note needed"
            print(f"  |   ❌  REMOVE  → Receipt No: {row.receipt_no:<12}  {row.rcpt_date}  {_fmt_amount(row.amount)}{cn_note}")

        print(f"  +{'─'*96}")

    # ── Summary: receipts that STILL NEED credit notes ─────────────────────────
    print("\n" + "-" * 100)
    print("  RECEIPTS THAT STILL NEED CREDIT NOTES  ⭐ (NEW ones to issue)")
    print("-" * 100)
    print(f"  {'Receipt No':<14} {'Date':<14} {'Amount':>16}  Customer")
    print(f"  {'─'*14} {'─'*14} {'─'*16}  {'─'*40}")

    total_new_cn = 0.0
    receipts_to_process = []
    
    for (amount, cust, dt), group_data in sorted(
        duplicates.items(), key=lambda x: -x[0][0]
    ):
        pos_rows = group_data['positive']
        neg_rows = group_data['negative']
        dupes_to_void = len(pos_rows) - 1
        new_cn_needed = max(0, dupes_to_void - len(neg_rows))
        
        # Get the duplicate receipts (skip the first, which is the original)
        duplicates_list = pos_rows[1:]
        
        # Only process the ones we haven't already issued credit notes for
        for idx, row in enumerate(duplicates_list):
            # If we've already issued a credit note for this amount, skip it
            if idx < len(neg_rows):
                continue  # Skip ones already covered by existing credit notes
            
            receipts_to_process.append((row, group_data['abs_amount']))
            total_new_cn += row.amount

    for row, abs_amount in receipts_to_process:
        print(
            f"  {row.receipt_no:<14} {row.rcpt_date:<14}"
            f" {_fmt_amount(row.amount)}  {row.cust_name}"
        )

    print(f"  {'─'*14} {'─'*14} {'─'*16}  {'─'*40}")
    print(f"  {'TOTAL NEW CN':<30} {_fmt_amount(total_new_cn)}")
    print("-" * 100)
    print()

    # ── Optional: all receipts ────────────────────────────────────────────────
    if show_all:
        print("\n  ALL RECEIPTS")
        print(f"  {'Receipt No':<14} {'Date':<14} {'Amount':>16}  {'Type':<14}  Customer")
        print(f"  {'─'*14} {'─'*14} {'─'*16}  {'─'*14}  {'─'*40}")
        for row in all_rows:
            print(
                f"  {row.receipt_no:<14} {row.rcpt_date:<14}"
                f" {_fmt_amount(row.amount)}  {row.rcpt_type:<14}  {row.cust_name}"
            )
        print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _yyyymmdd_to_portal(s: str) -> str:
    try:
        return datetime.strptime(s, "%Y%m%d").strftime("%d/%m/%Y")
    except ValueError:
        sys.exit(f"Invalid date '{s}'. Use YYYYMMDD format, e.g. 20260429")


def main() -> None:
    today_str = date.today().strftime("%Y%m%d")

    p = argparse.ArgumentParser(
        description="Fetch KRA eTIMS receipt list and identify duplicates.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--date",      default=today_str,
                   help="Single date to search (YYYYMMDD, default: today)")
    p.add_argument("--start",     default=None,
                   help="Start of date range (YYYYMMDD). Overrides --date.")
    p.add_argument("--end",       default=None,
                   help="End of date range (YYYYMMDD). Overrides --date.")
    p.add_argument("--cust-pin",  default="",
                   help="Filter by customer KRA PIN (optional).")
    p.add_argument("--all",       action="store_true",
                   help="Also print every receipt, not just duplicates.")
    p.add_argument("--save-html", default=None, metavar="FILE",
                   help="Save the raw HTML from page 1 to a file for debugging.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable debug logging.")
    args = p.parse_args()

    logging.basicConfig(
        level   = logging.DEBUG if args.verbose else logging.INFO,
        format  = "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt = "%H:%M:%S",
    )

    username = os.environ.get("KRA_USERNAME", "")
    password = os.environ.get("KRA_PASSWORD", "")
    if not username or not password:
        sys.exit(
            "KRA_USERNAME and KRA_PASSWORD must be set.\n"
            "Add them to your .env file or export them in your shell."
        )

    cfg = EtimsConfig(
        pin      = os.environ.get("KRA_PIN",    ""),
        username = username,
        password = password,
        branch   = os.environ.get("KRA_BRANCH", "00"),
    )

    if args.start and args.end:
        start_portal = _yyyymmdd_to_portal(args.start)
        end_portal   = _yyyymmdd_to_portal(args.end)
    else:
        start_portal = end_portal = _yyyymmdd_to_portal(args.date)

    log.info("Searching receipts  %s -> %s", start_portal, end_portal)

    session = _make_session()
    log.info("Logging in to eTIMS portal...")
    login(cfg, session)

    rows = fetch_all_receipts(
        session   = session,
        cfg       = cfg,
        start_dt  = start_portal,
        end_dt    = end_portal,
        cust_pin  = args.cust_pin,
        save_html = args.save_html,
    )

    if not rows:
        print("\nNo receipts found for this date range.")
        sys.exit(0)

    log.info("Parsed %d receipt row(s) total.", len(rows))

    duplicates = find_duplicates(rows)
    print_report(duplicates=duplicates, all_rows=rows, show_all=args.all)


if __name__ == "__main__":
    main()