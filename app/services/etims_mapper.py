"""
app/services/etims_mapper.py

Converts a confirmed GRN (+ its resolved Business/Branch) into the
structured payload expected by fill_kra.py and the eTIMS REST API.

Returns a 3-tuple: (invoice_header, items_list, meta) where:
  invoice_header – dict consumed by fill_kra.invoice_dict_to_receipt()
  items_list     – list of line-item dicts (keys aligned with fill_kra.grn_to_receipt)
  meta           – carries business_id, branch_id, business_name, branch_name,
                   invoice_amount for stamping onto the EtimsInvoice row.
"""

import uuid
from typing import Optional

from app.core.logging import get_logger

logger = get_logger(__name__)


# ── Store number lookup ────────────────────────────────────────────────────────
STORE_NUMBER_MAP: dict[str, str] = {
    "NAIVASHA KUBWA"            : "6",
    "NAIVAS KUBWA"              : "6",
    "NAIVASHA NDOGO"            : "1",
    "NAIVAS NDOGO"              : "1",
    "NAIVAS SUPERCENTER"        : "19",
    "NAKURU MIDTOWN"            : "99",
    "NAIVAS CENTRAL FRUITS&VEG" : "91",
    "NAIVAS CENTRAL FRUITS"     : "91",
    "NAIVAS SAFARI"             : "110",
    "CLEANSHELF NAKURU"         : "CS1",
    "SAFARI CENTER NAIVASHA"    : "110",
}


def get_store_no(store_name: str) -> str:
    if not store_name:
        return "?"
    key = store_name.strip().upper()
    if key in STORE_NUMBER_MAP:
        return STORE_NUMBER_MAP[key]
    for map_key, number in STORE_NUMBER_MAP.items():
        if map_key in key or key in map_key:
            return number
    return "?"


def build_etims_payload(
    confirmed_data: dict,
    invoice_no: str,
    *,
    business_id:   uuid.UUID | None = None,
    branch_id:     uuid.UUID | None = None,
    business_name: str | None       = None,
    branch_name:   str | None       = None,
) -> tuple[dict, list[dict], dict]:
    """
    Converts confirmed GRN data into (invoice_header, items_list, meta).

    Args:
        confirmed_data: the JSONB dict stored in grn.confirmed_data
        invoice_no:     the invoice reference (grn.invoice_no)
        business_id:    UUID of the resolved Business (from grn.business_id)
        branch_id:      UUID of the resolved Branch   (from grn.branch_id)
        business_name:  snapshot name for denormalisation
        branch_name:    snapshot name for denormalisation

    Returns:
        invoice  – header dict for fill_kra.invoice_dict_to_receipt()
        items    – list of line-item dicts
        meta     – dict with business_id, branch_id, business_name,
                   branch_name, invoice_amount

    Raises:
        ValueError if confirmed_data has no items
    """
    items_raw = confirmed_data.get("items", [])
    if not items_raw:
        raise ValueError("No items found in confirmed GRN data")

    # ── Resolve store identity ────────────────────────────────────────────────
    store_block = confirmed_data.get("store") or {}
    resolved_store_name = (
        branch_name
        or store_block.get("store_name")
        or store_block.get("company_name")
        or business_name
        or ""
    )
    resolved_business_name = (
        business_name
        or store_block.get("company_name")
        or "NAIVAS LIMITED"
    )

    store_no = get_store_no(resolved_store_name)

    # ── Build remark string ───────────────────────────────────────────────────
    # FIX 3: remark format now exactly matches ReceiptHeader.remark property in fill_kra.py
    remark = (
        f"Order No.{confirmed_data.get('lpo_number', '')},"
        f"Delivery Note No.{confirmed_data.get('delivery_invoice_no', '')},"
        f"Grn No. {confirmed_data.get('receipt_voucher_no', '')},"
        f"Invoice No.{invoice_no},"
        f"Store No {store_no}"
    )

    # ── Invoice header ────────────────────────────────────────────────────────
    invoice = {
        "custTin"       : "P000000000A",
        "custNm"        : resolved_business_name,
        "custBranchNm"  : resolved_store_name,
        "custMblNo"     : "0722000000",
        "custMblFornNo" : "",
        "pmtTyCd"       : "02",
        "remark"        : remark,
    }

    # ── Line items ────────────────────────────────────────────────────────────
    # FIX 5: use consistent key names that fill_kra.grn_to_receipt() /
    # fill_kra.invoice_dict_to_receipt() both understand:
    #   itemNm  (was itemNm  ✓ — mapper already used this, grn_to_receipt now reads it)
    #   itemCd  (was itemCd  ✓)
    #   qty     (float, not string — grn_to_receipt calls float() on it anyway,
    #             but keeping as float avoids double-conversion surprises)
    #   prc     (float, not string)
    #   dcRt    (was "dcRt": "0" as a string — keep consistent with SaleItem.dc_rt float)
    items = []
    for raw in items_raw:
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump()
        items.append({
            "itemCd" : "",
            "itemNm" : raw.get("description", ""),
            "uom"    : raw.get("uom", "KG"),
            "qty"    : float(raw.get("qty_received", 1)),   # FIX: float, not str
            "prc"    : float(raw.get("unit_price", 0)),     # FIX: float, not str
            "dcRt"   : 0.0,                                  # FIX: float, not "0"
        })

    # ── Meta: stamp onto EtimsInvoice row ─────────────────────────────────────
    meta = {
        "business_id"    : business_id,
        "branch_id"      : branch_id,
        "business_name"  : resolved_business_name,
        "branch_name"    : resolved_store_name,
        "invoice_amount" : float(confirmed_data.get("order_total") or 0),
    }

    logger.info(
        "Built eTIMS payload: invoice_no=%s  business=%s  branch=%s  items=%d",
        invoice_no, resolved_business_name, resolved_store_name, len(items),
    )
    return invoice, items, meta