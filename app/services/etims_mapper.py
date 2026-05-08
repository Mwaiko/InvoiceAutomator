"""
app/services/etims_mapper.py

Converts a confirmed GRN (+ its resolved Business/Branch) into the
structured payload expected by fill_kra.py and the eTIMS REST API.

Returns a 3-tuple: (invoice_header, items_list, meta) where:
  invoice_header – dict consumed by fill_kra.invoice_dict_to_receipt()
  items_list     – list of line-item dicts (keys aligned with fill_kra.grn_to_receipt)
  meta           – carries business_id, branch_id, business_name, branch_name,
                   invoice_amount, store_number, invoice_number for stamping
                   onto the EtimsInvoice row.

Invoice numbering
─────────────────
Each store (identified by its store_no) has its own sequential invoice counter.
The STORE_SEED_INVOICE map records the last invoice number that was manually
submitted before this system took over.  On every new submission the system:

  1. Queries etims_invoices for the highest invoice_number already saved for
     this store_number.
  2. If a DB record exists → next = that number + 1.
  3. If no DB record exists → next = seed + 1  (first automated invoice).
  4. Saves the new invoice_number to the EtimsInvoice row via meta so the
     caller (etims_tasks.submit_to_etims) can persist it before returning.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.services.etims_item_map import resolve_item

logger = get_logger(__name__)


# ── Store number lookup ────────────────────────────────────────────────────────
# Key   = normalised store name (upper-case, stripped)
# Value = store_no string used on the eTIMS invoice
STORE_NUMBER_MAP: dict[str, str] = {
    "NAIVASHA KUBWA"            : "6",    # LATEST INVOICE NO 014
    "NAIVAS KUBWA"              : "6",    # LATEST INVOICE NO 014
    "NAIVASHA NDOGO"            : "1",    # LATEST INVOICE NO 006
    "NAIVAS NDOGO"              : "1",    # LATEST INVOICE NO 006
    "NAIVAS SUPERCENTER"        : "19",   # LATEST INVOICE NO 004
    "NAKURU SUPERCENTRE"        : "19",   # LATEST INVOICE NO 004
    "NAKURU WESTSIDE"           : "63",   # LATEST INVOICE NO 011
    "NAKURU MIDTOWN"            : "99",   # LATEST INVOICE NO 004
    "NAIVAS CENTRAL FRUITS&VEG" : "91",   # LATEST INVOICE NO 007
    "NAIVAS CENTRAL FRUITS"     : "91",   # LATEST INVOICE NO 007
    "NAIVAS SAFARI"             : "110",  # LATEST INVOICE NO 199
    "NAKURU"                    : "CS1",  # LATEST INVOICE NO 101
    "SAFARI CENTER NAIVASHA"    : "110",  # LATEST INVOICE NO 199
}

# Seed values = last invoice number submitted *manually* (before automation).
# The next automated invoice for each store will be seed + 1.
# Keys must match the store_no values in STORE_NUMBER_MAP exactly.
STORE_SEED_INVOICE: dict[str, int] = {
    "6"   : 18,   # NAIVAS KUBWA / NAIVASHA KUBWA
    "1"   : 6,    # NAIVAS NDOGO / NAIVASHA NDOGO
    "19"  : 5,    # NAIVAS SUPERCENTER
    "63"  : 13,   # NAKURU WESTSIDE
    "99"  : 4,    # NAKURU MIDTOWN
    "91"  : 7,    # NAIVAS CENTRAL FRUITS & VEG
    "110" : 203,  # NAIVAS SAFARI / SAFARI CENTER NAIVASHA
    "CS1" : 103,  # CLEANSHELF NAKURU
}


def get_store_no(store_name: str) -> str:
    """Return the store number string for a given store name, or '?' if unknown."""
    if not store_name:
        return "?"
    key = store_name.strip().upper()
    if key in STORE_NUMBER_MAP:
        return STORE_NUMBER_MAP[key]
    for map_key, number in STORE_NUMBER_MAP.items():
        if map_key in key or key in map_key:
            return number
    return "?"


async def next_invoice_number(db: AsyncSession, store_no: str) -> int:
    """
    Return the next sequential invoice number for *store_no*.

    Strategy (per-store, not global):
      • Look up all invoice_number values already stored in etims_invoices
        for this store_number, filter to purely numeric ones, take the max.
      • If found  → next = max + 1
      • If not    → next = STORE_SEED_INVOICE[store_no] + 1
                    (falls back to 1 if the store has no seed entry)

    Alphanumeric legacy values (e.g. "2065Q") are skipped so they never
    corrupt the counter.
    """
    from app.db.models.etims_invoice import EtimsInvoice

    result = await db.execute(
        select(EtimsInvoice.invoice_no)
        .where(
            EtimsInvoice.store_number == store_no,
            EtimsInvoice.invoice_no.is_not(None),
        )
    )
    rows = result.scalars().all()

    max_saved: int | None = None
    for raw in rows:
        try:
            val = int(raw)
            if max_saved is None or val > max_saved:
                max_saved = val
        except (TypeError, ValueError):
            pass  # skip alphanumeric legacy values like "2065Q"

    if max_saved is not None:
        return max_saved + 1

    seed = STORE_SEED_INVOICE.get(store_no, 0)
    return seed + 1


async def build_etims_payload(
    confirmed_data: dict,
    invoice_no: str,
    db: AsyncSession,
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
        invoice_no:     the GRN-level invoice reference (grn.invoice_no) — kept
                        for backwards-compat but the sequential number now takes
                        precedence in the remark and on the invoice header
        db:             async DB session — needed to query the invoice counter
        business_id:    UUID of the resolved Business (from grn.business_id)
        branch_id:      UUID of the resolved Branch   (from grn.branch_id)
        business_name:  snapshot name for denormalisation
        branch_name:    snapshot name for denormalisation

    Returns:
        invoice  – header dict for fill_kra.invoice_dict_to_receipt()
        items    – list of line-item dicts
        meta     – dict with business_id, branch_id, business_name,
                   branch_name, invoice_amount, store_number, lpo_number
                   (caller MUST write lpo_number back to EtimsInvoice row
                    so the counter advances correctly next time)

    Raises:
        ValueError if confirmed_data has no items
    """
    items_raw = confirmed_data.get("items", [])
    logger.debug("Items raw data: %s", items_raw)
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

    # ── Fall back to Branch record if name lookup failed ─────────────────────
    # If the store name wasn't found in STORE_NUMBER_MAP we hit the DB and:
    #   1. Use branch.store_number as the store_no.
    #   2. Replace resolved_store_name with branch.branch_name so the remark
    #      and buyer display name reflect the canonical DB value, not whatever
    #      unrecognised string came in on the GRN.
    if store_no == "?" and branch_id:
        from app.db.models.business import Branch as BranchModel
        _branch = await db.get(BranchModel, branch_id)
        if _branch:
            if _branch.store_number:
                store_no = _branch.store_number
            if _branch.branch_name:
                original_store_name = resolved_store_name
                resolved_store_name = _branch.branch_name
                logger.info(
                    "store name %r not in STORE_NUMBER_MAP — "
                    "resolved to branch.branch_name=%r, store_number=%r from DB",
                    original_store_name, resolved_store_name, store_no,
                )

    # ── Resolve business mobile number ────────────────────────────────────────
    # Priority: Business.phone → Branch.phone → GRN store block → fallback
    cust_mbl_no = "0722000000"   # safe fallback
    if business_id:
        from app.db.models.business import Business as BusinessModel, Branch as BranchModel
        business_obj = await db.get(BusinessModel, business_id)
        if business_obj and business_obj.phone:
            cust_mbl_no = business_obj.phone
        elif branch_id:
            branch_obj = await db.get(BranchModel, branch_id)
            if branch_obj and branch_obj.phone:
                cust_mbl_no = branch_obj.phone

    seq_no = await next_invoice_number(db, store_no)
    # Zero-pad to 3 digits to match existing format (006, 014, 199, …)
    seq_no_str = str(seq_no).zfill(3)

    logger.info(
        "Invoice sequence: store_no=%s → next invoice_number=%s",
        store_no, seq_no_str,
    )

    # ── Build remark string ───────────────────────────────────────────────────
    remark = (
        f"Order No.{confirmed_data.get('lpo_number', '')},"
        f"Delivery Note No.{confirmed_data.get('delivery_invoice_no', '')},"
        f"Grn No. {confirmed_data.get('receipt_voucher_no', '')},"
        f"Invoice No.{seq_no_str},"
        f"Store No {store_no}"
    )

    # ── Buyer display name: "Business Name - Branch Name" ────────────────────
    if resolved_store_name and resolved_store_name != resolved_business_name:
        cust_nm = f"{resolved_business_name} - {resolved_store_name}"
    else:
        cust_nm = resolved_business_name

    # ── Invoice header ────────────────────────────────────────────────────────
    invoice = {
        "custTin"       : "",
        "custNm"        : cust_nm,
        "custBranchNm"  : resolved_store_name,
        "custMblNo"     : cust_mbl_no,
        "custMblFornNo" : "",
        "pmtTyCd"       : "07",
        "remark"        : remark,
        "invoice_no"    : seq_no_str,
    }

    # ── Line items ────────────────────────────────────────────────────────────
    items = []
    for raw in items_raw:
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump()

        description: str = raw.get("description", "")

        # Try to resolve eTIMS codes from the local catalogue first.
        # The GRN may carry codes already (e.g. from a previous lookup or manual
        # entry); we only use the catalogue values when the GRN field is blank.
        catalogue_entry = resolve_item(description)

        if catalogue_entry:
            item_cd      = raw.get("item_code") or catalogue_entry["itemCd"]
            item_cls_cd  = raw.get("item_cls_cd") or catalogue_entry["item_cls_cd"]
            uom          = raw.get("uom") or catalogue_entry["uom"]
            tax_ty_cd    = raw.get("tax_ty_cd") or catalogue_entry["tax_ty_cd"]
            if not raw.get("item_code") and catalogue_entry["itemCd"]:
                logger.info(
                    "etims_mapper: resolved '%s' → itemCd=%s cls=%s via catalogue",
                    description, catalogue_entry["itemCd"], catalogue_entry["item_cls_cd"],
                )
        else:
            # Fall back to whatever the GRN provides; use safe defaults.
            item_cd     = raw.get("item_code") or ""
            item_cls_cd = raw.get("item_cls_cd") or "5030150300"
            uom         = raw.get("uom") or "KG"
            tax_ty_cd   = raw.get("tax_ty_cd") or "D"
            if description:
                logger.warning(
                    "etims_mapper: '%s' not found in item catalogue — "
                    "using GRN-provided codes (itemCd=%r, cls=%r)",
                    description, item_cd, item_cls_cd,
                )

        items.append({
            "itemCd"      : item_cd,
            "item_cls_cd" : item_cls_cd,
            "itemNm"      : description,
            "uom"         : uom,
            "qty"         : float(raw.get("qty_received", 1)),
            "prc"         : float(raw.get("unit_price", 0)),
            "dcRt"        : 0.0,
            "tax_ty_cd"   : tax_ty_cd,
        })

    # ── Meta: stamp onto EtimsInvoice row ─────────────────────────────────────
    # store_number and invoice_number MUST be persisted by the caller so the
    # counter keeps advancing correctly on the next submission for this store.
    meta = {
        "business_id"    : business_id,
        "branch_id"      : branch_id,
        "business_name"  : resolved_business_name,
        "branch_name"    : resolved_store_name,
        "invoice_amount" : float(confirmed_data.get("order_total") or 0),
        "store_number"   : store_no,
        "invoice_number" : seq_no_str,
        'grn_no'         : confirmed_data.get('lpo_number', ''),
        'lpo_number'         : confirmed_data.get('receipt_voucher_no', '')
    }

    logger.info(
        "Built eTIMS payload: invoice_number=%s  store=%s  business=%s  branch=%s  items=%d",
        seq_no_str, store_no, resolved_business_name, resolved_store_name, len(items),
    )
    return invoice, items, meta