"""
app/services/etims_invoice_scraper.py

Parses the two KRA eTIMS portal popups needed to reconstruct an already-
submitted invoice, so it can be resubmitted with a corrected buyer PIN:

  1. POST /app/ebm/trns/popup/popupTrnsSalesInvoice
     -> buyer name, buyer mobile, non-fiscal remark (order/delivery/grn/
        invoice/store numbers), payment type.
     Does NOT carry itemCd / itemClsCd (those inputs are commented out in
     the markup), so it's header-only for our purposes.

  2. POST /app/ebm/trns/popup/popupTrnsSalesReceipt
     -> full authoritative line items: itemClsCd, itemCd, itemNm, unit
        price, qty, taxTyCd. This is the one we need for the item list.

Both parsers are defensive: KRA's HTML is hand-rolled and not guaranteed
stable, so every field extraction raises a clear ScrapeError naming exactly
what was missing rather than silently producing a wrong invoice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup


class ScrapeError(Exception):
    """Raised when an expected field can't be found in a KRA popup response."""


@dataclass
class ScrapedHeader:
    cust_nm:          str
    cust_mbl_no:      str
    cust_branch_nm:   str   # derived: text after " - " in cust_nm, if present
    pmt_ty_cd_display: str  # e.g. "OTHER" — display text, NOT the code KRA expects
    order_no:         str
    delivery_note_no: str
    grn_no:           str
    invoice_no:       str   # the "Invoice No." embedded in the remark (NOT orgInvcNo)
    store_no:         str
    remark_raw:       str
    receipt_no:       str   # KRA's internal receipt/orgInvcNo, e.g. "515"


@dataclass
class ScrapedItem:
    item_cls_cd: str
    item_cd:     str
    item_nm:     str
    prc:         float   # unit price, VAT incl
    qty:         float
    tax_ty_cd:   str


REMARK_RE = re.compile(
    r"Order No\.(?P<order>[^,]*),"
    r"Delivery Note No\.(?P<delivery>[^,]*),"
    r"Grn No\.?\s*(?P<grn>[^,]*),"
    r"Invoice No\.?\s*(?P<invoice>[^,]*),"
    r"Store No\.?\s*(?P<store>.*)",
    re.IGNORECASE,
)

# Added to handle the specific 'Return Order' formatting dynamically
RETURN_REMARK_RE = re.compile(
    r"Return Order number:\s*(?P<order>[^,]*)\s*,\s*"
    r"Return number:\s*(?P<delivery>[^,]*)\s*,\s*"
    r"Return Voucher No:\s*(?P<invoice>.*)",
    re.IGNORECASE,
)

# KRA's own popup HTML sometimes renders the literal text "null" (or "None")
# in a cell instead of leaving it blank, when the underlying field was never
# set on the original invoice. Scraping that text and resubmitting it as a
# literal "null" is worse than submitting nothing — normalize it to "" so it's
# treated the same as genuinely missing data (and so validation can catch it).
_NULLISH = {"null", "none", "n/a", "-", "nil"}


def _clean(value: str) -> str:
    v = (value or "").strip()
    return "" if v.lower() in _NULLISH else v


def _find_input_value(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("input", attrs={"name": name})
    if tag is None:
        raise ScrapeError(f"popupTrnsSalesInvoice: no <input name='{name}'> found")
    return _clean(tag.get("value") or "")


def parse_invoice_popup(html: str, *, receipt_no: str) -> ScrapedHeader:
    """
    Parse the response of POST /app/ebm/trns/popup/popupTrnsSalesInvoice.
    `receipt_no` is the orgInvcNo you requested (passed in, not scraped,
    since it's also embedded in the popup as confirmation).
    """
    soup = BeautifulSoup(html, "html.parser")

    cust_nm     = _find_input_value(soup, "custNm")
    cust_mbl_no = _find_input_value(soup, "custMblNo")
    remark_raw  = _find_input_value(soup, "remark")

    pmt_tag = soup.find(id="pmtTyCd")
    pmt_ty_cd_display = _clean(pmt_tag.get("value") or "") if pmt_tag else ""

    # Check for standard remark, then return remark, then empty remark
    m_standard = REMARK_RE.search(remark_raw)
    m_return = RETURN_REMARK_RE.search(remark_raw)

    order_no, delivery_note_no, grn_no, invoice_no, store_no = "", "", "", "", ""

    if m_standard:
        order_no         = _clean(m_standard.group("order"))
        delivery_note_no = _clean(m_standard.group("delivery"))
        grn_no           = _clean(m_standard.group("grn"))
        invoice_no       = _clean(m_standard.group("invoice"))
        store_no         = _clean(m_standard.group("store"))
    elif m_return:
        order_no         = _clean(m_return.group("order").strip(" ."))
        delivery_note_no = _clean(m_return.group("delivery").strip(" ."))
        invoice_no       = _clean(m_return.group("invoice").strip(" ."))
        # grn_no and store_no remain blank, defaulting securely upstream
    elif not remark_raw.strip():
        # Empty string remark - allow it through and let upstream defaults handle it
        pass
    else:
        raise ScrapeError(
            f"popupTrnsSalesInvoice: remark didn't match expected format: {remark_raw!r}"
        )

    cust_branch_nm = ""
    if " - " in cust_nm:
        cust_branch_nm = cust_nm.split(" - ", 1)[1].strip()

    return ScrapedHeader(
        cust_nm            = cust_nm,
        cust_mbl_no        = cust_mbl_no,
        cust_branch_nm      = cust_branch_nm,
        pmt_ty_cd_display   = pmt_ty_cd_display,
        order_no           = order_no,
        delivery_note_no   = delivery_note_no,
        grn_no             = grn_no,
        invoice_no         = invoice_no,
        store_no           = store_no,
        remark_raw         = remark_raw,
        receipt_no         = receipt_no,
    )


def parse_receipt_popup(html: str) -> list[ScrapedItem]:
    """
    Parse the response of POST /app/ebm/trns/popup/popupTrnsSalesReceipt.
    Returns the authoritative line items (itemCd, itemClsCd, price, qty, tax type).
    """
    soup = BeautifulSoup(html, "html.parser")

    header_row = soup.find("th", string=re.compile(r"Item sequence", re.I))
    if header_row is None:
        raise ScrapeError("popupTrnsSalesReceipt: could not find the item table header")

    table = header_row.find_parent("table")
    body = table.find("tbody")

    items: list[ScrapedItem] = []
    for row in body.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all("td")]
        if len(cells) < 10:
            continue  # skip stray/empty rows

        # columns: seq, itemClsCd, itemCd, itemNm, unitPrice, qty,
        #          supplyPrice, discountAmt, taxableSupplyPrice, taxType, vat
        _, item_cls_cd, item_cd, item_nm, unit_price, qty, *_rest, tax_ty_cd_col, _vat = cells

        items.append(ScrapedItem(
            item_cls_cd = _clean(item_cls_cd),
            item_cd     = _clean(item_cd),
            item_nm     = _clean(item_nm),
            prc         = float(unit_price.replace(",", "")),
            qty         = float(qty.replace(",", "")),
            tax_ty_cd   = _clean(tax_ty_cd_col),
        ))

    if not items:
        raise ScrapeError("popupTrnsSalesReceipt: item table was found but had no rows")

    return items