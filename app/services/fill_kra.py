"""
fill_kra.py  –  Submit a sales receipt to the KRA eTIMS *web portal*
               (etims.kra.go.ke) by replaying the browser form POST.

Flow
────
1.  GET  /basic/login/indexLogin       →  seed JSESSIONID + BIGip cookies
2.  POST /basic/login/loginProc        →  authenticate (mbrId / mbrPwd)
3.  For every GRN line item,
    POST /app/ebm/trns/sales/insertTrnsSalesReceipt

Endpoints confirmed from captured cURL:
  Login page : https://etims.kra.go.ke/basic/login/indexLogin
  Login POST : https://etims.kra.go.ke/basic/login/loginProc
  Sales POST : https://etims.kra.go.ke/app/ebm/trns/sales/insertTrnsSalesReceipt
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  (override via env-vars or pass EtimsConfig directly)
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL        = "https://etims.kra.go.ke"
LOGIN_PAGE_PATH = "/basic/login/indexLogin"   # GET first — seeds session cookie
LOGIN_PATH      = "/basic/login/loginProc"    # POST with mbrId / mbrPwd
SALES_PATH      = "/app/ebm/trns/sales/insertTrnsSalesReceipt"

# Retry / timeout knobs
TIMEOUT_S       = 30
MAX_RETRIES     = 3
BACKOFF_FACTOR  = 1.5   # waits 1.5 s, 3 s, 4.5 s between retries


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EtimsConfig:
    pin:      str
    username: str
    password: str
    # FIX 1: added missing `branch` field that the CLI __main__ block passes in
    branch:   str = "001"
    base_url: str = BASE_URL


@dataclass
class SaleItem:
    """One line on the receipt — mirrors the portal form fields."""
    item_cls_cd:  str           # e.g. "5030150300"
    item_cd:      str           # taxpayer item code  e.g. "KE2BGXKGX00001"
    item_nm:      str           # description
    pkg_unit_cd:  str = "BG"    # packaging unit
    qty_unit_cd:  str = "KG"    # qty unit
    qty:          float = 1.0
    prc:          float = 0.0
    dc_rt:        float = 0.0   # discount rate %
    tax_ty_cd:    str = "D"     # A=16% VAT, B=16%, D=exempt, E=8%
    bcd:          str = ""      # barcode (optional)
    pkg:          str = ""      # package count (optional)

    # ── Derived / computed ────────────────────────────────────────────────────
    @property
    def dc_amt(self) -> float:
        return round(self.prc * self.qty * self.dc_rt / 100, 2)

    @property
    def sply_amt(self) -> float:
        return round(self.prc * self.qty - self.dc_amt, 2)

    @property
    def tax_rate(self) -> float:
        return {"A": 16.0, "B": 16.0, "E": 8.0}.get(self.tax_ty_cd, 0.0)

    @property
    def taxbl_amt(self) -> float:
        if self.tax_rate == 0:
            return self.sply_amt
        return round(self.sply_amt / (1 + self.tax_rate / 100), 2)

    @property
    def tax_amt(self) -> float:
        if self.tax_rate == 0:
            return 0.0
        return round(self.sply_amt - self.taxbl_amt, 2)

    @property
    def tot_amt(self) -> float:
        return self.sply_amt   # portal totAmt = splyAmt (tax already inside)


@dataclass
class ReceiptHeader:
    """Header fields shared across all items in one receipt."""
    cust_nm:           str
    cust_mbl_no:       str = "020 8000792"
    cust_tin:          str = ""
    cust_mbl_forn_no:  str = ""
    # FIX 2: added custBranchNm field that etims_mapper.py puts in its invoice dict
    cust_branch_nm:    str = ""
    pmt_ty_cd:         str = "07"   # 06=other, 07=credit
    items:             list[SaleItem] = field(default_factory=list)

    # ── NON-FISCAL INFORMATION ────────────────────────────────────────────────
    order_no:          str = ""
    delivery_note_no:  str = ""
    grn_no:            str = ""
    invoice_no:        str = ""
    store_no:          str = ""

    @property
    def remark(self) -> str:
        """Build the NON-FISCAL remark string exactly as the portal expects it."""
        # FIX 3: unified remark format — matches etims_mapper.py build exactly
        return (
            f"Order No.{self.order_no},"
            f"Delivery Note No.{self.delivery_note_no},"
            f"Grn No. {self.grn_no},"
            f"Invoice No.{self.invoice_no},"
            f"Store No {self.store_no}"
        )

    # ── Receipt-level totals (computed from items) ────────────────────────────
    @property
    def tot_sply_amt(self) -> float:
        return round(sum(i.sply_amt for i in self.items), 2)

    @property
    def tot_taxbl_amt(self) -> float:
        return round(sum(i.taxbl_amt for i in self.items), 2)

    @property
    def tot_tax_amt(self) -> float:
        return round(sum(i.tax_amt for i in self.items), 2)

    @property
    def sum_tot_amt(self) -> float:
        return round(sum(i.tot_amt for i in self.items), 2)

    def taxbl_by_code(self, code: str) -> float:
        return round(sum(i.taxbl_amt for i in self.items if i.tax_ty_cd == code), 2)

    def tax_by_code(self, code: str) -> float:
        return round(sum(i.tax_amt for i in self.items if i.tax_ty_cd == code), 2)

    def tax_rate_for(self, code: str) -> float:
        rates = {i.tax_ty_cd: i.tax_rate for i in self.items}
        return rates.get(code, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION FACTORY  (retry + keep-alive)
# ─────────────────────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    sess = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["POST", "GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    sess.mount("https://", adapter)
    sess.mount("http://",  adapter)

    sess.headers.update({
        "User-Agent":      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
        "sec-ch-ua":        '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    })
    return sess


# ─────────────────────────────────────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────────────────────────────────────

def login(cfg: EtimsConfig, session: requests.Session) -> None:
    base = cfg.base_url

    # Step 1: seed the session cookie
    seed_url = f"{base}{LOGIN_PAGE_PATH}"
    log.info("  → GET %s  (seeding session cookie)", seed_url)
    r0 = session.get(seed_url, timeout=TIMEOUT_S)
    _raise_for_kra(r0, context="login-page")

    # Step 2: submit credentials
    url = f"{base}{LOGIN_PATH}"
    payload = {
        "mbrId":  cfg.username,
        "mbrPwd": cfg.password,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept":       "application/json, text/javascript, */*; q=0.01",
        "Origin":       base,
        "Referer":      f"{base}{LOGIN_PAGE_PATH}",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    log.info("  → POST %s  (mbrId=%s)", url, cfg.username)
    r = session.post(url, data=payload, headers=headers, timeout=TIMEOUT_S)
    _raise_for_kra(r, context="loginProc")

    if "JSESSIONID" not in session.cookies:
        raise KraError(
            "Login POST returned HTTP 200 but no JSESSIONID cookie was set. "
            "Verify mbrId / mbrPwd are correct and LOGIN_PATH is right."
        )

    try:
        body = r.json()
        rc = str(body.get("resultCd", "000")).strip()
        if rc != "000":
            raise KraError(
                f"Login rejected by KRA  resultCd={rc}  "
                f"msg={body.get('resultMsg', 'n/a')}"
            )
    except ValueError:
        pass

    log.info("✅ eTIMS login OK  (JSESSIONID=%s…)", session.cookies["JSESSIONID"][:8])


# ─────────────────────────────────────────────────────────────────────────────
# BUILD FORM PAYLOAD
# ─────────────────────────────────────────────────────────────────────────────

def _build_form(header: ReceiptHeader) -> list[tuple[str, str]]:
    def f(v: float) -> str:
        return f"{v:.2f}" if v != int(v) else str(int(v))

    # FIX 4: added custBranchNm to the form payload to match the new field
    pairs: list[tuple[str, str]] = [
        ("custTin",       header.cust_tin),
        ("custNm",        header.cust_nm),
        ("custBranchNm",  header.cust_branch_nm),
        ("custMblNo",     header.cust_mbl_no),
        ("custMblFornNo", header.cust_mbl_forn_no),
        ("pmtTyCd",       header.pmt_ty_cd),

        ("totSplyAmt",    f(header.tot_sply_amt)),
        ("totTaxblAmt",   f(header.tot_taxbl_amt)),
        ("totTaxAmt",     f(header.tot_tax_amt)),
        ("sumTotAmt",     f(header.sum_tot_amt)),

        ("taxblAmtA",     f(header.taxbl_by_code("A"))),
        ("taxAmtA",       f(header.tax_by_code("A"))),
        ("taxRtA",        f(header.tax_rate_for("A"))),
        ("taxblAmtB",     f(header.taxbl_by_code("B"))),
        ("taxAmtB",       f(header.tax_by_code("B"))),
        ("taxRtB",        "16"),
        ("taxblAmtC",     f(header.taxbl_by_code("C"))),
        ("taxAmtC",       f(header.tax_by_code("C"))),
        ("taxRtC",        "0"),
        ("taxblAmtE",     f(header.taxbl_by_code("E"))),
        ("taxAmtE",       f(header.tax_by_code("E"))),
        ("taxRtE",        "8"),
        ("taxblAmtD",     f(header.taxbl_by_code("D"))),
        ("taxAmtD",       f(header.tax_by_code("D"))),
        ("taxRtD",        "0"),
    ]

    for item in header.items:
        pairs += [
            ("itemClsCd", item.item_cls_cd),
            ("itemCd",    item.item_cd),
            ("bcd",       item.bcd),
            ("itemNm",    item.item_nm),
            ("pkgUnitCd", item.pkg_unit_cd),
            ("pkg",       item.pkg),
            ("qtyUnitCd", item.qty_unit_cd),
            ("qty",       f(item.qty)),
            ("prc",       f(item.prc)),
            ("dcRt",      f(item.dc_rt)),
            ("dcAmt",     f(item.dc_amt)),
            ("splyAmt",   f(item.sply_amt)),
            ("taxblAmt",  f(item.taxbl_amt)),
            ("taxTyRate", f(item.tax_rate)),
            ("taxTyCd",   item.tax_ty_cd),
            ("taxAmt",    f(item.tax_amt)),
            ("totAmt",    f(item.tot_amt)),
        ]

    pairs.append(("remark", header.remark))
    return pairs


# ─────────────────────────────────────────────────────────────────────────────
# SUBMIT ALL ITEMS IN ONE REQUEST
# ─────────────────────────────────────────────────────────────────────────────

def _submit_receipt(
    session: requests.Session,
    cfg:     EtimsConfig,
    header:  ReceiptHeader,
) -> dict:
    url  = f"{cfg.base_url}{SALES_PATH}"
    form = _build_form(header)

    hdrs = {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept":       "application/json, text/javascript, */*; q=0.01",
        "Origin":       cfg.base_url,
        "Referer":      (
            f"{cfg.base_url}/app/ebm/trns/sales/"
            "indexTrnsSalesReceiptDetail?bhfId=&invcNo=&curRcptNo="
        ),
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }

    item_names = [i.item_nm for i in header.items]
    log.info("  → Submitting %d item(s) in one receipt: %s", len(header.items), item_names)

    r = session.post(url, data=form, headers=hdrs, timeout=TIMEOUT_S)
    _raise_for_kra(r, context="insertTrnsSalesReceipt")

    try:
        return r.json()
    except ValueError:
        return {"raw": r.text.strip()}


def _extract_kracu(response_data):
    """
    Safely extracts the KRACU invoice number from various KRA response formats.
    """
    if not response_data:
        return None
        
    # 1. If it's already a string, check if it's the KRACU number
    if isinstance(response_data, str):
        if "KRACU" in response_data:
            return response_data
        return None

    # 2. Search common keys in the dictionary
    # The portal often returns 'invcNo' for the full KRACU string
    keys_to_check = ["invcNo", "cuInvcNo", "invoiceNo", "receiptNo"]
    
    # Check top level
    for key in keys_to_check:
        val = response_data.get(key)
        if val and "KRACU" in str(val):
            return str(val)
            
    # 3. Check inside the 'data' or 'result' nested objects (Very Common)
    for outer_key in ["data", "result", "rtnData"]:
        inner = response_data.get(outer_key)
        if isinstance(inner, dict):
            for key in keys_to_check:
                val = inner.get(key)
                if val and "KRACU" in str(val):
                    return str(val)
                    
    return None

def run_fill(
    cfg:    EtimsConfig,
    header: ReceiptHeader,
    *,
    delay_between_items: float = 0.5,
    download_pdf: bool = False,
    download_dir: "Path | str | None" = None,
) -> list[dict]:
    """
    Submit the receipt to KRA eTIMS.

    Parameters
    ----------
    cfg, header         : as before
    delay_between_items : kept for API compatibility (single-request flow, not used)
    download_pdf        : if True, immediately download the receipt PDF after a
                          successful submission using the KRACU returned by KRA.
                          Uses the same authenticated session — no extra login.
    download_dir        : directory to save the PDF (default: ./downloaded_receipts).
                          Ignored when download_pdf is False.

    Returns
    -------
    list[dict] — same structure as before, with an extra key ``pdf_path`` (str)
    added to each successful entry when download_pdf=True.
    """
    if not header.items:
        raise ValueError("ReceiptHeader has no items — nothing to submit.")

    # Resolve download_dir early so import errors surface before the network call
    if download_pdf:
        from pathlib import Path as _Path
        from download_invoice import download_receipt_pdf  # reuse existing logic
        _out_dir = _Path(download_dir) if download_dir else _Path("./downloaded_receipts")

    session = _make_session()

    log.info("🔑 Logging in to eTIMS portal (%s)…", cfg.base_url)
    login(cfg, session)

    try:
        resp = _submit_receipt(session, cfg, header)
        result: dict = {"items": [i.item_nm for i in header.items], "status": "ok", "response": resp}
        log.info("  ✅ Receipt accepted  (%d item(s))", len(header.items))

        # ── Auto-download PDF on the same session ─────────────────────────────
        if download_pdf:
            kracu = _extract_kracu(resp)
            if kracu:
                log.info("  📥 Downloading receipt PDF for %s …", kracu)
                pdf_path = download_receipt_pdf(session, cfg, kracu, _out_dir)
                result["kracu"]    = kracu
                result["pdf_path"] = str(pdf_path) if pdf_path else None
                if pdf_path:
                    log.info("  ✅ PDF saved → %s", pdf_path)
                else:
                    log.warning("  ⚠️  PDF download failed for %s", kracu)
            else:
                log.warning(
                    "  ⚠️  download_pdf=True but no KRACU found in KRA response. "
                    "Raw response: %s", str(resp)[:200]
                )
                result["kracu"]    = None
                result["pdf_path"] = None

        results = [result]

    except KraError as exc:
        log.error("  ❌ Receipt submission failed: %s", exc)
        results = [{"items": [i.item_nm for i in header.items], "status": "error", "error": str(exc)}]
    except Exception as exc:                          # noqa: BLE001
        log.error("  ❌ Unexpected error: %s", exc)
        results = [{"items": [i.item_nm for i in header.items], "status": "error", "error": str(exc)}]

    ok_count = sum(1 for r in results if r["status"] == "ok")
    log.info("📊 Done: %d/%d receipts submitted successfully.", ok_count, len(results))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

class KraError(Exception):
    """Raised for KRA-specific HTTP or application-level errors."""


def _raise_for_kra(r: requests.Response, *, context: str = "") -> None:
    prefix = f"[{context}] " if context else ""

    if r.status_code == 401:
        raise KraError(f"{prefix}Session expired or invalid credentials (HTTP 401). Re-login needed.")
    if r.status_code == 403:
        raise KraError(f"{prefix}Access forbidden (HTTP 403). Check PIN/branch permissions.")
    if r.status_code == 429:
        raise KraError(f"{prefix}Rate-limited by KRA portal (HTTP 429). Slow down or retry later.")
    if not r.ok:
        raise KraError(f"{prefix}HTTP {r.status_code}: {r.text[:300]}")

    try:
        body = r.json()
        result_cd = str(body.get("resultCd", "")).strip()
        if result_cd and result_cd != "000":
            raise KraError(
                f"{prefix}KRA application error  resultCd={result_cd}  "
                f"msg={body.get('resultMsg', 'n/a')}"
            )
    except (ValueError, AttributeError):
        pass


# ─────────────────────────────────────────────────────────────────────────────
# GRN → ReceiptHeader  converter
# ─────────────────────────────────────────────────────────────────────────────

def grn_to_receipt(grn: dict, cfg: EtimsConfig) -> ReceiptHeader:
    """
    Convert a parsed GRN dict (from read_salesReceipt.read_Grn) into a
    ReceiptHeader ready for run_fill().

    Also accepts the line-item dicts produced by etims_mapper.build_etims_payload(),
    which use keys itemNm / qty / prc / dcRt instead of description / qty_received /
    unit_price / dc_rt.  Both formats are handled transparently.
    """
    store        = grn.get("store") or {}
    company_name = (store.get("company_name") or "").strip()
    store_name   = (store.get("store_name")   or "").strip()
    buyer_name   = f"{company_name} {store_name}".strip() or "UNKNOWN BUYER"

    items: list[SaleItem] = []
    for line in grn.get("items", []):
        # FIX 5: accept BOTH key-name conventions so grn_to_receipt() works
        # whether called with raw-GRN dicts (description/qty_received/unit_price)
        # or mapper output dicts (itemNm/qty/prc).
        raw_qty = line.get("qty_received") or line.get("qty", 1)
        raw_prc = line.get("unit_price")   or line.get("prc", 0)
        item_nm = (line.get("description") or line.get("itemNm") or "").strip()
        item_cd = (line.get("item_code")   or line.get("itemCd") or "")

        try:
            qty = float(raw_qty)
        except (TypeError, ValueError):
            log.warning("Bad qty for item %r → defaulting to 1", item_nm)
            qty = 1.0

        try:
            prc = float(raw_prc)
        except (TypeError, ValueError):
            log.warning("Bad price for item %r → defaulting to 0", item_nm)
            prc = 0.0

        items.append(SaleItem(
            item_cls_cd = line.get("item_cls_cd",  "5030150300"),
            item_cd     = item_cd,
            item_nm     = item_nm,
            pkg_unit_cd = line.get("pkg_unit_cd",  "BG"),
            qty_unit_cd = line.get("uom",          "KG"),
            qty         = qty,
            prc         = prc,
            dc_rt       = float(line.get("dcRt") or line.get("dc_rt") or 0),
            tax_ty_cd   = line.get("tax_ty_cd",   "D"),
        ))

    if not items:
        raise ValueError("GRN contains no line items — cannot build receipt.")

    return ReceiptHeader(
        cust_nm         = grn.get("custNm",  buyer_name) or buyer_name,
        cust_tin        = grn.get("custTin", "") or "",
        cust_branch_nm  = grn.get("custBranchNm", store_name) or store_name,
        cust_mbl_no     = grn.get("custMblNo", "020 8000792") or "020 8000792",
        cust_mbl_forn_no= grn.get("custMblFornNo", "") or "",
        pmt_ty_cd       = grn.get("pmtTyCd", "07") or "07",
        items           = items,
        # Non-fiscal fields — read from top-level GRN keys
        order_no        = grn.get("lpo_number",          "") or "",
        delivery_note_no= grn.get("delivery_invoice_no", "") or "",
        grn_no          = grn.get("receipt_voucher_no",  "") or "",
        invoice_no      = grn.get("invoice_no",          "") or "",
        store_no        = grn.get("store_no",            "") or "",
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAPPER BRIDGE: etims_mapper invoice dict → ReceiptHeader
# ─────────────────────────────────────────────────────────────────────────────

def invoice_dict_to_receipt(invoice: dict, items_list: list[dict]) -> ReceiptHeader:
    """
    Convert the (invoice, items_list) tuple returned by
    etims_mapper.build_etims_payload() directly into a ReceiptHeader.

    This is the missing bridge between etims_mapper and fill_kra:
      invoice_header, items_list, meta = build_etims_payload(...)
      header = invoice_dict_to_receipt(invoice_header, items_list)
      results = run_fill(cfg, header)
    """
    sale_items: list[SaleItem] = []
    for line in items_list:
        try:
            qty = float(line.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1.0
        try:
            prc = float(line.get("prc", 0))
        except (TypeError, ValueError):
            prc = 0.0

        sale_items.append(SaleItem(
            item_cls_cd = line.get("item_cls_cd",  "5030150300"),
            item_cd     = line.get("itemCd",       ""),
            item_nm     = (line.get("itemNm")      or "").strip(),
            pkg_unit_cd = line.get("pkg_unit_cd",  "BG"),
            qty_unit_cd = line.get("uom",          "KG"),
            qty         = qty,
            prc         = prc,
            dc_rt       = float(line.get("dcRt") or 0),
            tax_ty_cd   = line.get("tax_ty_cd",   "D"),
        ))

    if not sale_items:
        raise ValueError("items_list is empty — cannot build ReceiptHeader.")

    # Parse non-fiscal fields out of the remark string if present, or fall
    # back to empty strings (caller can override before run_fill).
    remark = invoice.get("remark", "")
    order_no = delivery_note_no = grn_no = inv_no = store_no = ""
    for part in remark.split(","):
        part = part.strip()
        if part.startswith("Order No."):
            order_no = part[len("Order No."):]
        elif part.startswith("Delivery Note No."):
            delivery_note_no = part[len("Delivery Note No."):]
        elif part.startswith("Grn No."):
            grn_no = part[len("Grn No."):].strip()
        elif part.startswith("Invoice No."):
            inv_no = part[len("Invoice No."):]
        elif part.startswith("Store No"):
            store_no = part.split()[-1]

    # Prefer the direct "invoice_no" key over what was parsed from the remark
    # string — the remark roundtrip is lossy when invoice_no is None/empty.
    resolved_inv_no = invoice.get("invoice_no") or inv_no

    return ReceiptHeader(
        cust_nm          = invoice.get("custNm",        ""),
        cust_tin         = invoice.get("custTin",       ""),
        cust_branch_nm   = invoice.get("custBranchNm",  ""),
        cust_mbl_no      = invoice.get("custMblNo",     "020 8000792"),
        cust_mbl_forn_no = invoice.get("custMblFornNo", ""),
        pmt_ty_cd        = invoice.get("pmtTyCd",       "07"),
        items            = sale_items,
        order_no         = order_no,
        delivery_note_no = delivery_note_no,
        grn_no           = grn_no,
        invoice_no       = resolved_inv_no,
        store_no         = store_no,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI  (python fill_kra.py invoice.pdf)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import os
    import sys
    import argparse
    from pathlib import Path

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Submit a sales receipt to KRA eTIMS and optionally download the PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("invoice", metavar="invoice.pdf|invoice.jpg",
                        help="Path to the source invoice / GRN file to submit.")
    parser.add_argument("--download-pdf", action="store_true",
                        help="Download the receipt PDF immediately after a successful submission.")
    parser.add_argument("--out", metavar="DIR", default="./downloaded_receipts",
                        help="Directory to save the downloaded PDF (default: ./downloaded_receipts).")
    parser.add_argument("--skip-check", action="store_true",
                        help="Skip the pre-flight connectivity check and submit immediately.")
    parser.add_argument("--check-timeout", type=float, default=15,
                        help="Timeout (seconds) for each connectivity check step (default: 15).")
    parser.add_argument("--wait", type=int, metavar="SECONDS",
                        help="If the site is down, keep retrying the connectivity check every N "
                             "seconds until it recovers, then submit automatically.")
    args = parser.parse_args()

    cfg = EtimsConfig(
        pin      = os.environ.get("KRA_PIN",      "YOUR_KRA_PIN"),
        branch   = os.environ.get("KRA_BRANCH",   "001"),
        username = os.environ.get("KRA_USERNAME",  "YOUR_USERNAME"),
        password = os.environ.get("KRA_PASSWORD",  "YOUR_PASSWORD"),
    )

    # ── Pre-flight connectivity check ─────────────────────────────────────────
    if not args.skip_check:
        try:
            from test_etims_connection import run_health_check, print_report
        except ImportError:
            log.warning(
                "⚠️  test_etims_connection.py not found — skipping pre-flight check. "
                "Place it in the same directory to enable it."
            )
        else:
            import time as _time

            attempt = 0
            while True:
                attempt += 1
                if attempt > 1:
                    print(f"\n[Attempt {attempt}]", end="")

                report = run_health_check(
                    full     = False,   # connectivity only — no second login needed
                    timeout  = args.check_timeout,
                )
                print_report(report)

                if report.site_up:
                    break

                if args.wait:
                    print(f"  ⏳ Site is down. Retrying in {args.wait}s… (Ctrl-C to cancel)")
                    _time.sleep(args.wait)
                else:
                    print(
                        "  ❌ Aborting — KRA eTIMS is unreachable.\n"
                        "     Tip: use --wait 60 to keep retrying, or --skip-check to submit anyway."
                    )
                    sys.exit(1)

    # ── Parse invoice & submit ─────────────────────────────────────────────────
    from read_salesReceipt import read_Grn

    grn    = read_Grn(args.invoice)
    header = grn_to_receipt(grn, cfg)

    print(f"\n📋 {len(header.items)} item(s) parsed.  Customer: {header.cust_nm}")
    print(f"   Total supply: {header.tot_sply_amt}  Tax: {header.tot_tax_amt}  Grand: {header.sum_tot_amt}\n")

    results = run_fill(
        cfg,
        header,
        download_pdf=args.download_pdf,
        download_dir=Path(args.out) if args.download_pdf else None,
    )

    print("\n── RESULTS ──")
    print(json.dumps(results, indent=2, default=str))

    # Surface the saved PDF path prominently
    for r in results:
        if r.get("pdf_path"):
            print(f"\n📄 Receipt PDF saved → {r['pdf_path']}")

    # Exit with error code if any submission failed
    if any(r.get("status") != "ok" for r in results):
        sys.exit(1)