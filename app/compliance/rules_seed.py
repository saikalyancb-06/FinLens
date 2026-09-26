"""Default company policy compliance rules seeded per user with realistic corporate governance limits."""
from app.models.compliance import (
    PolicyRule,
    RULE_AMOUNT_LIMIT,
    RULE_CASH_DAILY_AGGREGATE,
    RULE_CASH_PAYMENT_LIMIT,
    RULE_CASH_RECEIPT_LIMIT,
    RULE_DAILY_OUTFLOW_LIMIT,
    RULE_MIN_BALANCE,
    RULE_VELOCITY_LIMIT,
    RULE_WEEKEND_PAYMENT,
)

L = 100  # paise per rupee

DEFAULT_RULES = [
    dict(
        code="POL_APPROVED_POLICIES",
        name="Transactions follow approved company policies",
        description="Daily total outflow must remain within the authorized corporate expenditure ceiling of ₹25,00,000.",
        category="internal",
        rule_type=RULE_DAILY_OUTFLOW_LIMIT,
        threshold_paise=25_00_000 * L,
        direction_scope="debit",
        severity="medium",
    ),
    dict(
        code="POL_EXPENSE_LIMITS",
        name="Expenses comply with prescribed limits",
        description="Single expense disbursements above ₹5,00,000 require pre-approved department purchase orders.",
        category="internal",
        rule_type=RULE_AMOUNT_LIMIT,
        threshold_paise=5_00_000 * L,
        direction_scope="debit",
        severity="medium",
    ),
    dict(
        code="POL_REQUIRED_APPROVALS",
        name="Required approvals are obtained",
        description="High-value disbursements above ₹10,00,000 require documented dual approval from executive directors.",
        category="internal",
        rule_type=RULE_AMOUNT_LIMIT,
        threshold_paise=10_00_000 * L,
        direction_scope="debit",
        severity="critical",
    ),
    dict(
        code="POL_SUPPORTING_BILLS",
        name="Supporting bills and receipts are available",
        description="Major vendor payments above ₹2,00,000 must be verified against matching tax invoices and receipts.",
        category="internal",
        rule_type=RULE_AMOUNT_LIMIT,
        threshold_paise=2_00_000 * L,
        direction_scope="debit",
        severity="medium",
    ),
    dict(
        code="POL_AUTHORIZED_VENDORS",
        name="Payments are made only to authorized vendors",
        description="High-value disbursements above ₹5,00,000 must be made strictly to verified, registered corporate vendor accounts.",
        category="internal",
        rule_type=RULE_AMOUNT_LIMIT,
        threshold_paise=5_00_000 * L,
        direction_scope="debit",
        severity="high",
    ),
    dict(
        code="POL_BUSINESS_PURPOSE",
        name="Transactions have a valid business purpose",
        description="Disbursements above ₹1,00,000 must carry a clear commercial business purpose or contract reference.",
        category="internal",
        rule_type=RULE_AMOUNT_LIMIT,
        threshold_paise=1_00_000 * L,
        direction_scope="debit",
        severity="medium",
    ),
    dict(
        code="POL_NO_PERSONAL_EXPENSE",
        name="No unauthorized or personal expenses are identified",
        description="Personal retail, dining, entertainment, or subscription expenses on company bank accounts are prohibited.",
        category="internal",
        rule_type=RULE_AMOUNT_LIMIT,
        threshold_paise=1_000 * L,
        narration_filter=r"swiggy|zomato|netflix|amazon prime|hotstar|pvr|inox|spotify|playstation|steam",
        direction_scope="debit",
        severity="high",
    ),
    dict(
        code="POL_REIMBURSEMENT_GUIDELINES",
        name="Reimbursements comply with company guidelines",
        description="Staff travel and operational reimbursement claims exceeding ₹50,000 require HR/Finance policy verification.",
        category="internal",
        rule_type=RULE_AMOUNT_LIMIT,
        threshold_paise=50_000 * L,
        narration_filter=r"reimb|reimbursement|claim|travel claim|allowance",
        direction_scope="debit",
        severity="medium",
    ),
    dict(
        code="POL_PROPER_DOCUMENTATION",
        name="Transactions are properly documented",
        description="Disbursements above ₹1,00,000 must have clear narrative detail and UTR / voucher numbers.",
        category="internal",
        rule_type=RULE_AMOUNT_LIMIT,
        threshold_paise=1_00_000 * L,
        direction_scope="debit",
        severity="low",
    ),
    dict(
        code="POL_REGULATORY_CONTROLS",
        name="Regulatory and internal control requirements are followed",
        description="Mandatory Income Tax Act s.40A(3) cash limit of ₹10,000 per day and statutory compliance requirements.",
        category="statutory",
        statute_ref="Income Tax Act s.40A(3) / s.269ST",
        rule_type=RULE_CASH_PAYMENT_LIMIT,
        threshold_paise=10_000 * L,
        direction_scope="debit",
        severity="critical",
    ),
]


def seed_policy_rules(db, user_id, overwrite=False):
    """Create the default company policy rule set for a user. Returns the rules created."""
    existing = {
        r.code: r for r in db.query(PolicyRule).filter(PolicyRule.user_id == user_id).all()
    }
    created = []
    for spec in DEFAULT_RULES:
        current = existing.get(spec["code"])
        if current is not None:
            if overwrite:
                for key, value in spec.items():
                    setattr(current, key, value)
                current.is_system = True
            continue
        rule = PolicyRule(user_id=user_id, is_system=True, is_active=True, **spec)
        db.add(rule)
        created.append(rule)

    if created or overwrite:
        db.commit()
    return created
