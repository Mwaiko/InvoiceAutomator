import csv
import uuid
import logging
import argparse
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, astuple
from typing import Optional

# ── LOGGING ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
DB_URL = "postgresql://grn_db_user:ykNj3fBzPTAuG2aXiBNukuVQEZxuLkwP@dpg-d6vrl45m5p6s73ah11ng-a.virginia-postgres.render.com/grn_db?sslmode=require"
BUSINESS_NAME = "Naivas Limited"

# ── DATA MODEL ─────────────────────────────────────────────────────────────────

@dataclass
class InvoiceRecord:
    id:               str
    grn_number:       str
    store_number:     str
    lpo_number:       str
    business_name:    str
    branch_name:      str
    status:           str
    payment_status:   str
    invoice_amount:   float
    amount_paid:      float
    retry_count:      int
    created_at:       datetime
    updated_at:       datetime
    cust_invoice_no:  Optional[str]   
    invoice_no:       Optional[str]   

    def as_tuple(self) -> tuple:
        return astuple(self)


# ── CSV PARSING ────────────────────────────────────────────────────────────────

def _parse_date(raw: str) -> datetime:
    """
    Accept both M/D/YYYY (the actual format in this CSV)
    and YYYY-MM-DD as a fallback.
    """
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date format: {raw!r}")

def _parse_amount(raw: str) -> float:
    cleaned = raw.replace(",", "").replace(" ", "").strip()
    return float(cleaned)

def _parse_payment_status(row: list[str]) -> str:
    """
    Payment status lives in different columns depending on the month:
      - Sep-Dec 2025 : col 9 is empty → unpaid
      - Jan 2026     : col 9 = "PAID"
      - Feb+ 2026    : col 9 = sub-sequence number, col 10 = "Paid" / "PAID"
    Strategy: check col 10 first; if it looks like a status word use it,
    otherwise fall back to col 9.
    """
    col9  = row[9].strip().lower()  if len(row) > 9  else ""
    col10 = row[10].strip().lower() if len(row) > 10 else ""

    if "paid" in col10:
        return "paid"
    if "paid" in col9:
        return "paid"
    return "pending"

def _is_data_row(row: list[str]) -> bool:
    """
    A real transaction row starts with a date in M/D/YYYY format and has
    at least 5 columns.
    """
    if not row or len(row) < 5:
        return False
    cell = row[0].strip()
    parts = cell.split("/")
    if len(parts) != 3:
        return False
    try:
        int(parts[0]); int(parts[1]); int(parts[2])
        return True
    except ValueError:
        return False

def _col(row: list[str], idx: int, default: str = "") -> str:
    """Safe column accessor."""
    return row[idx].strip() if len(row) > idx else default

def parse_csv(file_path: Path) -> tuple[list[InvoiceRecord], list[dict]]:
    """
    Parse the QOS statement CSV.

    Column mapping (0-based):
        0  → date          (M/D/YYYY)
        1  → GRN / PO no.
        2  → NVS lpo no.
        3  → sequence no.  (ignored)
        4  → amount
        5  → branch name
        6  → (empty)
        7  → internal ref  (ignored)
        8  → store number
        9  → PAID (Jan rows) or sub-seq (Feb+ rows)
        10 → payment status (Feb+ rows)

    Returns (valid_records, failed_rows).
    """
    records:  list[InvoiceRecord] = []
    failures: list[dict]          = []

    with open(file_path, mode="r", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.reader(f)
        for line_num, row in enumerate(reader, start=1):
            if not _is_data_row(row):
                continue

            # inv_no is now derived inside the try block as cust_inv_no

            try:
                invoice_amount = _parse_amount(_col(row, 4, "0"))
                payment_status = _parse_payment_status(row)

                # If the invoice is paid, amount_paid must equal invoice_amount.
                # For pending invoices, amount_paid stays 0.
                amount_paid = invoice_amount if payment_status == "paid" else 0.0

                cust_inv_no = _col(row, 3) or None
                invoice_no  = _col(row, 7) or None

                record = InvoiceRecord(
                    id              = str(uuid.uuid4()),
                    grn_number      = _col(row, 2),
                    store_number    = _col(row, 8),
                    lpo_number      = _col(row, 1),   
                    business_name   = BUSINESS_NAME,
                    branch_name     = _col(row, 5).upper(),
                    status          = "submitted",
                    payment_status  = payment_status,
                    invoice_amount  = invoice_amount,
                    amount_paid     = amount_paid,
                    retry_count     = 0,
                    created_at      = _parse_date(row[0]),
                    updated_at      = datetime.now(),
                    cust_invoice_no = cust_inv_no,
                    invoice_no      = invoice_no,
                )
                records.append(record)

            except (ValueError, IndexError) as e:
                failures.append({
                    "line": line_num,
                    "error": str(e),
                    "preview": row[:6],
                })

    # Deduplicate by grn_number — last row in the CSV wins.
    seen: dict[str, InvoiceRecord] = {}
    for r in records:
        if r.grn_number in seen:
            log.warning(
                "Duplicate grn_number %r in CSV (lines kept: last occurrence). "
                "lpo_number %r will overwrite lpo_number %r.",
                r.grn_number, r.lpo_number, seen[r.grn_number].lpo_number,
            )
        seen[r.grn_number] = r
    records = list(seen.values())

    # If an lpo_number appears more than once, make each occurrence unique
    # by appending the row index only for the duplicates.
    lpo_counts: dict[str, int] = {}
    for r in records:
        key = r.lpo_number or ""
        lpo_counts[key] = lpo_counts.get(key, 0) + 1

    lpo_seen: dict[str, int] = {}
    for i, r in enumerate(records, start=1):
        key = r.lpo_number or ""
        if lpo_counts[key] > 1:
            occurrence = lpo_seen.get(key, 0) + 1
            lpo_seen[key] = occurrence
            r.lpo_number = f"{r.lpo_number}-{i}" if r.lpo_number else str(i)
            log.warning(
                "Duplicate lpo_number %r (occurrence %d) — renamed to %r.",
                key, occurrence, r.lpo_number,
            )

    return records, failures


# ── DATABASE SYNC ──────────────────────────────────────────────────────────────

INSERT_QUERY = """
    INSERT INTO etims_invoices (
        id, grn_number, store_number, lpo_number,
        business_name, branch_name, status, payment_status,
        invoice_amount, amount_paid, retry_count, created_at, updated_at,
        cust_invoice_no, invoice_no
    ) VALUES %s
    ON CONFLICT (lpo_number) DO UPDATE SET
        grn_number      = EXCLUDED.grn_number,
        status          = EXCLUDED.status,
        payment_status  = EXCLUDED.payment_status,
        invoice_amount  = EXCLUDED.invoice_amount,
        amount_paid     = EXCLUDED.amount_paid,
        updated_at      = EXCLUDED.updated_at,
        cust_invoice_no = EXCLUDED.cust_invoice_no,
        invoice_no      = EXCLUDED.invoice_no;
"""

def sync_to_postgres(records: list[InvoiceRecord], db_url: str) -> None:
    if not records:
        log.warning("No records to sync.")
        return
    if not db_url:
        raise ValueError("DB_URL is not set.")

    tuples = [r.as_tuple() for r in records]
    conn: Optional[psycopg2.extensions.connection] = None
    try:
        conn = psycopg2.connect(db_url)
        with conn.cursor() as cur:
            execute_values(cur, INSERT_QUERY, tuples)
        conn.commit()
        log.info("✅ Synced %d invoices to the database.", len(records))
    except psycopg2.DatabaseError as e:
        if conn:
            conn.rollback()
        log.error("❌ Database error: %s", e)
        raise
    finally:
        if conn:
            conn.close()


# ── ORCHESTRATOR ───────────────────────────────────────────────────────────────

def process_statement(file_path: str, dry_run: bool = False) -> None:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {path}")

    log.info("Reading: %s", path.resolve())
    records, failures = parse_csv(path)

    log.info("Parsed %d valid rows.", len(records))

    if failures:
        log.warning("%d row(s) could not be parsed:", len(failures))
        for f in failures:
            log.warning("  Line %d — %s | Preview: %s", f["line"], f["error"], f["preview"])

    if dry_run:
        log.info("Dry-run mode: skipping database write.")
        _print_sample(records)
        return

    sync_to_postgres(records, DB_URL)


def _print_sample(records: list[InvoiceRecord], n: int = 5) -> None:
    log.info("Sample output (first %d records):", min(n, len(records)))
    for r in records[:n]:
        amount_str = f"{r.invoice_amount:>10,.2f}"
        paid_str   = f"{r.amount_paid:>10,.2f}"
        log.info(
            "  [%s] GRN=%-14s CUST_INV=%-18s INV_NO=%-14s Branch=%-28s Store=%-4s Amount=%s  Paid=%s  Status=%s",
            r.created_at.date(), r.grn_number, r.cust_invoice_no, r.invoice_no,
            r.branch_name, r.store_number, amount_str, paid_str, r.payment_status,
        )


# ── ENTRY POINT ────────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse a QOS statement CSV and upsert invoices into PostgreSQL."
    )
    parser.add_argument("file", help="Path to the CSV statement file.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and validate only; do not write to the database.",
    )
    return parser


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()
    process_statement(args.file, dry_run=args.dry_run)