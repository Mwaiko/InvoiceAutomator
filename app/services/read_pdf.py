"""
read_pdf.py

Extracts a structured GRN dict from a PDF file.

Handles:
  - Text-based PDFs         → pdfplumber (table-first, regex fallback)
  - Image-only PDFs         → page rasterisation + NVIDIA OCR
  - Hybrid PDFs             → per-page strategy selection
  - Multi-page PDFs         → full merge across all pages
  - Large page images       → automatic resize/compress to fit OCR API limit

Output dict shape is identical to read_image_content.py.
"""

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

import pdfplumber
from PIL import Image          # Pillow — pip install pillow

logger = logging.getLogger(__name__)

# ── UOM vocabulary ────────────────────────────────────────────────────────────
_UOM_SET     = {"PCS", "KG", "LTR", "CTN", "PKT", "BAG", "BTL", "TIN", "SET", "PAC", "BOX", "EA"}
_UOM_PATTERN = "|".join(_UOM_SET)

# ── NVIDIA OCR size gate ──────────────────────────────────────────────────────
# Base64 overhead ≈ ×1.37; 180 000 chars ≈ 131 KB original file.
# We target 120 KB to leave headroom.
_MAX_IMAGE_BYTES = 120_000   # bytes (before base64 encoding)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def extract_grn(pdf_path: str) -> dict:
    """
    Extract GRN content from a (possibly multi-page, possibly image-based) PDF.

    Strategy per page:
      • If pdfplumber can extract ≥ 30 characters → text mode.
      • Otherwise → rasterise and OCR.

    Results from all pages are merged into a single GRN dict.
    """
    pdf_path = str(pdf_path)

    with pdfplumber.open(pdf_path) as pdf:
        pages_info = [
            {
                "index":  i,
                "text":   page.extract_text() or "",
                "tables": page.extract_tables() or [],
            }
            for i, page in enumerate(pdf.pages)
        ]

    total_pages = len(pages_info)
    logger.info("PDF has %d page(s): %s", total_pages, pdf_path)

    page_results: list[dict] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for info in pages_info:
            page_no = info["index"] + 1          # 1-based for logging / pdftoppm

            if len(info["text"].strip()) >= 30:
                # ── Text page ─────────────────────────────────────────────────
                logger.info("Page %d/%d: text mode", page_no, total_pages)
                result = _extract_from_text(info["text"], info["tables"])
            else:
                # ── Image page ────────────────────────────────────────────────
                logger.info("Page %d/%d: OCR mode", page_no, total_pages)
                img_path = _rasterise_page(pdf_path, page_no, tmpdir, total_pages)
                img_path = _ensure_within_size_limit(img_path)
                result   = _ocr_page(img_path)

            page_results.append(result)

    return _merge_pages(page_results)


# ─────────────────────────────────────────────────────────────────────────────
# Text-mode extraction  (pdfplumber data)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_from_text(full_text: str, tables: list) -> dict:
    """Extract header, items and totals from selectable-text page data."""
    grn: dict = {}

    # ── Header ────────────────────────────────────────────────────────────────
    header_patterns = {
        "lpo_number":          r"LPO number[:\s]+([A-Z0-9]+)",
        "delivery_invoice_no": r"Delivery note/Invoice No[:\s]+([\w#/\-]+)",
        "vendor_id":           r"Vendor ID[:\s]+([A-Z0-9/]+)",
        "receipt_voucher_no":  r"Receipt Voucher No[:\s]+([A-Z0-9\-]+)",
        "receipt_date":        r"Receipt Date[:\s]+(\d{1,2} \w+ \d{4}|\d{1,2} \w{3} \d{4})",
    }
    for key, pattern in header_patterns.items():
        m = re.search(pattern, full_text, re.IGNORECASE)
        grn[key] = m.group(1).strip() if m else None

    # ── Store ─────────────────────────────────────────────────────────────────
    grn["store"] = {
        "company_name": _find(full_text, r"Company Name\s+(Naivas[^\n]+?)(?:\s{2,}|Company Name|\n)"),
        "store_name":   _find(full_text, r"Store Name\s+(.+?)\n"),
        "address":      _find(full_text, r"Address\s+([^\n]+?)(?:\s{2,}|Address|\n)"),
        "location":     _find(full_text, r"Location\s+(.+?)(?:\s{2,}|Email|\n)"),
    }

    # ── Supplier ──────────────────────────────────────────────────────────────
    grn["supplier"] = {
        "company_name": _find(full_text, r"(QUALITY OUTSOURCE SOLUTION[^\n]*)"),
        "city":         _find(full_text, r"City\s+([^\n]+?)(?:\s{2,}|Email|\n)"),
        "email":        _find(full_text, r"Email\s+(\S+@\S+)"),
    }

    # ── Line items ────────────────────────────────────────────────────────────
    items = _items_from_tables(tables)
    if not items:
        items = _items_from_text(_normalise_lines(full_text))
    grn["items"] = items

    # ── Totals ────────────────────────────────────────────────────────────────
    sub   = _find(full_text, r"Sub total\s+([\d,]+\.\d{2})")
    total = _find(full_text, r"Order total\s+([\d,]+\.\d{2})")
    grn["sub_total"]   = float(sub.replace(",", ""))   if sub   else None
    grn["vat"]         = 0.0
    grn["order_total"] = float(total.replace(",", "")) if total else None

    # ── Signatories ───────────────────────────────────────────────────────────
    grn["received_by"]  = _find(full_text, r"RECEIVED BY[:\s]+([A-Za-z ]+?)(?:\s{2,}|CONFIRMED)")
    grn["confirmed_by"] = _find(full_text, r"CONFIRMED BY[:\s]+([A-Za-z ]+?)(?:\s{2,}|\n)")
    grn["date"]         = _find(full_text, r"DATE\s+(\d{1,2} \w+,? \d{4})")

    return grn


def _items_from_tables(tables: list) -> list:
    items = []
    for table in tables:
        for row in table:
            if not row:
                continue
            cells = [_clean_cell(c) for c in row]
            if not (re.match(r"^\d{1,4}$", cells[0]) and re.match(r"^\d{13}$", cells[1])):
                continue
            uom_idx = next((i for i, c in enumerate(cells) if c.upper() in _UOM_SET), None)
            if uom_idx is None or uom_idx + 3 >= len(cells):
                continue
            desc = re.sub(r"\s+", " ", " ".join(c for c in cells[2:uom_idx] if c)).strip()
            try:
                items.append({
                    "no":           int(cells[0]),
                    "item_code":    cells[1],
                    "description":  desc,
                    "uom":          cells[uom_idx].upper(),
                    "qty_received": float(cells[uom_idx + 1]),
                    "unit_price":   float(cells[uom_idx + 2]),
                    "net_amount":   float(cells[uom_idx + 3].replace(",", "")),
                })
            except (ValueError, IndexError):
                continue
    return items


_NEW_LINE_RE = re.compile(
    r"^\s*(?:\d+\s+\d{13}|Sub total|Order total|RECEIVED|CONFIRMED|DATE)",
    re.IGNORECASE,
)

def _normalise_lines(text: str) -> str:
    lines, out = text.splitlines(), []
    for line in lines:
        s = line.strip()
        if not s:
            out.append("")
        elif _NEW_LINE_RE.match(line) or not out:
            out.append(s)
        else:
            out[-1] = out[-1] + " " + s
    return "\n".join(out)


def _items_from_text(text: str) -> list:
    pat = re.compile(
        r"(\d{1,4})\s+(\d{13})\s+(.+?)\s+(" + _UOM_PATTERN + r")\s+([\d.]+)\s+([\d.]+)\s+([\d,]+\.\d{2})",
        re.IGNORECASE,
    )
    return [
        {
            "no":           int(m.group(1)),
            "item_code":    m.group(2),
            "description":  re.sub(r"\s+", " ", m.group(3)).strip(),
            "uom":          m.group(4).upper(),
            "qty_received": float(m.group(5)),
            "unit_price":   float(m.group(6)),
            "net_amount":   float(m.group(7).replace(",", "")),
        }
        for m in pat.finditer(text)
    ]


# ─────────────────────────────────────────────────────────────────────────────
# OCR-mode: rasterise → resize → OCR
# ─────────────────────────────────────────────────────────────────────────────

def _rasterise_page(pdf_path: str, page_no: int, tmpdir: str, total_pages: int) -> str:
    """
    Rasterise a single page via pdftoppm.

    Uses 150 DPI — sufficient for OCR and keeps file sizes manageable.
    Returns the path to the JPEG file produced.
    """
    prefix = f"{tmpdir}/p{page_no:04d}"

    result = subprocess.run(
        [
            "pdftoppm",
            "-jpeg",
            "-r", "150",
            "-f", str(page_no),
            "-l", str(page_no),
            pdf_path, prefix,
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pdftoppm failed (page {page_no}): {result.stderr.decode().strip()}"
        )

    # pdftoppm zero-pads based on total page count
    images = sorted(Path(tmpdir).glob(f"p{page_no:04d}-*.jpg"))
    if not images:
        raise RuntimeError(f"pdftoppm produced no image for page {page_no} of {pdf_path}")

    return str(images[0])


def _ensure_within_size_limit(img_path: str) -> str:
    """
    If the image exceeds _MAX_IMAGE_BYTES, progressively scale it down and
    re-save as JPEG until it fits.  Returns the path of the (possibly new)
    image that is safe to send to the NVIDIA OCR API.
    """
    size = Path(img_path).stat().st_size
    if size <= _MAX_IMAGE_BYTES:
        return img_path

    logger.info(
        "Image %s is %d KB — resizing to fit OCR API limit",
        img_path, size // 1024,
    )

    img        = Image.open(img_path).convert("RGB")
    out_path   = img_path.replace(".jpg", "_resized.jpg")
    quality    = 85
    scale      = 1.0

    while True:
        w = int(img.width  * scale)
        h = int(img.height * scale)
        resized = img.resize((w, h), Image.LANCZOS)
        resized.save(out_path, "JPEG", quality=quality)

        if Path(out_path).stat().st_size <= _MAX_IMAGE_BYTES:
            logger.info("Resized to %dx%d @ q%d → %d KB", w, h, quality,
                        Path(out_path).stat().st_size // 1024)
            return out_path

        # Alternate between reducing quality and reducing scale
        if quality > 60:
            quality -= 10
        else:
            scale  -= 0.1

        if scale < 0.3:
            raise RuntimeError(
                f"Cannot shrink {img_path} below the OCR API size limit "
                "({_MAX_IMAGE_BYTES // 1024} KB). The page may be too complex."
            )


def _ocr_page(img_path: str) -> dict:
    """Run the NVIDIA OCR pipeline on a single page image and return a GRN dict."""
    try:
        from read_image_content import extract_grn_from_image          # standalone
    except ImportError:
        from app.services.read_image_content import extract_grn_from_image

    return extract_grn_from_image(img_path)


# ─────────────────────────────────────────────────────────────────────────────
# Multi-page merge
# ─────────────────────────────────────────────────────────────────────────────

# Fields where the FIRST non-None value wins (header / store / supplier).
_SCALAR_FIELDS = (
    "lpo_number", "delivery_invoice_no", "vendor_id",
    "receipt_voucher_no", "receipt_date",
    "received_by", "confirmed_by", "date",
    "sub_total", "vat", "order_total",
)

_DICT_FIELDS = ("store", "supplier")


def _merge_pages(results: list[dict]) -> dict:
    """
    Merge per-page GRN dicts into one.

    Rules:
      • Scalar header fields  → first non-None value across pages.
      • Nested dict fields    → same first-non-None logic per sub-key.
      • items list            → concatenate; renumber sequentially;
                                deduplicate by item_code.
      • vat                   → sum (usually all zeros, but just in case).
    """
    if not results:
        return {}
    if len(results) == 1:
        return results[0]

    merged: dict = {}

    # ── Scalar fields ─────────────────────────────────────────────────────────
    for field in _SCALAR_FIELDS:
        for r in results:
            val = r.get(field)
            if val is not None:
                merged[field] = val
                break
        else:
            merged[field] = None

    # Special: sum VAT across pages (usually 0 + 0 = 0)
    total_vat = sum(r.get("vat") or 0.0 for r in results)
    merged["vat"] = total_vat

    # ── Nested dict fields ────────────────────────────────────────────────────
    for field in _DICT_FIELDS:
        sub_keys = set()
        for r in results:
            if isinstance(r.get(field), dict):
                sub_keys.update(r[field].keys())

        merged_sub: dict = {}
        for key in sub_keys:
            for r in results:
                val = (r.get(field) or {}).get(key)
                if val is not None:
                    merged_sub[key] = val
                    break
            else:
                merged_sub[key] = None

        merged[field] = merged_sub

    # ── Line items ────────────────────────────────────────────────────────────
    seen_codes: set[str] = set()
    all_items:  list     = []

    for r in results:
        for item in r.get("items") or []:
            code = item.get("item_code", "")
            if code and code in seen_codes:
                logger.debug("Skipping duplicate item_code: %s", code)
                continue
            seen_codes.add(code)
            all_items.append(item)

    # Renumber sequentially
    for idx, item in enumerate(all_items, start=1):
        item["no"] = idx

    merged["items"] = all_items

    logger.info(
        "Merged %d page(s): %d unique items | sub_total=%s | order_total=%s",
        len(results),
        len(all_items),
        merged.get("sub_total"),
        merged.get("order_total"),
    )

    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _clean_cell(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


# ─────────────────────────────────────────────────────────────────────────────
# CLI runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    pdf_path = sys.argv[1] if len(sys.argv) > 1 else "P042017519.pdf"
    result   = extract_grn(pdf_path)
    print(json.dumps(result, indent=2))