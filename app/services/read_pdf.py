import pdfplumber
import re
import json

# ── UOM vocabulary ────────────────────────────────────────────────────────────
_UOM_SET = {"PCS", "KG", "LTR", "CTN", "PKT", "BAG", "BTL", "TIN", "SET", "PAC", "BOX", "EA"}
_UOM_PATTERN = "|".join(_UOM_SET)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def extract_grn(pdf_path: str) -> dict:
    """Extract Goods Received Note content from a PDF into a structured dictionary.

    Handles both single-line and **multi-line item descriptions** by:
      1. Preferring pdfplumber's structured table data (cells already separated).
      2. Falling back to a regex scan over text that has been pre-normalised to
         collapse wrapped description lines back onto a single row.
    """

    grn = {}

    with pdfplumber.open(pdf_path) as pdf:
        # Collect text and tables from ALL pages so nothing is missed
        full_text = "\n".join(
            page.extract_text() or "" for page in pdf.pages
        )
        all_tables = [
            table
            for page in pdf.pages
            for table in (page.extract_tables() or [])
        ]

    # ── Header fields ────────────────────────────────────────────────────────
    header_patterns = {
        "lpo_number":          r"LPO number[:\s]+([A-Z0-9]+)",
        "delivery_invoice_no": r"Delivery note/Invoice No[:\s]+([A-Z0-9]+)",
        "vendor_id":           r"Vendor ID[:\s]+([A-Z0-9/]+)",
        "receipt_voucher_no":  r"Receipt Voucher No[:\s]+([A-Z0-9\-]+)",
        "receipt_date":        r"Receipt Date[:\s]+(\d{1,2} \w+ \d{4}|\d{1,2} \w{3} \d{4})",
    }

    for key, pattern in header_patterns.items():
        match = re.search(pattern, full_text, re.IGNORECASE)
        grn[key] = match.group(1).strip() if match else None

    # ── Store / company details ──────────────────────────────────────────────
    grn["store"] = {
        "company_name": _find(full_text, r"Company Name\s+(Naivas[^\n]+?)(?:\s{2,}|Company Name|\n)"),
        "store_name":   _find(full_text, r"Store Name\s+(.+?)\n"),
        "address":      _find(full_text, r"Address\s+(NAIVAS[^\n]+?)(?:\s{2,}|Address|\n)"),
        "location":     _find(full_text, r"Location\s+(.+?)(?:\s{2,}|Email|\n)"),
    }

    # ── Supplier details ─────────────────────────────────────────────────────
    grn["supplier"] = {
        "company_name": _find(full_text, r"(QUALITY OUTSOURCE SOLUTION[^\n]*)"),
        "email":        _find(full_text, r"Email\s+(\S+@\S+)"),
    }

    # ── Line items ───────────────────────────────────────────────────────────
    # Strategy 1: structured table rows (most reliable for multi-line cells)
    items = _extract_items_from_tables(all_tables)

    # Strategy 2: regex over normalised text (fallback / cross-check)
    if not items:
        normalised = _normalise_item_lines(full_text)
        items = _extract_items_from_text(normalised)

    grn["items"] = items

    # ── Totals ───────────────────────────────────────────────────────────────
    sub_total   = _find(full_text, r"Sub total\s+([\d,]+\.\d{2})")
    order_total = _find(full_text, r"Order total\s+([\d,]+\.\d{2})")

    grn["sub_total"]   = float(sub_total.replace(",", ""))   if sub_total   else None
    grn["vat"]         = 0.0
    grn["order_total"] = float(order_total.replace(",", "")) if order_total else None

    # ── Signatories ──────────────────────────────────────────────────────────
    grn["received_by"]  = _find(full_text, r"RECEIVED BY[:\s]+([A-Za-z ]+?)(?:\s{2,}|CONFIRMED)")
    grn["confirmed_by"] = _find(full_text, r"CONFIRMED BY[:\s]+([A-Za-z ]+?)(?:\s{2,}|\n)")
    grn["date"]         = _find(full_text, r"DATE\s+(\d{1,2} \w+,? \d{4})")

    return grn


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 1 – table-based extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_items_from_tables(tables: list) -> list:
    """Parse line items from pdfplumber table data.

    pdfplumber splits multi-line cells for us, so description wrapping is
    handled transparently — we just need to clean and join cell text.
    """
    items = []

    for table in tables:
        for row in table:
            if not row:
                continue

            cells = [_clean_cell(c) for c in row]

            # Item rows: first cell is a small integer, second is a 13-digit barcode
            if not (re.match(r"^\d{1,4}$", cells[0]) and re.match(r"^\d{13}$", cells[1])):
                continue

            # Locate the UOM cell
            uom_idx = next(
                (i for i, c in enumerate(cells) if c.upper() in _UOM_SET),
                None,
            )
            if uom_idx is None or uom_idx + 3 >= len(cells):
                continue  # Row doesn't have enough numeric columns

            # Collapse any wrapped description text between barcode and UOM
            description = " ".join(
                c for c in cells[2:uom_idx] if c
            )
            description = re.sub(r"\s+", " ", description).strip()

            try:
                items.append({
                    "no":           int(cells[0]),
                    "item_code":    cells[1],
                    "description":  description,
                    "uom":          cells[uom_idx].upper(),
                    "qty_received": float(cells[uom_idx + 1]),
                    "unit_price":   float(cells[uom_idx + 2]),
                    "net_amount":   float(cells[uom_idx + 3].replace(",", "")),
                })
            except (ValueError, IndexError):
                continue  # Malformed numeric cell; skip silently

    return items


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 2 – regex over normalised plain text
# ─────────────────────────────────────────────────────────────────────────────

# Anchors that mark the START of a new logical line (never a continuation)
_NEW_LINE_RE = re.compile(
    r"^\s*(?:"
    r"\d+\s+\d{13}"           # item row: <no> <barcode>
    r"|Sub total"
    r"|Order total"
    r"|RECEIVED"
    r"|CONFIRMED"
    r"|DATE"
    r")",
    re.IGNORECASE,
)


def _normalise_item_lines(text: str) -> str:
    """Fold wrapped description lines back onto the item row they belong to.

    Any line that does NOT look like the start of a new logical record is
    treated as a continuation of the previous line and appended to it.
    """
    lines = text.splitlines()
    normalised: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            normalised.append("")
            continue

        if _NEW_LINE_RE.match(line) or not normalised:
            normalised.append(stripped)
        else:
            # Continuation — join onto previous non-empty line
            normalised[-1] = normalised[-1] + " " + stripped

    return "\n".join(normalised)


def _extract_items_from_text(text: str) -> list:
    """Regex scan for item rows after the text has been normalised."""
    item_pattern = re.compile(
        r"(\d{1,4})\s+"                       # row number
        r"(\d{13})\s+"                         # barcode
        r"(.+?)\s+"                            # description (lazy, single logical line)
        r"(" + _UOM_PATTERN + r")\s+"          # UOM
        r"([\d.]+)\s+"                         # qty
        r"([\d.]+)\s+"                         # unit price
        r"([\d,]+\.\d{2})",                    # net amount
        re.IGNORECASE,
    )

    items = []
    for match in item_pattern.finditer(text):
        items.append({
            "no":           int(match.group(1)),
            "item_code":    match.group(2),
            "description":  re.sub(r"\s+", " ", match.group(3)).strip(),
            "uom":          match.group(4).upper(),
            "qty_received": float(match.group(5)),
            "unit_price":   float(match.group(6)),
            "net_amount":   float(match.group(7).replace(",", "")),
        })

    return items


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find(text: str, pattern: str) -> str | None:
    """Return first capturing group or None."""
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _clean_cell(value) -> str:
    """Normalise a pdfplumber table cell to a plain string."""
    if value is None:
        return ""
    # Cells can contain embedded newlines when a description wraps inside a cell
    return re.sub(r"\s+", " ", str(value)).strip()


# ── CLI runner ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    pdf_path = sys.argv[1] if len(sys.argv) > 1 else "Etims/GRN Report P040167866 2264440285.pdf"
    result = extract_grn(pdf_path)
    print(json.dumps(result, indent=2))