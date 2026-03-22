"""
app/services/business_resolver.py

Resolves (or creates) a Business + Branch from the store/supplier
block that comes out of the GRN extractor.

Extracted GRN store shape:
    {
      "store": {
          "company_name": "Naivas Limited",
          "store_name":   "SAFARI CENTER NAIVASHA",
          "location":     "SAFARI CENTER NAIVASHA",
          "address":      null
      },
      "supplier": {
          "company_name": "QUALITY OUTSOURCE SOLUTION",
          "email": "..."
      },
      ...
    }

Rules:
  • Business is keyed on normalised company_name  (case-insensitive, stripped)
  • Branch   is keyed on normalised store_name + business_id
  • If either doesn't exist it is CREATED automatically
  • On every call the branch.location / address are kept up-to-date
    (non-destructive: we only set fields that are currently NULL)
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.business import Branch, Business

logger = get_logger(__name__)


def _norm(value: str | None) -> str:
    """Normalise a name for comparison: strip + upper."""
    return (value or "").strip().upper()


async def resolve_business_and_branch(
    db: AsyncSession,
    extracted_data: dict,
) -> tuple[Business | None, Branch | None]:
    """
    Given the raw extracted dict from the GRN extractor, find-or-create
    the matching Business and Branch rows.

    Returns (business, branch). Either may be None if the extracted data
    doesn't contain recognisable store information.
    """

    store    = extracted_data.get("store")    or {}
    supplier = extracted_data.get("supplier") or {}

    # ── Determine company name ─────────────────────────────────────────────
    # Primary source: store.company_name  (the buying company, e.g. "Naivas Limited")
    # Fallback: supplier.company_name     (our company — use only if store is absent)
    company_name: str | None = store.get("company_name") or supplier.get("company_name")
    store_name:   str | None = store.get("store_name") or store.get("location")

    if not company_name:
        logger.warning("business_resolver: no company_name in extracted_data — skipping")
        return None, None

    # ── Find or create Business ────────────────────────────────────────────
    normed_company = _norm(company_name)

    result = await db.execute(
        select(Business).where(
            # Case-insensitive exact match on stored name
            Business.name.ilike(company_name.strip())
        )
    )
    business = result.scalar_one_or_none()

    if business is None:
        business = Business(name=company_name.strip())
        db.add(business)
        await db.flush()   # get business.id
        logger.info("business_resolver: created new Business '%s' id=%s", business.name, business.id)
    else:
        logger.info("business_resolver: matched existing Business '%s' id=%s", business.name, business.id)

    # ── Find or create Branch ──────────────────────────────────────────────
    if not store_name:
        logger.warning("business_resolver: no store_name — returning business only, no branch")
        return business, None

    result = await db.execute(
        select(Branch).where(
            Branch.business_id == business.id,
            Branch.branch_name.ilike(store_name.strip()),
        )
    )
    branch = result.scalar_one_or_none()

    if branch is None:
        branch = Branch(
            business_id=business.id,
            branch_name=store_name.strip(),
            location=store.get("location"),
            address=store.get("address"),
        )
        db.add(branch)
        await db.flush()
        logger.info(
            "business_resolver: created new Branch '%s' id=%s for Business '%s'",
            branch.branch_name, branch.id, business.name,
        )
    else:
        # Non-destructively update location/address if we now have the data
        if branch.location is None and store.get("location"):
            branch.location = store["location"]
        if branch.address is None and store.get("address"):
            branch.address = store["address"]
        logger.info(
            "business_resolver: matched existing Branch '%s' id=%s",
            branch.branch_name, branch.id,
        )

    return business, branch


async def post_confirmation_update_balances(
    db: AsyncSession,
    business: Business,
    branch: Branch,
    order_total: float,
) -> None:
    """
    Called after a GRN is confirmed.
    Increments total_invoiced on both the Business and the Branch
    so outstanding_balance stays accurate.
    """
    business.total_invoiced = float(business.total_invoiced or 0) + order_total
    branch.total_invoiced   = float(branch.total_invoiced   or 0) + order_total
    # No flush/commit here — caller owns the transaction
    logger.info(
        "balance update: business='%s' branch='%s' +%.2f",
        business.name, branch.branch_name, order_total,
    )