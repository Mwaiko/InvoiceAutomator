"""
app/services/etims_item_map.py

eTIMS Item Catalogue
─────────────────────
A local lookup table that maps product descriptions to their KRA-registered
eTIMS codes.  This avoids hitting the KRA portal search popup on every
submission and ensures that every line item carries the correct:

  • itemCd       – KRA item code   (e.g. "KE2BEXKGX00002")
  • item_cls_cd  – Classification  (e.g. "5040150100")
  • tax_ty_cd    – Tax type        (e.g. "D" = exempt / "B" = 16 % VAT)
  • uom          – Unit of measure (e.g. "KG", "BE", "U", "GRM")
  • pkg_cd       – Package code    (e.g. "BE", "BG", "BZ")

Usage
─────
    from app.services.etims_item_map import resolve_item

    entry = resolve_item("sukuma wiki")
    # → {"itemCd": "KE2BEXKGX00002", "item_cls_cd": "5040150100",
    #    "tax_ty_cd": "D", "uom": "KG", "pkg_cd": "BE"}

    entry = resolve_item("unknown product")
    # → None   (caller must fall back to values from the GRN)

Adding new items
────────────────
1. Search for the item on the KRA portal:
     POST /app/ebm/trns/popup/trnsSalesTaxPayerItemClsList  (find cls code)
     POST /app/ebm/trns/popup/trnsSalesTaxpayerItemList     (find item code)
2. Add an entry to ETIMS_ITEM_CATALOGUE below.
3. If the item name in your GRN data differs from the KRA name, add an alias
   to ITEM_NAME_ALIASES.

Classification codes
────────────────────
5030150300  – Api apples (fresh fruit)
5040150100  – Brittany artichokes / fresh vegetables & herbs
5041150100  – Organic brittany artichokes / organic produce
8017190900  – Business & utility provider relations consultation
8613210200  – Training, planning & development consultancy services
"""

from __future__ import annotations

import re
from typing import TypedDict


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────

class ItemEntry(TypedDict):
    itemCd:      str   # KRA item code
    item_cls_cd: str   # KRA classification code
    tax_ty_cd:   str   # Tax type ("D" = exempt, "B" = 16% VAT, "E" = zero-rated)
    uom:         str   # Unit of measure
    pkg_cd:      str   # Package code


# ─────────────────────────────────────────────────────────────────────────────
# Catalogue
# Key = UPPER-CASE normalised canonical KRA item name.

ETIMS_ITEM_CATALOGUE: dict[str, ItemEntry] = {

    # ── Fresh vegetables & herbs  (cls 5040150100) ───────────────────────────
    "MATOKE"            : {"itemCd": "KE1BEXBEX00001", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "BE",  "pkg_cd": "BE"},
    "TREE TOMATO"       : {"itemCd": "KE1BEXBEX00002", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "BE",  "pkg_cd": "BE"},
    "KALES"             : {"itemCd": "KE1BEXKGX00001", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "SAGHET"            : {"itemCd": "KE1BEXKGX00002", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "MINT HERBS"        : {"itemCd": "KE1BEXKGX00003", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "KUNDE"             : {"itemCd": "KE1BEXKGX00004", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "PAW PAW"           : {"itemCd": "KE1BEXKGX00005", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "DANIA"             : {"itemCd": "KE2BEXBEX00001", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "BE",  "pkg_cd": "BE"},
    "MANAGU"            : {"itemCd": "KE2BEXBEX00002", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "BE",  "pkg_cd": "BE"},
    "TERERE"            : {"itemCd": "KE2BEXBEX00003", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "BE",  "pkg_cd": "BE"},
    "KAHURURA"          : {"itemCd": "KE2BEXBEX00004", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "BE",  "pkg_cd": "BE"},
    "BASIL"             : {"itemCd": "KE2BEXGRM00001", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "GRM", "pkg_cd": "BE"},
    "LEEKS"             : {"itemCd": "KE2BEXKGX00001", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "SUKUMA WIKI"       : {"itemCd": "KE2BEXKGX00002", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "SPINACH"           : {"itemCd": "KE2BEXKGX00003", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "LETTUCE"           : {"itemCd": "KE2BEXKGX00004", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "GARLIC LOCAL"      : {"itemCd": "KE2BEXKGX00005", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "THORN MELON"       : {"itemCd": "KE2BEXKGX00006", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "CAULIFLOWER"       : {"itemCd": "KE2BEXKGX00007", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BE"},
    "SALAD ONION"       : {"itemCd": "KE2BEXU00001",   "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "U",   "pkg_cd": "BE"},
    "BROCCOLI"          : {"itemCd": "KE2BGXKGX00003", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BG"},
    "ONIONS WHITE"      : {"itemCd": "KE2BGXKGX00004", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BG"},
    "CABBAGE RED"       : {"itemCd": "KE2BGXKGX00005", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BG"},
    "BEETROOTS"         : {"itemCd": "KE2BGXKGX00006", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BG"},
    "RED POTATOES"      : {"itemCd": "KE2BGXKGX00007", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BG"},
    "CABBAGE"           : {"itemCd": "KE2BGXU00001",   "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "U",   "pkg_cd": "BG"},
    "ONIONS RED"        : {"itemCd": "KE2BZXBEX00001", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "BE",  "pkg_cd": "BZ"},
    "POTATOES"          : {"itemCd": "KE2BZXKGX00001", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BZ"},
    "COURGETTS"         : {"itemCd": "KE2BZXKGX00002", "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "KG",  "pkg_cd": "BZ"},
    "SALAD ONION 250G"  : {"itemCd": "KE2BZXU00001",   "item_cls_cd": "5040150100", "tax_ty_cd": "D", "uom": "U",   "pkg_cd": "BZ"},

    "CELERY"            : {"itemCd": "KE2BEXBEX00005", "item_cls_cd": "5041150100", "tax_ty_cd": "D", "uom": "U",   "pkg_cd": "BZ"},
    "EGGPLANT"          : {"itemCd": "KE2BEXBEX00006", "item_cls_cd": "5041150100", "tax_ty_cd": "D", "uom": "U",   "pkg_cd": "BZ"},
    "ROSEMARY"          : {"itemCd": "KE2BEXBEX00007", "item_cls_cd": "5041150100", "tax_ty_cd": "D", "uom": "U",   "pkg_cd": "BZ"},
    # ── Fresh fruit  (cls 5030150300) ─────────────────────────────────────────
    "CAPSICUMS GREEN"   : {"itemCd": "KE1BEXKGX00006", "item_cls_cd": "5030150300", "tax_ty_cd": "D", "uom": "U",   "pkg_cd": "BZ"},

    # ── Organic produce  (cls 5041150100) ─────────────────────────────────────
    "ORGANIC BRITTANY ARTICHOKES": {"itemCd": "", "item_cls_cd": "5041150100", "tax_ty_cd": "D", "uom": "KG", "pkg_cd": "BE"},

    # ── Consultancy services  (cls 8017190900 / 8613210200) ───────────────────
    "BUSINESS AND UTILITY PROVIDER RELATIONS CONSULTATION AND ENGAGEMENT": {
        "itemCd": "", "item_cls_cd": "8017190900", "tax_ty_cd": "B", "uom": "U", "pkg_cd": "BE",
    },
    "TRAINING PLANNING AND DEVELOPMENT CONSULTANCY SERVICE": {
        "itemCd": "", "item_cls_cd": "8613210200", "tax_ty_cd": "B", "uom": "U", "pkg_cd": "BE",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Aliases
# Maps alternate / colloquial / GRN descriptions → canonical catalogue key.
# All values must exist as keys in ETIMS_ITEM_CATALOGUE above.
# ─────────────────────────────────────────────────────────────────────────────
ITEM_NAME_ALIASES: dict[str, str] = {
    # ── Vegetables & Greens ───────────────────────────────────────────────────
    "SUKUMA"            : "SUKUMA WIKI",
    "COLLARD GREENS"    : "SUKUMA WIKI",
    "KALE"              : "KALES",
    "SKUMA"             : "SUKUMA WIKI", # Common typo
    "SPINACHES"         : "SPINACH",
    "TERERE LEAVES"     : "TERERE",
    "AMARANTH"          : "TERERE",
    "AMARANTHUS"        : "TERERE",
    "MANAGU LEAVES"     : "MANAGU",
    "AFRICAN NIGHTSHADE": "MANAGU",
    "KUNDE LEAVES"      : "KUNDE",
    "COWPEAS LEAVES"    : "KUNDE",
    "COW PEA"           : "KUNDE",
    "SAGETI"            : "SAGHET",
    "SAGA"              : "SAGHET",
    "SPIDER PLANT"      : "SAGHET",
    "KAHURURA LEAVES"   : "KAHURURA",
    "PUMPKIN LEAVES"    : "KAHURURA",
    
    # ── Alliums (Onions & Garlic) ─────────────────────────────────────────────
    "SPRING ONION"      : "SALAD ONION",
    "SPRING ONIONS"     : "SALAD ONION",
    "SALAD ONIONS"      : "SALAD ONION",
    "SCALLIONS"         : "SALAD ONION",
    "WHITE ONIONS"      : "ONIONS WHITE",
    "ONION WHITE"       : "ONIONS WHITE",
    "RED ONIONS"        : "ONIONS RED",
    "ONION RED"         : "ONIONS RED",
    "BULB ONION"        : "ONIONS RED",
    "GARLIC"            : "GARLIC LOCAL",
    "KITUNGUU SAUMU"    : "GARLIC LOCAL",
    "KITUNGUU"          : "ONIONS RED",

    # ── Roots & Tubers ────────────────────────────────────────────────────────
    "POTATO"            : "POTATOES",
    "IRISH POTATOES"    : "POTATOES",
    "WARU"              : "POTATOES",
    "RED POTATO"        : "RED POTATOES",
    "BEETROOT"          : "BEETROOTS",
    "BEET"              : "BEETROOTS",

    # ── Fruits & Gourd Family ─────────────────────────────────────────────────
    "PLANTAIN"          : "MATOKE",
    "GREEN BANANA"      : "MATOKE",
    "BANANA"            : "MATOKE",
    "COOKING BANANAS"   : "MATOKE",
    "PAWPAW"            : "PAW PAW",
    "PAPAYA"            : "PAW PAW",
    "TREE TOMATOES"     : "TREE TOMATO",
    "TAMARILLO"         : "TREE TOMATO",
    "THORN MELON FRUIT" : "THORN MELON",
    "HORNED MELON"      : "THORN MELON",
    "KIWANO"            : "THORN MELON",
    "COURGETTE"         : "COURGETTS",
    "ZUCCHINI"          : "COURGETTS",
    "EGG PLANT"         : "EGGPLANT",
    "BRINJAL"           : "EGGPLANT",
    "AUBERGINE"         : "EGGPLANT",
    "CAPSICUM"          : "CAPSICUMS GREEN",
    "HOHO"              : "CAPSICUMS GREEN",
    "GREEN PELLER"      : "CAPSICUMS GREEN",

    # ── Herbs & Brassicas ─────────────────────────────────────────────────────
    "CORIANDER"         : "DANIA",
    "DHANIA"            : "DANIA",
    "CILANTRO"          : "DANIA",
    "HERB MINT"         : "MINT HERBS",
    "MINT"              : "MINT HERBS",
    "SWEET BASIL"       : "BASIL",
    "CABBAGE WHITE"     : "CABBAGE",
    "GREEN CABBAGE"     : "CABBAGE",
    "CAULI"             : "CAULIFLOWER",
    "BROCOLI"           : "BROCCOLI", # Common typo
    
    # ── Consultancy & Services ───────────────────────────────────────────────
    "STAKEHOLDER ENGAGEMENT": "BUSINESS AND UTILITY PROVIDER RELATIONS CONSULTATION AND ENGAGEMENT",
    "UTILITY CONSULTATION"  : "BUSINESS AND UTILITY PROVIDER RELATIONS CONSULTATION AND ENGAGEMENT",
    "TRAINING CONSULTANCY"  : "TRAINING PLANNING AND DEVELOPMENT CONSULTANCY SERVICE",
    "STAFF DEVELOPMENT"     : "TRAINING PLANNING AND DEVELOPMENT CONSULTANCY SERVICE",
}

# ─────────────────────────────────────────────────────────────────────────────
# Lookup helpers
# ─────────────────────────────────────────────────────────────────────────────

def _normalise(name: str) -> str:
    """Strip, upper-case, and collapse internal whitespace."""
    return re.sub(r"\s+", " ", name.strip().upper())


def resolve_item(description: str) -> ItemEntry | None:
    """
    Look up an eTIMS ItemEntry by product description.

    Resolution order
    ─────────────────
    1. Exact match against ETIMS_ITEM_CATALOGUE (after normalisation).
    2. Alias match via ITEM_NAME_ALIASES.
    3. Substring match: catalogue key is contained in the description, or
       vice-versa (catches "SUKUMA WIKI 1KG" → "SUKUMA WIKI").
    4. Returns None if nothing matches — the caller should log a warning and
       fall back to whatever codes came with the GRN item.

    Args:
        description: free-form item name from the GRN, e.g. "Sukuma wiki 1kg"

    Returns:
        ItemEntry dict or None.
    """
    if not description:
        return None

    key = _normalise(description)

    # 1. Exact match
    if key in ETIMS_ITEM_CATALOGUE:
        return ETIMS_ITEM_CATALOGUE[key]

    # 2. Alias match
    canonical = ITEM_NAME_ALIASES.get(key)
    if canonical and canonical in ETIMS_ITEM_CATALOGUE:
        return ETIMS_ITEM_CATALOGUE[canonical]

    # 3. Substring match (longest catalogue key wins to avoid false positives)
    best_key: str | None = None
    best_len = 0
    for cat_key in ETIMS_ITEM_CATALOGUE:
        if cat_key in key or key in cat_key:
            if len(cat_key) > best_len:
                best_key = cat_key
                best_len = len(cat_key)

    if best_key:
        return ETIMS_ITEM_CATALOGUE[best_key]

    return None


def resolve_item_strict(description: str) -> ItemEntry | None:
    """
    Like resolve_item() but skips the fuzzy substring step (steps 1 & 2 only).
    Use when you need high-confidence matches and would rather get None than a
    potentially wrong code.
    """
    if not description:
        return None

    key = _normalise(description)

    if key in ETIMS_ITEM_CATALOGUE:
        return ETIMS_ITEM_CATALOGUE[key]

    canonical = ITEM_NAME_ALIASES.get(key)
    if canonical and canonical in ETIMS_ITEM_CATALOGUE:
        return ETIMS_ITEM_CATALOGUE[canonical]

    return None


def list_all_items() -> list[dict]:
    """
    Return a flat list of all catalogue entries, useful for admin UIs / debugging.

    Each dict has keys: name, itemCd, item_cls_cd, tax_ty_cd, uom, pkg_cd.
    """
    return [
        {"name": name, **entry}
        for name, entry in sorted(ETIMS_ITEM_CATALOGUE.items())
    ]