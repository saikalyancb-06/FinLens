"""Policy rules, policy violations and anomaly findings.

Two related but distinct ideas, deliberately kept in separate tables:

* An **anomaly** is "this looks wrong or suspicious" — a balance that does not
  add up, a duplicate payment, a spike against the account's own baseline. It is
  detected, not decreed, and it can be a false positive.

* A **policy violation** is "this broke a rule that we or the law defined" — a
  cash receipt over the s.269ST ceiling, a payment above an approval limit. The
  rule is written down in `policy_rules`, so a violation is always explainable by
  pointing at the row that defines it.

Both carry a `fingerprint`, unique per user, so re-running detection updates
existing findings instead of piling up duplicates on every scan.

Rule types are plain strings rather than a PostgreSQL ENUM: new detectors get
added often, and altering an ENUM type needs a migration every time.
"""
import datetime
import uuid

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Date, ForeignKey, Index, Integer,
    JSON, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database.session import Base

# ---------------------------------------------------------------------------
# Rule types understood by app/compliance/policy_engine.py
# ---------------------------------------------------------------------------
RULE_CASH_RECEIPT_LIMIT = "cash_receipt_limit"        # s.269ST style: one credit, cash
RULE_CASH_PAYMENT_LIMIT = "cash_payment_limit"        # s.40A(3) style: one debit, cash
RULE_CASH_DAILY_AGGREGATE = "cash_daily_aggregate"    # CTR style: all cash in a day
RULE_AMOUNT_LIMIT = "amount_limit"                    # any single transaction over X
RULE_VELOCITY_LIMIT = "velocity_limit"                # more than N transactions in a window
RULE_DAILY_OUTFLOW_LIMIT = "daily_outflow_limit"      # total debits in a day over X
RULE_MIN_BALANCE = "min_balance"                      # closing balance under X
RULE_WEEKEND_PAYMENT = "weekend_payment"              # debit posted on a non-business day

RULE_TYPES = (
    RULE_CASH_RECEIPT_LIMIT,
    RULE_CASH_PAYMENT_LIMIT,
    RULE_CASH_DAILY_AGGREGATE,
    RULE_AMOUNT_LIMIT,
    RULE_VELOCITY_LIMIT,
    RULE_DAILY_OUTFLOW_LIMIT,
    RULE_MIN_BALANCE,
    RULE_WEEKEND_PAYMENT,
)

SEVERITIES = ("critical", "high", "medium", "low")
VIOLATION_STATUSES = ("open", "acknowledged", "resolved", "waived")
ANOMALY_STATUSES = ("open", "acknowledged", "resolved", "false_positive")


class PolicyRule(Base):
    """A single compliance rule, owned by a user and editable in the UI."""
    __tablename__ = "policy_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    code = Column(String(60), nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)

    # "statutory" rules quote a law; "internal" rules are the company's own.
    category = Column(String(30), nullable=False, server_default="internal")
    statute_ref = Column(String(120), nullable=True)

    rule_type = Column(String(50), nullable=False)

    # Money is integer paise everywhere in this codebase; keep that here too.
    threshold_paise = Column(BigInteger, nullable=True)
    threshold_count = Column(Integer, nullable=True)
    window_days = Column(Integer, nullable=True)

    # Optional case-insensitive regex the narration must match for the rule to
    # apply. This is what makes a rule like s.269SS/269T usable: the statute
    # covers loans and deposits, not every cash movement, and a bank statement
    # only tells them apart through the narration wording.
    narration_filter = Column(String(300), nullable=True)

    # 'debit', 'credit' or 'any'
    direction_scope = Column(String(10), nullable=False, server_default="any")
    severity = Column(String(20), nullable=False, server_default="medium")

    # A seeded statutory rule. Editable, but the UI marks it so an accidental
    # edit to a legal threshold is at least visible.
    is_system = Column(Boolean, nullable=False, server_default="false")
    is_active = Column(Boolean, nullable=False, server_default="true")

    # Result of the last scan, cached here so the dashboard can render a
    # compliance percentage with a plain SELECT. Recomputing on every GET meant
    # a read endpoint was re-evaluating every rule against every transaction and
    # writing violation rows as a side effect.
    last_applicable = Column(Integer, nullable=True)
    last_violations = Column(Integer, nullable=True)
    last_evaluated_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    violations = relationship(
        "PolicyViolation", back_populates="rule", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "code", name="uq_policy_rule_user_code"),
        Index("ix_policy_rules_user_active", "user_id", "is_active"),
    )


class PolicyViolation(Base):
    """One transaction (or one day's aggregate) breaking one rule."""
    __tablename__ = "policy_violations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    rule_id = Column(
        UUID(as_uuid=True), ForeignKey("policy_rules.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Null for aggregate violations, which are about a day rather than a row.
    transaction_id = Column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    account_id = Column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )

    occurred_on = Column(Date, nullable=True, index=True)
    amount_paise = Column(BigInteger, nullable=True)
    severity = Column(String(20), nullable=False, server_default="medium")
    status = Column(String(20), nullable=False, server_default="open", index=True)

    detail = Column(Text, nullable=True)
    evidence = Column(JSON, nullable=True)

    # Stable identity for this finding, so a re-scan updates rather than duplicates.
    fingerprint = Column(String(64), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    rule = relationship("PolicyRule", back_populates="violations")

    __table_args__ = (
        UniqueConstraint("user_id", "fingerprint", name="uq_policy_violation_fingerprint"),
        Index("ix_policy_violations_user_status", "user_id", "status"),
    )


class AnomalyFinding(Base):
    """A detected irregularity in the statement data or the money movement."""
    __tablename__ = "anomaly_findings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    account_id = Column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    statement_id = Column(
        UUID(as_uuid=True), ForeignKey("statements.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    transaction_id = Column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )

    # See app/compliance/anomaly_engine.py for the registered detector keys.
    anomaly_type = Column(String(50), nullable=False, index=True)
    severity = Column(String(20), nullable=False, server_default="medium")
    status = Column(String(20), nullable=False, server_default="open", index=True)

    title = Column(String(200), nullable=False)
    detail = Column(Text, nullable=True)
    amount_paise = Column(BigInteger, nullable=True)
    occurred_on = Column(Date, nullable=True, index=True)
    evidence = Column(JSON, nullable=True)

    fingerprint = Column(String(64), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "fingerprint", name="uq_anomaly_fingerprint"),
        Index("ix_anomaly_user_status_type", "user_id", "status", "anomaly_type"),
    )
