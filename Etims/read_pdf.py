import pdfplumber
import re
import json


def extract_grn(pdf_path: str) -> dict:
    """Extract Goods Received Note content from a PDF into a structured dictionary."""

    grn = {}

    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        text = page.extract_text()
        tables = page.extract_tables()

    # ── Header fields ────────────────────────────────────────────────────────
    header_patterns = {
        "lpo_number":           r"LPO number[:\s]+([A-Z0-9]+)",
        "delivery_invoice_no":  r"Delivery note/Invoice No[:\s]+([A-Z0-9]+)",
        "vendor_id":            r"Vendor ID[:\s]+([A-Z0-9/]+)",
        "receipt_voucher_no":   r"Receipt Voucher No[:\s]+([A-Z0-9\-]+)",
        "receipt_date":         r"Receipt Date[:\s]+(\d{1,2} \w+ \d{4}|\d{1,2} \w{3} \d{4})",
    }

    for key, pattern in header_patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        grn[key] = match.group(1).strip() if match else None

    # ── Store / company details ──────────────────────────────────────────────
    grn["store"] = {
        "company_name": _find(text, r"Company Name\s+(Naivas[^\n]+?)(?:\s{2,}|Company Name|\n)"),
        "store_name":   _find(text, r"Store Name\s+(.+?)\n"),
        "address":      _find(text, r"Address\s+(NAIVAS[^\n]+?)(?:\s{2,}|Address|\n)"),
        "location":     _find(text, r"Location\s+(.+?)(?:\s{2,}|Email|\n)"),
    }

    # ── Supplier details ─────────────────────────────────────────────────────
    grn["supplier"] = {
        "company_name": _find(text, r"(QUALITY OUTSOURCE SOLUTION[^\n]*)"),
        "email":        _find(text, r"Email\s+(\S+@\S+)"),
    }

    # ── Line items ───────────────────────────────────────────────────────────
    items = []
    # Pattern: <no> <item_code> <description> <UOM> <qty> <unit_price> <net_amount>
    # UOM can be PCS, KG, LTR, CTN, etc.
    item_pattern = re.compile(
        r"(\d+)\s+(\d{13})\s+(.+?)\s+(PCS|KG|LTR|CTN|PKT|BAG|BTL|TIN|SET|PAC|BOX|EA)\s+([\d.]+)\s+([\d.]+)\s+([\d,]+\.\d{2})",
        re.IGNORECASE
    )

    for match in item_pattern.finditer(text):
        items.append({
            "no":           int(match.group(1)),
            "item_code":    match.group(2),
            "description":  match.group(3).strip(),
            "uom":          match.group(4).upper(),
            "qty_received": float(match.group(5)),
            "unit_price":   float(match.group(6)),
            "net_amount":   float(match.group(7).replace(",", "")),
        })

    grn["items"] = items

    # ── Totals ───────────────────────────────────────────────────────────────
    sub_total   = _find(text, r"Sub total\s+([\d,]+\.\d{2})")
    order_total = _find(text, r"Order total\s+([\d,]+\.\d{2})")

    grn["sub_total"]   = float(sub_total.replace(",", ""))   if sub_total   else None
    grn["vat"]         = 0.0
    grn["order_total"] = float(order_total.replace(",", "")) if order_total else None

    # ── Signatories ──────────────────────────────────────────────────────────
    grn["received_by"]  = _find(text, r"RECEIVED BY[:\s]+([A-Za-z ]+?)(?:\s{2,}|CONFIRMED)")
    grn["confirmed_by"] = _find(text, r"CONFIRMED BY[:\s]+([A-Za-z ]+?)(?:\s{2,}|\n)")
    grn["date"]         = _find(text, r"DATE\s+(\d{1,2} \w+,? \d{4})")

    return grn


def _find(text: str, pattern: str) -> str | None:
    """Return first capturing group or None."""
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else None


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    pdf_path = sys.argv[1] if len(sys.argv) > 1 else "Etims/GRN Report P040167866 2264440285.pdf"
    result = extract_grn(pdf_path)

    print(json.dumps(result, indent=2))