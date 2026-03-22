from paddleocr import PaddleOCR
import re
import json

# ── OCR singleton (initialised once) ─────────────────────────────────────────
_ocr = None
def _get_ocr() -> PaddleOCR:
    global _ocr
    if _ocr is None:
        _ocr = PaddleOCR(
            use_angle_cls=False,
            lang='en',
            use_gpu=False,  # Assuming CPU based on your lscpu output
            enable_mkldnn=False, # MKLDNN can also trigger SIGILL on some AMD CPUs
            # This is the critical line:
            delete_pass=["self_attention_fuse_pass"], 
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    return _ocr

def extract_grn_from_image(image_path: str) -> dict:
    """Run OCR on *image_path* and return a GRN dict."""

    ocr = _get_ocr()
    # result is a list: [ [ [ [box], (text, score) ], ... ] ]
    result = ocr.ocr(image_path, cls=False)

    tokens: list[str] = []
    
    # PaddleOCR returns a list for each page/image. 
    # Since we process one image, we look at result[0].
    if result and result[0]:
        for line in result[0]:
            # line[1][0] is the actual text string
            text_value = line[1][0]
            if text_value.strip():
                tokens.append(text_value.strip())

    full_text = " ".join(tokens)   # flat string for regex searches
    return _parse_tokens(tokens, full_text)

# ── Internal parser ───────────────────────────────────────────────────────────
def _find(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _parse_tokens(tokens: list[str], full_text: str) -> dict:
    grn: dict = {}

    # ── Header fields ─────────────────────────────────────────────────────────
    grn["lpo_number"]          = _find(full_text, r"LPO\s*No\.?\s*[:\s]*(\d+)")
    grn["delivery_invoice_no"] = _find(full_text, r"Inv\.?\s*No\.?\s*[:\s]*(\d+)")
    grn["vendor_id"]           = _find(full_text, r"Supplier\s+(\w+)")
    grn["receipt_voucher_no"]  = _find(full_text, r"G\.?R\.?N\.?\s*No\.?\s*[:\s]*(\d+)")
    grn["receipt_date"]        = _find(full_text, r"GRN\s*Date\s+(\d{1,2}\s+\w+\s+\d{4})")

    # ── Store / company ────────────────────────────────────────────────────────
    # The company name is usually near the top before any keyword
    company_match = re.search(
        r"(NAIVAS[^,\n]+|CLEANSHELF[^,\n]+|QUICKMART[^,\n]+)", full_text, re.IGNORECASE
    )
    company_name = company_match.group(1).strip() if company_match else None

    # Try to get a store/branch name from "NAKURU", "NAIROBI" style city tokens
    # or from a pattern like "Store Name <value>"
    store_name = _find(full_text, r"Store\s*Name\s+(.+?)(?:\s{2,}|Store|$)")
    location   = _find(full_text, r"(NAKURU|NAIROBI|MOMBASA|KISUMU|ELDORET|THIKA)")

    grn["store"] = {
        "company_name": company_name,
        "store_name":   store_name or location,
        "address":      _find(full_text, r"(P\.O\.BOX\s*[\d\-]+[^\s,]*)"),
        "location":     location,
    }

    # ── Supplier ───────────────────────────────────────────────────────────────
    grn["supplier"] = {
        "company_name": _find(full_text, r"(QUALITY\s*OUTSOURCE\s*SOLUTION[^\s,]*)"),
        "email":        _find(full_text, r"(\S+@\S+)"),
    }

    # ── Line items ─────────────────────────────────────────────────────────────
    # Strategy: locate numeric item codes (6-digit) then gather following tokens
    items: list[dict] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # 6-digit item code
        if re.fullmatch(r"\d{6}", tok):
            item_code   = tok
            description = []
            qty         = None
            uom         = None
            unit_price  = None
            net_amount  = None

            j = i + 1
            while j < len(tokens):
                t = tokens[j]

                # UOM (unit of measure)
                if t.upper() in ("KGS", "PCS", "PKT", "LTR", "EA", "CTN"):
                    uom = t.upper()
                    j += 1
                    continue

                # Numbers that look like qty / price
                if re.fullmatch(r"[\d,]+\.?\d*", t.replace(",", "")):
                    val = float(t.replace(",", ""))
                    if qty is None:
                        qty = val
                    elif unit_price is None:
                        unit_price = val
                    elif net_amount is None:
                        net_amount = val
                        j += 1
                        break   # we have all numeric fields
                    j += 1
                    continue

                # Stop when we hit the next item code or known keywords
                if re.fullmatch(r"\d{6}", t) or t in (
                    "Net Amount", "VAT Amount", "Total Amount",
                    "Prepared", "Authorised", "Approved", "*****End"
                ):
                    break

                # Skip punctuation tokens
                if t in (".", ",", ":", "1"):
                    j += 1
                    continue

                # Otherwise it's part of the description
                description.append(t)
                j += 1

            items.append({
                "no":           len(items) + 1,
                "item_code":    item_code,
                "description":  " ".join(description).strip(),
                "uom":          uom or "PCS",
                "qty_received": qty or 1.0,
                "unit_price":   unit_price or 0.0,
                "net_amount":   net_amount or 0.0,
            })
            i = j
        else:
            i += 1

    grn["items"] = items

    # ── Totals ─────────────────────────────────────────────────────────────────
    net_str   = _find(full_text, r"Net Amount\s*[:\s]*([\d,]+\.\d{2})")
    total_str = _find(full_text, r"Total Amount\s*[:\s]*([\d,]+\.\d{2})")

    grn["sub_total"]   = float(net_str.replace(",", ""))   if net_str   else None
    grn["vat"]         = 0.0
    grn["order_total"] = float(total_str.replace(",", "")) if total_str else None

    # ── Signatories ────────────────────────────────────────────────────────────
    grn["received_by"]  = _find(full_text, r"Prepared\s+By\s+([A-Za-z]+)")
    grn["confirmed_by"] = _find(full_text, r"Authorised\s+By\s+([A-Za-z ]+?)(?:\s{2,}|$)")
    grn["date"]         = _find(full_text, r"Date\s+(\d{1,2}\s+\w+,?\s+\d{4})")

    return grn


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "test.jpeg"
    result = extract_grn_from_image(path)
    print(json.dumps(result, indent=2))