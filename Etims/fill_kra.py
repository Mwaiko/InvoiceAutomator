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
        # For VAT-bearing items (A=16%, B=16%, E=8%): taxable base = splyAmt / (1 + rate)
        # For zero-rate / exempt items (D, C, NV): taxblAmt = splyAmt, taxAmt = 0
        # This matches the browser curl exactly:
        #   taxTyCd=D → taxblAmt=12750  taxAmt=0  (NOT taxblAmt=0)
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
    cust_nm:         str
    cust_mbl_no:     str = "020 8000792"
    cust_tin:        str = ""
    cust_mbl_forn_no: str = ""
    pmt_ty_cd:       str = "07"   # 06=other, 07=credit
    items:           list[SaleItem] = field(default_factory=list)

    # ── NON-FISCAL INFORMATION ────────────────────────────────────────────────
    # These populate the remark field sent to KRA, visible on the printed receipt
    # under "NON-FISCAL INFORMATION".  Fill all that apply.
    order_no:        str = ""   # LPO / purchase order number  e.g. "P040603675"
    delivery_note_no: str = ""  # Delivery note / invoice no   e.g. "2076QOS"
    grn_no:          str = ""   # GRN / receipt voucher number e.g. "NVS-008740467"
    invoice_no:      str = ""   # Supplier invoice number      e.g. "193"
    store_no:        str = ""   # Naivas store number          e.g. "110"

    @property
    def remark(self) -> str:
        """Build the NON-FISCAL remark string exactly as the portal expects it."""
        return (
            f"Order No.{self.order_no},"
            f"Delivery Note No.{self.delivery_note_no},"
            f"Grn No. {self.grn_no},"
            f"Invoice No. {self.invoice_no} ,"
            f"Store No.{self.store_no}"
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

    # Mimic the browser headers from the captured cURL
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
    """
    Authenticate against the eTIMS portal.

    Step 1 – GET /basic/login/indexLogin  so the server issues a fresh
             JSESSIONID + BIGip load-balancer cookie before we POST.
    Step 2 – POST mbrId / mbrPwd to /basic/login/loginProc
             (field names confirmed from captured cURL).

    The session object retains all cookies automatically for every
    subsequent request.
    """
    base = cfg.base_url

    # ── Step 1: seed the session cookie ──────────────────────────────────────
    seed_url = f"{base}{LOGIN_PAGE_PATH}"
    log.info("  → GET %s  (seeding session cookie)", seed_url)
    r0 = session.get(seed_url, timeout=TIMEOUT_S)
    _raise_for_kra(r0, context="login-page")

    # ── Step 2: submit credentials ────────────────────────────────────────────
    url = f"{base}{LOGIN_PATH}"
    payload = {
        "mbrId":  cfg.username,   # confirmed field names from cURL --data-raw
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

    # ── Validate we got a usable session ──────────────────────────────────────
    if "JSESSIONID" not in session.cookies:
        raise KraError(
            "Login POST returned HTTP 200 but no JSESSIONID cookie was set. "
            "Verify mbrId / mbrPwd are correct and LOGIN_PATH is right."
        )

    # Portal may return JSON error even on HTTP 200 e.g. {"resultCd":"011"}
    try:
        body = r.json()
        rc = str(body.get("resultCd", "000")).strip()
        if rc != "000":
            raise KraError(
                f"Login rejected by KRA  resultCd={rc}  "
                f"msg={body.get('resultMsg', 'n/a')}"
            )
    except ValueError:
        pass  # HTML / redirect response — cookie check above is sufficient

    log.info("✅ eTIMS login OK  (JSESSIONID=%s…)", session.cookies["JSESSIONID"][:8])


# ─────────────────────────────────────────────────────────────────────────────
# BUILD FORM PAYLOAD
# ─────────────────────────────────────────────────────────────────────────────

def _build_form(header: ReceiptHeader) -> list[tuple[str, str]]:
    """
    Build the x-www-form-urlencoded payload for ALL items in one receipt.

    The KRA portal expects a single POST where item-level fields (itemClsCd,
    itemCd, itemNm, qty, prc, etc.) are repeated once per line item — exactly
    as the browser does.  We use a list-of-tuples so that duplicate keys are
    preserved in the correct order when urllib encodes them.
    """

    def f(v: float) -> str:
        return f"{v:.2f}" if v != int(v) else str(int(v))

    # ── Header / receipt-level fields (sent once) ─────────────────────────────
    pairs: list[tuple[str, str]] = [
        ("custTin",       header.cust_tin),
        ("custNm",        header.cust_nm),
        ("custMblNo",     header.cust_mbl_no),
        ("custMblFornNo", header.cust_mbl_forn_no),
        ("pmtTyCd",       header.pmt_ty_cd),

        ("totSplyAmt",    f(header.tot_sply_amt)),
        ("totTaxblAmt",   f(header.tot_taxbl_amt)),
        ("totTaxAmt",     f(header.tot_tax_amt)),
        ("sumTotAmt",     f(header.sum_tot_amt)),

        # Tax breakdown by rate code
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

    # ── Per-item fields (repeated for every line) ─────────────────────────────
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

    # ── Remark sent once at the end (matches browser curl) ────────────────────
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
    """Submit the entire receipt (all items) in a single POST — matching the browser."""
    url  = f"{cfg.base_url}{SALES_PATH}"
    form = _build_form(header)   # list-of-tuples with repeated item keys

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


# ─────────────────────────────────────────────────────────────────────────────
# MAIN PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def run_fill(
    cfg:    EtimsConfig,
    header: ReceiptHeader,
    *,
    delay_between_items: float = 0.5,   # kept for API compatibility, unused
) -> list[dict]:
    """
    Log in, submit ALL items in one single receipt POST, return server response.

    Returns a list with a single dict (for API compatibility with callers that
    iterate results).  Raises on any hard failure.
    """
    if not header.items:
        raise ValueError("ReceiptHeader has no items — nothing to submit.")

    session = _make_session()

    # ── 1. Login ──────────────────────────────────────────────────────────────
    log.info("🔑 Logging in to eTIMS portal (%s)…", cfg.base_url)
    login(cfg, session)

    # ── 2. Submit entire receipt in ONE POST ──────────────────────────────────
    try:
        resp = _submit_receipt(session, cfg, header)
        results = [{"items": [i.item_nm for i in header.items], "status": "ok", "response": resp}]
        log.info("  ✅ Receipt accepted  (%d item(s))", len(header.items))
    except KraError as exc:
        log.error("  ❌ Receipt submission failed: %s", exc)
        results = [{"items": [i.item_nm for i in header.items], "status": "error", "error": str(exc)}]
    except Exception as exc:                          # noqa: BLE001
        log.error("  ❌ Unexpected error: %s", exc)
        results = [{"items": [i.item_nm for i in header.items], "status": "error", "error": str(exc)}]

    # ── 3. Summary ────────────────────────────────────────────────────────────
    ok_count = sum(1 for r in results if r["status"] == "ok")
    log.info("📊 Done: %d/%d receipts submitted successfully.", ok_count, len(results))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

class KraError(Exception):
    """Raised for KRA-specific HTTP or application-level errors."""


def _raise_for_kra(r: requests.Response, *, context: str = "") -> None:
    """Raise KraError with a clean message for any non-2xx or KRA error body."""
    prefix = f"[{context}] " if context else ""

    if r.status_code == 401:
        raise KraError(f"{prefix}Session expired or invalid credentials (HTTP 401). Re-login needed.")
    if r.status_code == 403:
        raise KraError(f"{prefix}Access forbidden (HTTP 403). Check PIN/branch permissions.")
    if r.status_code == 429:
        raise KraError(f"{prefix}Rate-limited by KRA portal (HTTP 429). Slow down or retry later.")
    if not r.ok:
        raise KraError(f"{prefix}HTTP {r.status_code}: {r.text[:300]}")

    # Some portal endpoints return HTTP 200 with a JSON error body
    try:
        body = r.json()
        result_cd = str(body.get("resultCd", "")).strip()
        if result_cd and result_cd != "000":
            raise KraError(
                f"{prefix}KRA application error  resultCd={result_cd}  "
                f"msg={body.get('resultMsg', 'n/a')}"
            )
    except (ValueError, AttributeError):
        pass   # plain-text response — already checked r.ok above


# ─────────────────────────────────────────────────────────────────────────────
# GRN → ReceiptHeader  converter  (replaces the logic that was in main.py)
# ─────────────────────────────────────────────────────────────────────────────

def grn_to_receipt(grn: dict, cfg: EtimsConfig) -> ReceiptHeader:
    """
    Convert a parsed GRN dict (from read_salesReceipt.read_Grn) into a
    ReceiptHeader ready for run_fill().

    The non-fiscal fields (order_no, delivery_note_no, grn_no, invoice_no,
    store_no) are populated from the GRN where available.  invoice_no and
    store_no are often blank in GRN PDFs — set them on the returned header
    before calling run_fill() if you have them:

        header = grn_to_receipt(grn, cfg)
        header.invoice_no = "193"
        header.store_no   = "110"
        run_fill(cfg, header)
    """
    store        = grn.get("store") or {}
    company_name = (store.get("company_name") or "").strip()
    store_name   = (store.get("store_name")   or "").strip()
    buyer_name   = f"{company_name} {store_name}".strip() or "UNKNOWN BUYER"

    items: list[SaleItem] = []
    for line in grn.get("items", []):
        raw_qty = line.get("qty_received", 1)
        raw_prc = line.get("unit_price",   0)

        try:
            qty = float(raw_qty)
        except (TypeError, ValueError):
            log.warning("Bad qty for item %r → defaulting to 1", line.get("description"))
            qty = 1.0

        try:
            prc = float(raw_prc)
        except (TypeError, ValueError):
            log.warning("Bad price for item %r → defaulting to 0", line.get("description"))
            prc = 0.0

        items.append(SaleItem(
            item_cls_cd = line.get("item_cls_cd",  "5030150300"),
            item_cd     = line.get("item_code",    ""),
            item_nm     = (line.get("description") or "").strip(),
            pkg_unit_cd = line.get("pkg_unit_cd",  "BG"),
            qty_unit_cd = line.get("uom",          "KG"),
            qty         = qty,
            prc         = prc,
            dc_rt       = float(line.get("dc_rt", 0) or 0),
            tax_ty_cd   = line.get("tax_ty_cd",   "D"),
        ))

    if not items:
        raise ValueError("GRN contains no line items — cannot build receipt.")

    return ReceiptHeader(
        cust_nm         = buyer_name,
        cust_mbl_no     = "020 8000792",
        items           = items,
        # ── Non-fiscal information ────────────────────────────────────────────
        order_no        = grn.get("lpo_number",          "") or "",
        delivery_note_no= grn.get("delivery_invoice_no", "") or "",
        grn_no          = grn.get("receipt_voucher_no",  "") or "",
        invoice_no      = grn.get("invoice_no",          "") or "",  # set manually if not in PDF
        store_no        = grn.get("store_no",            "") or "",  # set manually if not in PDF
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI  (python fill_kra.py invoice.pdf)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import os
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) < 2:
        print("Usage: python fill_kra.py <invoice.pdf|invoice.jpg>")
        sys.exit(1)

    # Read credentials from environment (safer than hard-coding)
    cfg = EtimsConfig(
        pin      = os.environ.get("KRA_PIN",      "YOUR_KRA_PIN"),
        branch   = os.environ.get("KRA_BRANCH",   "001"),
        username = os.environ.get("KRA_USERNAME",  "YOUR_USERNAME"),
        password = os.environ.get("KRA_PASSWORD",  "YOUR_PASSWORD"),
    )

    from read_salesReceipt import read_Grn

    grn     = read_Grn(sys.argv[1])
    header  = grn_to_receipt(grn, cfg)

    print(f"\n📋 {len(header.items)} item(s) parsed.  Customer: {header.cust_nm}")
    print(f"   Total supply: {header.tot_sply_amt}  Tax: {header.tot_tax_amt}  Grand: {header.sum_tot_amt}\n")

    results = run_fill(cfg, header)

    print("\n── RESULTS ──")
    print(json.dumps(results, indent=2, default=str))