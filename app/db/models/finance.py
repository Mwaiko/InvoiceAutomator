"""
app/db/models/finance.py

Account            – a money-holding account (Bank, M-Pesa, Cash, etc.)
ExpenseCategory    – lookup table for classifying expenses
Expense            – a recorded outgoing cost, optionally tied to a business/branch
AccountTransaction – immutable ledger entry that debits / credits an Account

Every financial movement (expense payment, invoice receipt) should create a
corresponding AccountTransaction row so the account's current_balance can be
reconciled at any time.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin


# ── 1. Account ─────────────────────────────────────────────────────────────────
class Account(UUIDMixin, TimestampMixin, Base):
    """
    Represents a real money bucket (e.g. "Equity Bank – Ops", "Safaricom M-Pesa").

    account_type choices (not enforced at DB level, validate in schema/service):
        'bank' | 'mpesa' | 'cash' | 'other'

    current_balance is updated in-place whenever an AccountTransaction is posted
    against this account (INBOUND adds, OUTBOUND subtracts).
    """

    __tablename__ = "accounts"

    # ── Identity ──────────────────────────────────────────────────────────────
    account_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    account_type: Mapped[str] = mapped_column(String(50),  nullable=False)

    # ── Balance ───────────────────────────────────────────────────────────────
    current_balance: Mapped[float] = mapped_column(
        Numeric(15, 2), default=0.00, nullable=False, server_default="0"
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    transactions: Mapped[list["AccountTransaction"]] = relationship(
        "AccountTransaction", back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Account {self.account_name} ({self.account_type}) bal={self.current_balance}>"


# ── 2. ExpenseCategory ─────────────────────────────────────────────────────────
class ExpenseCategory(UUIDMixin, TimestampMixin, Base):
    """
    Lookup / taxonomy for expenses (e.g. "Fuel", "Staff Salaries", "Utilities").
    Deleting a category is restricted while expenses reference it (ON DELETE RESTRICT).
    """

    __tablename__ = "expense_categories"

    # ── Identity ──────────────────────────────────────────────────────────────
    name:        Mapped[str]        = mapped_column(String(100), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text)

    # ── Relationships ─────────────────────────────────────────────────────────
    expenses: Mapped[list["Expense"]] = relationship(
        "Expense", back_populates="category"
    )

    def __repr__(self) -> str:
        return f"<ExpenseCategory {self.name}>"


# ── 3. Expense ─────────────────────────────────────────────────────────────────
class Expense(UUIDMixin, TimestampMixin, Base):
    """
    A single recorded cost incurred by the business.

    Optional links:
      category   → ExpenseCategory  (RESTRICT on delete – category must exist)
      business   → Business         (SET NULL on delete)
      branch     → Branch           (SET NULL on delete)
      created_by → User             (SET NULL on delete)

    KRA tracking:
      is_kra_declared – True once the expense has been declared to KRA.

    Receipt storage:
      receipt_path – relative or absolute path to the uploaded receipt file
                     (e.g. "receipts/2025/06/invoice_abc.pdf").

    Status choices (validate in schema/service):
      'paid' | 'pending' | 'cancelled'
    """

    __tablename__ = "expenses"

    # ── Foreign keys ──────────────────────────────────────────────────────────
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("expense_categories.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("branches.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Core fields ───────────────────────────────────────────────────────────
    amount:       Mapped[float]     = mapped_column(Numeric(15, 2), nullable=False)
    description:  Mapped[str]       = mapped_column(Text,           nullable=False)
    expense_date: Mapped[date]      = mapped_column(Date,           nullable=False)
    status:       Mapped[str]       = mapped_column(String(20),     nullable=False, default="paid", server_default="paid")

    # ── KRA & receipt ─────────────────────────────────────────────────────────
    is_kra_declared: Mapped[bool]       = mapped_column(Boolean,        default=False, nullable=False, server_default="false")
    receipt_path:    Mapped[str | None] = mapped_column(String(1000))

    # ── Relationships ─────────────────────────────────────────────────────────
    category: Mapped["ExpenseCategory | None"] = relationship(
        "ExpenseCategory", back_populates="expenses"
    )
    business: Mapped["Business | None"] = relationship(  # type: ignore[name-defined]
        "Business", foreign_keys=[business_id]
    )
    branch: Mapped["Branch | None"] = relationship(      # type: ignore[name-defined]
        "Branch", foreign_keys=[branch_id]
    )
    created_by: Mapped["User | None"] = relationship(    # type: ignore[name-defined]
        "User", foreign_keys=[created_by_id]
    )
    # The AccountTransaction that was posted when this expense was paid (if any)
    account_transaction: Mapped["AccountTransaction | None"] = relationship(
        "AccountTransaction",
        back_populates="expense",
        uselist=False,
    )

    def __repr__(self) -> str:
        return f"<Expense {self.description[:30]!r} amount={self.amount} status={self.status}>"


# ── 4. AccountTransaction ──────────────────────────────────────────────────────
class AccountTransaction(UUIDMixin, Base):
    """
    Immutable ledger entry that records every movement of money in or out of an Account.

    Intentionally has NO TimestampMixin – transaction_date is the authoritative
    timestamp and should never be silently auto-updated.

    transaction_type choices:
        'INBOUND'  – money arriving  (e.g. customer payment received)
        'OUTBOUND' – money leaving   (e.g. expense paid out)

    payment_method examples: 'mpesa', 'bank_transfer', 'cash', 'cheque'

    Links (all nullable – a transaction may be freestanding):
      account  → Account       (CASCADE delete: transactions die with the account)
      expense  → Expense       (SET NULL on delete)
      invoice  → ETIMSInvoice  (SET NULL on delete)  [etims_invoices table]
      logged_by → User         (SET NULL on delete)
    """

    __tablename__ = "account_transactions"

    # ── Foreign keys ──────────────────────────────────────────────────────────
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    expense_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("expenses.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("etims_invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    logged_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Core fields ───────────────────────────────────────────────────────────
    transaction_type: Mapped[str]   = mapped_column(String(20),  nullable=False)  # 'INBOUND' | 'OUTBOUND'
    payment_method:   Mapped[str]   = mapped_column(String(50),  nullable=False)
    amount:           Mapped[float] = mapped_column(Numeric(15, 2), nullable=False)
    reference_no:     Mapped[str | None] = mapped_column(String(100))
    transaction_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, server_default="now()"
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    account: Mapped["Account | None"] = relationship(
        "Account", back_populates="transactions"
    )
    expense: Mapped["Expense | None"] = relationship(
        "Expense", back_populates="account_transaction"
    )
    invoice: Mapped["EtimsInvoice | None"] = relationship(  # type: ignore[name-defined]
        "EtimsInvoice", foreign_keys=[invoice_id]
    )
    logged_by: Mapped["User | None"] = relationship(        # type: ignore[name-defined]
        "User", foreign_keys=[logged_by_id]
    )

    @property
    def signed_amount(self) -> float:
        """Positive for INBOUND, negative for OUTBOUND – useful for balance maths."""
        return float(self.amount) if self.transaction_type == "INBOUND" else -float(self.amount)

    def __repr__(self) -> str:
        return (
            f"<AccountTransaction {self.transaction_type} "
            f"{self.amount} via {self.payment_method}>"
        )