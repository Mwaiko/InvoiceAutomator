"""
app/services/read_image_content.py

Extracts a structured GRN dict from an image file using the
NVIDIA Nemotron-OCR API instead of the local PaddleOCR library.

Supports Cleanshelf / Naivas / Quickmart image GRNs.

Environment variable required:
    nvapi – NVIDIA API key

Output dict shape (identical to read_pdf.py):
{
    "receipt_voucher_no":  str | None,   # GRN No
    "lpo_number":          str | None,
    "delivery_invoice_no": str | None,   # Inv. No
    "receipt_date":        str | None,
    "store": {
        "company_name": str | None,
        "store_name":   str | None,
        "address":      str | None,
        "location":     str | None,
    },
    "supplier": {
        "company_name": str | None,
        "email":        str | None,
    },
    "items": [
        {
            "no":           int,
            "item_code":    str,
            "description":  str,
            "uom":          str,
            "qty_received": float,
            "unit_price":   float,
            "net_amount":   float,
        },
        ...
    ],
    "sub_total":   float | None,
    "vat":         float,
    "order_total": float | None,
    "received_by":  str | None,
    "confirmed_by": str | None,
    "date":         str | None,
}
"""

import base64
import json
import logging
import os
import re
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_NVIDIA_OCR_URL = "https://ai.api.nvidia.com/v1/cv/nvidia/nemotron-ocr-v1"

# ── UOM vocabulary ────────────────────────────────────────────────────────────
_KNOWN_UOMS = {
    "KGS", "KG", "PCS", "PKT", "LTR", "L", "EA",
    "CTN", "BTL", "BAG", "TIN", "SET", "PAC", "BOX",
}

# ── Address stripper ──────────────────────────────────────────────────────────
# Matches " P.O. BOX …", " P O BOX …", " PO BOX …", " P.O BOX …" and variants,
# plus plain " BOX …" — everything from that point to end of string is stripped.
_ADDRESS_RE = re.compile(
    r"\s+P\.?\s*O\.?\s*BOX\b.*$|\s+BOX\s+\d.*$",
    re.IGNORECASE,
)


def _strip_address(name: str) -> str:
    """Remove trailing P.O. BOX / address fragments from a company name."""
    if not name:
        return name
    return _ADDRESS_RE.sub("", name).strip()



def extract_grn_from_image(image_path: str) -> dict:
    """
    Run OCR on *image_path* via the NVIDIA API and return a GRN dict.
    Drop-in replacement for the old PaddleOCR-based function.
    """
    raw_detections = _call_nvidia_ocr(image_path)
    rows           = _detections_to_rows(raw_detections)
    tokens         = _rows_to_tokens(rows)
    full_text      = " ".join(tokens)

    logger.debug("OCR full_text:\n%s", "\n".join(" | ".join(r) for r in rows))

    return _parse(tokens, full_text, rows)


# ═════════════════════════════════════════════════════════════════════════════
#  NVIDIA OCR helpers
# ═════════════════════════════════════════════════════════════════════════════

def _call_nvidia_ocr(image_path: str) -> list[dict]:
    """
    POST the image to the NVIDIA Nemotron-OCR endpoint.
    Returns the raw list of text-detection dicts.
    """
    api_key = os.environ.get("nvapi") or os.environ.get("NVAPI_KEY")
    if not api_key:
        raise EnvironmentError(
            "NVIDIA OCR API key not found. "
            "Set the 'nvapi' (or 'NVAPI_KEY') environment variable."
        )

    suffix = Path(image_path).suffix.lower()
    mime   = "image/jpeg" if suffix in (".jpg", ".jpeg") else f"image/{suffix.lstrip('.')}"

    with open(image_path, "rb") as fh:
        image_b64 = base64.b64encode(fh.read()).decode()

    if len(image_b64) > 180_000:
        raise ValueError(
            f"Image '{image_path}' is too large for the inline API "
            "(> ~135 KB encoded). Use the NVIDIA assets API for large files."
        )

    payload = {
        "input": [{"type": "image_url", "url": f"data:{mime};base64,{image_b64}"}]
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept":        "application/json",
    }

    response = requests.post(_NVIDIA_OCR_URL, headers=headers, json=payload, timeout=30)
    response.raise_for_status()

    data = response.json()
    try:
        return data["data"][0]["text_detections"]
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Unexpected NVIDIA OCR response shape: {exc}") from exc


def _detections_to_rows(
    detections: list[dict],
    y_threshold: float = 0.008,
) -> list[list[str]]:
    """
    Convert raw bounding-box detections into ordered rows of text tokens.

    Each detection item has the shape:
        {
          "text_prediction": {"text": "...", "confidence": 0.91},
          "bounding_box":    {"points": [{"x": ..., "y": ...}, ...]},
        }
    """
    items: list[dict] = []
    for det in detections:
        try:
            text   = det["text_prediction"]["text"].strip()
            points = det["bounding_box"]["points"]
            if not text:
                continue

            ys       = [p["y"] for p in points]
            center_y = (min(ys) + max(ys)) / 2
            min_x    = min(p["x"] for p in points)

            items.append({"text": text, "center_y": center_y, "min_x": min_x})
        except KeyError:
            continue

    # Sort top-to-bottom
    items.sort(key=lambda d: d["center_y"])

    # Group into rows by vertical proximity
    rows:        list[list[dict]] = []
    current_row: list[dict]       = []

    for item in items:
        if not current_row:
            current_row.append(item)
        else:
            avg_y = sum(i["center_y"] for i in current_row) / len(current_row)
            if abs(item["center_y"] - avg_y) <= y_threshold:
                current_row.append(item)
            else:
                rows.append(current_row)
                current_row = [item]
    if current_row:
        rows.append(current_row)

    # Sort each row left-to-right and return only the text strings
    return [
        [i["text"] for i in sorted(row, key=lambda d: d["min_x"])]
        for row in rows
    ]


def _rows_to_tokens(rows: list[list[str]]) -> list[str]:
    """Flatten all rows into a single list of tokens."""
    return [tok for row in rows for tok in row]


# ═════════════════════════════════════════════════════════════════════════════
#  GRN parser
# ═════════════════════════════════════════════════════════════════════════════

def _find(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse(tokens: list[str], full_text: str, rows: list[list[str]]) -> dict:
    grn: dict = {}

    # ── 1. Header fields ──────────────────────────────────────────────────────
    # receipt_voucher_no
    # Cleanshelf: "G.R.N. No. 57662" / "GRN No 57662"
    # Naivas:     "Receipt Voucher No: IPR-000015453"
    grn["receipt_voucher_no"] = (
        _find(full_text, r"Receipt\s*Voucher\s*No[:\s]+([A-Z0-9\-]+)")
        or _find(full_text, r"G\.?R\.?N\.?\s*No\.?\s*[:\s]*(\d+)")
        or _find(full_text, r"GRN\s*No\.?\s*[:\s]*(\d+)")
    )

    # lpo_number
    # Cleanshelf: "LPO No. 72233"
    # Naivas:     "LPO number: P042017519"  (alphanumeric, keyword is "number")
    grn["lpo_number"] = (
        _find(full_text, r"LPO\s*number[:\s]+([\w]+)")
        or _find(full_text, r"LPO\s*No\.?\s*[:\s]*(\d+)")
        or _find(full_text, r"LPO\s*[:\s]+([A-Z0-9]+)")
    )

    # delivery_invoice_no
    # Cleanshelf: "Inv. No. 2166"
    # Naivas:     "Delivery note/Invoice No: #Q/2093"  — value may start with #
    grn["delivery_invoice_no"] = (
        _find(full_text, r"Delivery\s*note/Invoice\s*No[:\s]+([\w#/\-]+)")
        or _find(full_text, r"I[ne][vV]\.?\s*No\.?\s*[:\s]*(\d+)")
        or _find(full_text, r"Invoice\s*No\.?\s*[:\s]*([A-Z0-9]+)")
    )

    # vendor_id — "Vendor ID: Q/498"
    grn["vendor_id"] = _find(full_text, r"Vendor\s*ID[:\s]+([A-Z0-9/]+)")

    # receipt_date
    # Cleanshelf: "GRN Date 27 Mar 2026"
    # Naivas:     "Receipt Date: 5 Apr 2026"
    grn["receipt_date"] = (
        _find(full_text, r"Receipt\s*Date[:\s]+(\d{1,2}\s+\w+\s+\d{4})")
        or _find(full_text, r"GRN\s*Date\s+(\d{1,2}\s+\w+\s+\d{4})")
        or _find(full_text, r"Date[:\s]+(\d{1,2}\s+\w+\s+\d{4})")
    )

    # ── 2. Store / company ────────────────────────────────────────────────────
    # company_name: first word-bounded match of known chain names
    # Use a tight lookahead so we don't swallow the rest of the page.
    # Stops at whitespace followed by any known next-field keyword.
    _STOP = r"(?=\s+(?:Store|Address|City|Email|Location|Supplier|LPO|No\b|Company|Vendor|Receipt))"
    company_match = re.search(
        r"Company\s+Name\s+((?:NAIVAS|CLEANSHELF|QUICKMART)\s+\w+(?:\s+\w+){0,4})" + _STOP,
        full_text, re.IGNORECASE,
    )
    if not company_match:
        # Fallback: grab first occurrence of the chain brand
        company_match = re.search(
            r"((?:NAIVAS|CLEANSHELF|QUICKMART)\s+(?:LTD|LIMITED|SUPERMARKETS?)\b[^,\n]{0,30})",
            full_text, re.IGNORECASE,
        )
    company_name = (
        _strip_address(company_match.group(1).strip())
        if company_match else None
    )

    # store_name: first non-whitespace word(s) after "Store Name"
    # Use \S+ to grab just the value, not the entire remaining page.
    store_name = (
        _find(full_text, r"Store\s*Name\s+(\S+(?:\s+\S+){0,2})" + _STOP)
        or _find(full_text, r"Store\s*Name\s+(\S+)")
    )

    location = _find(
        full_text,
        r"\b(NAKURU|NAIROBI|MOMBASA|KISUMU|ELDORET|THIKA|RUIRU|KAREN|WESTLANDS|MLOLONGO"
        r"|NAIVASHA|KUBWA|NDOGO)\b",
    )

    grn["store"] = {
        "company_name": company_name,
        "store_name":   store_name or location,
        "address":      _find(full_text, r"(P\.O\.?\s*BOX\s*[\d\-]+[^\s,]*)"),
        "location":     location,
    }

    # ── 3. Supplier ───────────────────────────────────────────────────────────
    grn["supplier"] = {
        "company_name": (
            _find(full_text, r"(QUALITY\s+OUTSOURCE\s+SOLUTION(?:\s+LIMITED)?)")
            or _find(full_text, r"Supplier\s+([A-Z][A-Za-z ]+?)" + _STOP)
        ),
        "email": _find(full_text, r"(\S+@\S+\.\S+)"),
    }

    # ── 4. Line items ─────────────────────────────────────────────────────────
    # Supports both:
    #   Cleanshelf → 6-digit item codes  (e.g. 290719)
    #   Naivas     → 13-digit barcodes   (e.g. 2035031000000)
    _ITEM_CODE_RE = re.compile(r"^\d{6}$|^\d{13}$")

    items: list[dict] = []
    i = 0

    while i < len(tokens):
        tok = tokens[i]

        if not _ITEM_CODE_RE.match(tok):
            i += 1
            continue

        item_code    = tok
        description: list[str] = []
        qty          = None
        uom          = None
        unit_price   = None
        net_amount   = None

        j = i + 1
        while j < len(tokens):
            t = tokens[j]

            # Stop at the next item code (same format as anchor)
            if _ITEM_CODE_RE.match(t):
                break

            # Stop at totals / footer keywords
            if re.search(
                r"^(Sub\s*total|Order\s*total|Net\s*A[Mm]|VAT|Total\s*A|"
                r"With\s*Hold|Prepared|Authorised|Approved|Checked|\*+End|Printed|"
                r"RECEIVED|CONFIRMED)",
                t, re.IGNORECASE,
            ):
                break

            t_upper = t.upper().rstrip("S")   # "KGS" → "KG"

            # UOM token
            if t.upper() in _KNOWN_UOMS or t_upper in _KNOWN_UOMS:
                uom = t.upper()
                j += 1
                continue

            # Numeric token — strip commas / stray punctuation before parsing
            clean = re.sub(r"[^\d.]", "", t.replace(",", "").replace(":", ".").rstrip("."))
            if re.fullmatch(r"\d+\.?\d*", clean) and clean:
                val = float(clean)
                if qty is None and val < 10_000:
                    qty = val
                elif unit_price is None:
                    unit_price = val
                elif net_amount is None:
                    net_amount = val
                    j += 1
                    break
                j += 1
                continue

            # Skip pure punctuation / noise tokens
            if re.fullmatch(r"[.,:\-|%]+", t) or t in ("1", "0"):
                j += 1
                continue

            # Everything else is part of the description
            description.append(t)
            j += 1

        computed_net = round((qty or 1.0) * (unit_price or 0.0), 2)
        if net_amount is None:
            net_amount = computed_net

        items.append({
            "no":           len(items) + 1,
            "item_code":    item_code,
            "description":  " ".join(description).strip(),
            "uom":          uom or "PCS",
            "qty_received": qty or 1.0,
            "unit_price":   unit_price or 0.0,
            "net_amount":   net_amount,
        })
        i = j

    grn["items"] = items

    # ── 5. Totals ─────────────────────────────────────────────────────────────
    def _money(pattern: str) -> float | None:
        s = _find(full_text, pattern)
        return float(s.replace(",", "")) if s else None

    grn["sub_total"] = (
        _money(r"Sub\s*total\s*([\d,]+\.\d{2})")
        or _money(r"Net\s*A[Mm][Oo][Uu][Nn][Tt]\s*[:\|]?\s*([\d,]+\.\d{2})")
    )
    grn["vat"] = (
        _money(r"VAT\s*([\d,]+\.\d{2})")
        or _money(r"VAT\s*A[Mm][Oo][Uu][Nn][Tt]\s*[:\|]?\s*([\d,]+\.\d{2})")
        or 0.0
    )
    grn["order_total"] = (
        _money(r"Order\s*total\s*([\d,]+\.\d{2})")
        or _money(r"Total\s*A[Rr]?[Oo][Uu][Nn][Tt]\s*[:\|]?\s*([\d,]+\.\d{2})")
        or _money(r"Total\s*Net\s*Amount\s*TO\s*Pay\s*[:\|]?\s*([\d,]+\.\d{2})")
    )

    # ── 6. Signatories ────────────────────────────────────────────────────────
    # Naivas:     "RECEIVED BY: DANIEL MOMANYI OIGO"
    # Cleanshelf: "Prepared By <name>"
    grn["received_by"] = (
        _find(full_text, r"RECEIVED\s+BY[:\s]+([A-Za-z ]+?)(?=\s+(?:CONFIRMED|SIGN|$))")
        or _find(full_text, r"Prepared\s+By\s+([A-Za-z]+)")
    )
    grn["confirmed_by"] = (
        _find(full_text, r"CONFIRMED\s+BY[:\s]+([A-Za-z ]+?)(?=\s+(?:SIGN|DATE|Page|$))")
        or _find(full_text, r"Authoris[e]?d\s+By\s+([A-Za-z ]+?)(?=\s+(?:Approved|Page|$))")
    )
    grn["date"] = _find(full_text, r"Date[:\s]+(\d{1,2}\s+\w+,?\s+\d{4})")

    return grn


# ═════════════════════════════════════════════════════════════════════════════
#  Standalone test
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.DEBUG)
    path   = sys.argv[1] if len(sys.argv) > 1 else "test.jpeg"
    result = extract_grn_from_image(path)
    print(json.dumps(result, indent=2))