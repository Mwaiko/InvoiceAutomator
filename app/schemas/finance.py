"""
app/schemas/finance.py

Pydantic request/response schemas for:
  Account · ExpenseCategory · Expense · AccountTransaction

Also contains read-only report shapes used by finance_service.py:
  ProfitReport · BranchProfitLine · AuditExportRow · DailyFulfillmentMetrics
"""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ═══════════════════════════════════════════════════════════════════════════════
# 1. ACCOUNT
# ═══════════════════════════════════════════════════════════════════════════════

class AccountCreate(BaseModel):
    account_name: str = Field(..., min_length=1, max_length=255)
    account_type: str = Field(..., pattern="^(bank|mpesa|cash|other)$")
    current_balance: float = Field(default=0.00, ge=0)


class AccountUpdate(BaseModel):
    """All fields optional – only supplied keys are patched."""
    account_name:    str   | None = Field(None, min_length=1, max_length=255)
    account_type:    str   | None = Field(None, pattern="^(bank|mpesa|cash|other)$")
    current_balance: float | None = None


class AccountResponse(BaseModel):
    id:              uuid.UUID
    account_name:    str
    account_type:    str
    current_balance: float
    created_at:      datetime
    updated_at:      datetime

    model_config = {"from_attributes": True}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. EXPENSE CATEGORY
# ═══════════════════════════════════════════════════════════════════════════════

class ExpenseCategoryCreate(BaseModel):
    name:        str       = Field(..., min_length=1, max_length=100)
    description: str | None = None


class ExpenseCategoryUpdate(BaseModel):
    name:        str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None


class ExpenseCategoryResponse(BaseModel):
    id:          uuid.UUID
    name:        str
    description: str | None
    created_at:  datetime
    updated_at:  datetime

    model_config = {"from_attributes": True}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. EXPENSE
# ═══════════════════════════════════════════════════════════════════════════════

class ExpenseCreate(BaseModel):
    category_id:     uuid.UUID | None = None
    business_id:     uuid.UUID | None = None
    branch_id:       uuid.UUID | None = None
    amount:          float             = Field(..., gt=0)
    description:     str               = Field(..., min_length=1)
    expense_date:    date              = Field(default_factory=date.today)
    status:          str               = Field(default="paid", pattern="^(paid|pending|cancelled)$")
    is_kra_declared: bool              = False
    receipt_path:    str | None        = None

    # ── Supply-channel tag ────────────────────────────────────────────────────
    # 'farm'       – produce grown in-house
    # 'outsourced' – produce bought from an external supplier
    # None         – non-produce expenses (wages, fuel, utilities, etc.)
    source: str | None = Field(
        default=None,
        pattern="^(farm|outsourced)$",
        description="Supply channel: 'farm', 'outsourced', or null for overhead expenses.",
    )

    # ── Optional: when paying from a specific account in one shot ─────────────
    # If provided, finance_service.record_expense_payment() will also create an
    # AccountTransaction so the account balance is updated atomically.
    account_id:     uuid.UUID | None = None
    payment_method: str | None       = None   # required if account_id is set
    reference_no:   str | None       = None


class ExpenseUpdate(BaseModel):
    category_id:     uuid.UUID | None = None
    business_id:     uuid.UUID | None = None
    branch_id:       uuid.UUID | None = None
    amount:          float   | None   = Field(None, gt=0)
    description:     str     | None   = None
    expense_date:    date    | None   = None
    status:          str     | None   = Field(None, pattern="^(paid|pending|cancelled)$")
    source:          str     | None   = Field(None, pattern="^(farm|outsourced)$")
    is_kra_declared: bool    | None   = None
    receipt_path:    str     | None   = None


class ExpenseResponse(BaseModel):
    id:              uuid.UUID
    category_id:     uuid.UUID | None
    business_id:     uuid.UUID | None
    branch_id:       uuid.UUID | None
    created_by_id:   uuid.UUID | None
    amount:          float
    description:     str
    expense_date:    date
    status:          str
    source:          str | None
    is_kra_declared: bool
    receipt_path:    str | None

    # Resolved human-readable names (populated via from_orm_expense())
    category_name:   str | None = None
    business_name:   str | None = None
    branch_name:     str | None = None
    created_by_name: str | None = None

    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_expense(cls, exp: Any) -> "ExpenseResponse":
        """
        Preferred constructor in route handlers.
        Resolves joined relationship names before returning the schema.

        Usage:
            return ExpenseResponse.from_orm_expense(expense)
        """
        schema = cls.model_validate(exp)
        if getattr(exp, "category", None):
            schema.category_name = exp.category.name
        if getattr(exp, "business", None):
            schema.business_name = exp.business.name
        if getattr(exp, "branch", None):
            schema.branch_name = exp.branch.branch_name
        if getattr(exp, "created_by", None):
            schema.created_by_name = getattr(exp.created_by, "full_name", None)
        return schema


# ── KRA audit export row (used by GET /expenses/kra-export) ───────────────────

class KRAExportRow(BaseModel):
    """One row in the KRA audit CSV / JSON export."""
    expense_id:      uuid.UUID
    expense_date:    date
    description:     str
    category:        str | None
    amount:          float
    source:          str | None
    receipt_path:    str | None
    business_name:   str | None
    branch_name:     str | None
    reference_no:    str | None   # pulled from linked AccountTransaction


# ═══════════════════════════════════════════════════════════════════════════════
# 4. ACCOUNT TRANSACTION
# ═══════════════════════════════════════════════════════════════════════════════

class AccountTransactionCreate(BaseModel):
    """
    Used when creating a standalone (freestanding) transaction, i.e. not
    generated automatically by record_expense_payment().

    Example: recording an inbound payment received from a customer.
    """
    account_id:       uuid.UUID
    transaction_type: str       = Field(..., pattern="^(INBOUND|OUTBOUND)$")
    payment_method:   str       = Field(..., min_length=1, max_length=50)
    amount:           float     = Field(..., gt=0)
    reference_no:     str | None = None
    expense_id:       uuid.UUID | None = None
    invoice_id:       uuid.UUID | None = None
    transaction_date: datetime  | None = None   # defaults to now() if omitted


class AccountTransactionResponse(BaseModel):
    id:               uuid.UUID
    account_id:       uuid.UUID | None
    expense_id:       uuid.UUID | None
    invoice_id:       uuid.UUID | None
    logged_by_id:     uuid.UUID | None
    transaction_type: str
    payment_method:   str
    amount:           float
    signed_amount:    float            # computed property on the ORM model
    reference_no:     str | None
    transaction_date: datetime

    # Resolved names
    account_name:     str | None = None
    logged_by_name:   str | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_orm_txn(cls, txn: Any) -> "AccountTransactionResponse":
        schema = cls.model_validate(txn)
        if getattr(txn, "account", None):
            schema.account_name = txn.account.account_name
        if getattr(txn, "logged_by", None):
            schema.logged_by_name = getattr(txn.logged_by, "full_name", None)
        return schema


# ═══════════════════════════════════════════════════════════════════════════════
# 5. PROFIT / ANALYTICS REPORT SHAPES (read-only)
# ═══════════════════════════════════════════════════════════════════════════════

class SourceExpenseSplit(BaseModel):
    """
    Expense breakdown by supply channel for a single branch or the whole business.
    Used inside BranchProfitLine and ProfitReport.
    """
    farm:       float = 0.0   # sum of expenses where source = 'farm'
    outsourced: float = 0.0   # sum of expenses where source = 'outsourced'
    other:      float = 0.0   # sum of expenses where source IS NULL (overheads)

    @property
    def total(self) -> float:
        return self.farm + self.outsourced + self.other


class BranchProfitLine(BaseModel):
    """
    Profit/loss breakdown for a single branch, split by supply channel.

    revenue              = sum of confirmed GRN order_totals for that branch
    farm_expenses        = expenses WHERE source = 'farm'
    outsourced_expenses  = expenses WHERE source = 'outsourced'
    other_expenses       = expenses WHERE source IS NULL (branch overheads)
    total_expenses       = farm + outsourced + other
    gross_profit         = revenue - total_expenses
    """
    branch_id:           uuid.UUID | None
    branch_name:         str
    business_name:       str | None
    revenue:             float
    farm_expenses:       float
    outsourced_expenses: float
    other_expenses:      float
    total_expenses:      float
    gross_profit:        float
    expense_count:       int

    # Legacy alias kept for backwards-compat with existing API consumers
    @property
    def expenses(self) -> float:
        return self.total_expenses


class ProfitReport(BaseModel):
    """
    Full profitability report as returned by GET /finance/profit-report.

    period_start / period_end are the filters applied.

    Expense columns are split by source so you can see the farm-vs-outsourced
    cost contribution at a glance:
      total_farm_expenses       = all paid expenses where source = 'farm'
      total_outsourced_expenses = all paid expenses where source = 'outsourced'
      total_other_expenses      = all paid expenses where source IS NULL

    general_expenses are those with branch_id IS NULL (overheads: wages, rent …),
    further split by source.

    net_profit = total_revenue - total_expenses
    """
    period_start:              date | None
    period_end:                date | None
    total_revenue:             float
    total_expenses:            float          # all paid expenses in period
    branch_expenses:           float          # expenses WHERE branch_id IS NOT NULL
    general_expenses:          float          # expenses WHERE branch_id IS NULL
    total_farm_expenses:       float          # across all branches + general
    total_outsourced_expenses: float          # across all branches + general
    total_other_expenses:      float          # non-sourced overheads
    net_profit:                float
    branches:                  list[BranchProfitLine]
    generated_at:              datetime


# ═══════════════════════════════════════════════════════════════════════════════
# 6. DAILY FULFILLMENT DASHBOARD (read-only)
# ═══════════════════════════════════════════════════════════════════════════════

class SourceFulfillmentLine(BaseModel):
    """
    Fulfillment stats for a single supply channel on a given day.

    requested  = total units ordered (OrderItem.quantity_requested)
    accepted   = total units accepted on GRNs (GRNItem.quantity_accepted)
    rejected   = total units rejected on GRNs (GRNItem.quantity_rejected)
    rate       = accepted / requested × 100  (0–100 %)
    """
    source:    str    # 'farm' | 'outsourced'
    requested: float
    accepted:  float
    rejected:  float
    rate:      float  # acceptance rate as a percentage


class DailyFulfillmentMetrics(BaseModel):
    """
    Returned by GET /finance/daily-dashboard?target_date=YYYY-MM-DD.

    farm and outsourced are the per-channel breakdowns.
    overall is the combined view across all channels.
    """
    target_date: date
    farm:        SourceFulfillmentLine
    outsourced:  SourceFulfillmentLine
    overall:     SourceFulfillmentLine