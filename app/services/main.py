"""
main.py  –  Entry point: read a GRN file → submit to KRA eTIMS portal.

Usage:
    python main.py path/to/grn.pdf
    python main.py path/to/grn.jpg

Credentials are read from environment variables:
    KRA_PIN, KRA_BRANCH, KRA_USERNAME, KRA_PASSWORD
"""

import json
import logging
import os
import sys

from fill_kra import EtimsConfig, grn_to_receipt, run_fill
from read_salesReceipt import read_Grn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python main.py <grn_file.pdf|grn_file.jpg>")
        sys.exit(1)

    grn_file = sys.argv[1]

    # ── 1. Credentials from env ───────────────────────────────────────────────
    cfg = EtimsConfig(
        pin      = "P051621945B",#os.environ.get("KRA_PIN",      ""),
        username = "P051621945B",#os.environ.get("KRA_USERNAME",  ""),
        password = "Nairobi@2025"#os.environ.get("KRA_PASSWORD",  ""),
    )

    missing = [k for k, v in {
        "KRA_PIN": cfg.pin, "KRA_USERNAME": cfg.username, "KRA_PASSWORD": cfg.password
    }.items() if not v]

    if missing:
        log.error("Missing environment variable(s): %s", ", ".join(missing))
        log.error("Set them before running:  export KRA_PIN=Pxxxxxxxxx  ...")
        sys.exit(1)

    # ── 2. Parse GRN ─────────────────────────────────────────────────────────
    log.info("📂 Reading GRN: %s", grn_file)
    grn = read_Grn(grn_file)

    log.debug("Parsed GRN:\n%s", json.dumps(grn, indent=2, default=str))

    # ── 3. Convert to ReceiptHeader ───────────────────────────────────────────
    try:
        header = grn_to_receipt(grn, cfg)
    except ValueError as exc:
        log.error("❌ Could not build receipt: %s", exc)
        sys.exit(1)

    # ── 3b. Non-fiscal information ────────────────────────────────────────────
    # invoice_no and store_no are rarely in the GRN PDF — prompt if missing.
    print("\n── NON-FISCAL INFORMATION ──")
    print(f"  Order No      : {header.order_no        or '(from GRN)'}")
    print(f"  Delivery Note : {header.delivery_note_no or '(from GRN)'}")
    print(f"  GRN No        : {header.grn_no           or '(from GRN)'}")

    if not header.invoice_no:
        header.invoice_no = input("  Invoice No    : ").strip()

    if not header.store_no:
        header.store_no = input("  Store No      : ").strip()

    print(f"\n  Remark → {header.remark}\n")

    log.info("✅ %d item(s) ready.  Customer: %s", len(header.items), header.cust_nm)
    log.info("   Totals → supply: %.2f  tax: %.2f  grand: %.2f",
             header.tot_sply_amt, header.tot_tax_amt, header.sum_tot_amt)

    # ── 4. Submit to KRA ──────────────────────────────────────────────────────
    results = run_fill(cfg, header)

    # ── 5. Summary ────────────────────────────────────────────────────────────
    ok  = [r for r in results if r["status"] == "ok"]
    err = [r for r in results if r["status"] == "error"]

    print("\n── SUBMISSION RESULTS ──")
    print(json.dumps(results, indent=2, default=str))

    print(f"\n{'✅' if not err else '⚠️ '} Receipt submitted {'successfully' if not err else 'with errors'}.")
    if err:
        print("Failed receipt:")
        for r in err:
            print(f"  • {r.get('error', 'unknown error')}")
        sys.exit(2)   # non-zero exit so CI/scripts can detect partial failure


if __name__ == "__main__":
    main()