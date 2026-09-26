"""
tests/test_validator.py
Unit tests for app/parsers/validator.py

Covers:
  - Missing date / amount → fatal rejection
  - MAX_AMOUNT guard
  - Both debit+credit → self-healed
  - Balance continuity: happy path, opening-balance skip, jump detected
  - Duplicate detection
  - Confidence threshold
  - Non-fatal warnings preserved
"""

import pytest
from app.parsers.validator import TransactionValidator, ValidationResult


def _txn(**kwargs) -> dict:
    """Build a minimal valid transaction dict with overrides.

    Auto-resolves credit/debit defaults:
    - If caller explicitly sets ``debit > 0`` without ``credit``, credit defaults to 0.
    - If caller explicitly sets ``credit > 0`` without ``debit``, debit defaults to 0.
    This avoids spurious ``both_debit_credit`` validation warnings that would
    trigger self-healing and contaminate balance continuity checks.
    """
    base = {
        "date":             "2026-08-01",
        "description":      "TEST TRANSACTION",
        "debit":            0.0,
        "credit":           100.0,
        "amount":           100.0,
        "balance":          1000.0,
        "transaction_type": "credit",
        "reference_number": "",
        "raw_text":         "2026-08-01 TEST TRANSACTION 100.00 1000.00",
        "confidence":       1.0,
        "warnings":         [],
        "source_page":      1,
        "source_method":    "test",
    }
    # Auto-resolve: if debit is being explicitly set to >0 and credit is not
    # explicitly provided, zero out the default credit (and vice versa).
    if "debit" in kwargs and kwargs["debit"] > 0 and "credit" not in kwargs:
        base["credit"] = 0.0
        base["transaction_type"] = "debit"
    elif "credit" in kwargs and kwargs["credit"] > 0 and "debit" not in kwargs:
        base["debit"] = 0.0
        base["transaction_type"] = "credit"
    base.update(kwargs)
    return base



@pytest.fixture
def validator():
    return TransactionValidator()


# ─────────────────────────────────────────────────────────────────────────────
# Pass-1: Per-row validation
# ─────────────────────────────────────────────────────────────────────────────

class TestPerRowValidation:

    def test_valid_row_passes(self, validator):
        result = validator.validate([_txn()])
        assert len(result.transactions) == 1
        assert len(result.rejected) == 0

    def test_missing_date_rejected(self, validator):
        result = validator.validate([_txn(date="")])
        assert len(result.rejected) == 1
        assert any(e["error_type"] == "missing_date" for e in result.errors)

    def test_invalid_date_rejected(self, validator):
        result = validator.validate([_txn(date="32/13/2026")])
        assert len(result.rejected) == 1

    def test_missing_amount_rejected(self, validator):
        result = validator.validate([_txn(debit=0.0, credit=0.0, amount=0.0)])
        assert len(result.rejected) == 1
        assert any(e["error_type"] == "missing_amount" for e in result.errors)

    def test_garbage_large_amount_rejected(self, validator):
        """Amount above MAX_AMOUNT (₹5 crore) should fail sanitisation → reject"""
        result = validator.validate([_txn(credit=999_999_999.0, amount=999_999_999.0)])
        assert len(result.rejected) == 1
        assert any(e["field"] in ("credit", "amount") for e in result.errors)

    def test_garbage_many_digits_rejected(self, validator):
        result = validator.validate([_txn(credit=12345678901234567.0, amount=12345678901234567.0)])
        assert len(result.rejected) == 1

    def test_both_debit_and_credit_self_healed(self, validator):
        """When both are set the validator should resolve it, not reject."""
        result = validator.validate([_txn(debit=200.0, credit=100.0, amount=200.0)])
        # Not fatal — should be in transactions, not rejected
        assert len(result.transactions) == 1
        txn = result.transactions[0]
        # After self-heal, only one of debit/credit should be non-zero
        assert txn["debit"] == 0.0 or txn["credit"] == 0.0

    def test_multiline_description_cleaned(self, validator):
        result = validator.validate([_txn(description="Line1\nLine2\t  Line3")])
        txn = result.transactions[0]
        assert "\n" not in txn["description"]
        assert "\t" not in txn["description"]
        assert "  " not in txn["description"]

    def test_low_confidence_row_rejected(self, validator):
        result = validator.validate([_txn(confidence=0.1)])
        assert len(result.rejected) == 1

    def test_non_fatal_missing_description_kept(self, validator):
        result = validator.validate([_txn(description="")])
        # Missing description is a warning, not fatal
        assert len(result.transactions) == 1
        assert any(e["error_type"] == "missing_description" for e in result.errors)


# ─────────────────────────────────────────────────────────────────────────────
# Pass-2: Balance continuity
# ─────────────────────────────────────────────────────────────────────────────

class TestBalanceContinuity:

    def test_clean_sequence_no_errors(self, validator):
        txns = [
            _txn(date="2026-08-01", debit=0.0,   credit=1000.0, amount=1000.0, balance=1000.0),
            _txn(date="2026-08-02", debit=200.0,  credit=0.0,   amount=200.0,  balance=800.0, transaction_type="debit"),
            _txn(date="2026-08-03", debit=0.0,    credit=500.0, amount=500.0,  balance=1300.0),
        ]
        result = validator.validate(txns)
        balance_errors = [e for e in result.errors if e["error_type"] == "balance_discontinuity"]
        assert len(balance_errors) == 0

    def test_opening_balance_row_skipped(self, validator):
        """Row with debit=credit=0 but a balance is an opening balance — must NOT trigger continuity error."""
        txns = [
            _txn(date="2026-08-01", debit=0.0, credit=0.0, amount=0.0, balance=5000.0),  # opening
            _txn(date="2026-08-02", debit=200.0, credit=0.0, amount=200.0, balance=4800.0, transaction_type="debit"),
        ]
        result = validator.validate(txns)
        balance_errors = [e for e in result.errors if e["error_type"] == "balance_discontinuity"]
        assert len(balance_errors) == 0

    def test_balance_jump_detected(self, validator):
        txns = [
            _txn(date="2026-08-01", debit=0.0,   credit=1000.0, amount=1000.0, balance=1000.0),
            _txn(date="2026-08-02", debit=200.0,  credit=0.0,   amount=200.0,  balance=9999.0, transaction_type="debit"),
        ]
        result = validator.validate(txns)
        balance_errors = [e for e in result.errors if e["error_type"] == "balance_discontinuity"]
        assert len(balance_errors) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Pass-2: Duplicate detection
# ─────────────────────────────────────────────────────────────────────────────

class TestDuplicateDetection:

    def test_exact_duplicate_detected(self, validator):
        t = _txn()
        result = validator.validate([t, dict(t)])  # identical row twice
        dup_errors = [e for e in result.errors if e["error_type"] == "duplicate_transaction"]
        assert len(dup_errors) == 1

    def test_different_rows_no_duplicate(self, validator):
        t1 = _txn(credit=100.0, amount=100.0, balance=1000.0)
        t2 = _txn(date="2026-08-02", credit=200.0, amount=200.0, balance=1200.0)
        result = validator.validate([t1, t2])
        dup_errors = [e for e in result.errors if e["error_type"] == "duplicate_transaction"]
        assert len(dup_errors) == 0

    def test_same_amount_different_date_not_duplicate(self, validator):
        t1 = _txn(date="2026-08-01", credit=500.0, amount=500.0)
        t2 = _txn(date="2026-08-05", credit=500.0, amount=500.0)
        result = validator.validate([t1, t2])
        dup_errors = [e for e in result.errors if e["error_type"] == "duplicate_transaction"]
        assert len(dup_errors) == 0

    def test_upi_duplicate_same_date_and_time_filtered(self, validator):
        t1 = _txn(
            date="2026-08-01",
            time="14:30:00",
            description="UPI/123456789012/SWIGGY",
            debit=350.0,
            amount=350.0,
            transaction_type="debit",
            reference_number="123456789012",
        )
        t2 = _txn(
            date="2026-08-01",
            time="14:30:00",
            description="UPI/123456789012/SWIGGY",
            debit=350.0,
            amount=350.0,
            transaction_type="debit",
            reference_number="123456789012",
        )
        result = validator.validate([t1, t2])
        dup_errors = [e for e in result.errors if e["error_type"] == "duplicate_transaction"]
        assert len(dup_errors) == 1
        assert len(result.transactions) == 1
        assert len(result.rejected) == 1

    def test_upi_same_date_different_time_kept(self, validator):
        t1 = _txn(
            date="2026-08-01",
            time="14:30:00",
            description="UPI/123456789012/SWIGGY",
            debit=350.0,
            amount=350.0,
            transaction_type="debit",
            reference_number="123456789012",
        )
        t2 = _txn(
            date="2026-08-01",
            time="18:45:10",
            description="UPI/123456789012/SWIGGY",
            debit=350.0,
            amount=350.0,
            transaction_type="debit",
            reference_number="123456789012",
        )
        result = validator.validate([t1, t2])
        dup_errors = [e for e in result.errors if e["error_type"] == "duplicate_transaction"]
        assert len(dup_errors) == 0
        assert len(result.transactions) == 2
        assert len(result.rejected) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Batch / integration
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchValidation:

    def test_mixed_valid_and_invalid(self, validator):
        txns = [
            _txn(),                         # valid
            _txn(date=""),                  # invalid → rejected
            _txn(credit=500.0, amount=500.0, balance=1500.0, date="2026-08-03"),  # valid
        ]
        result = validator.validate(txns)
        assert len(result.transactions) == 2
        assert len(result.rejected) == 1

    def test_all_invalid_returns_empty(self, validator):
        txns = [
            _txn(date="", debit=0.0, credit=0.0, amount=0.0),
            _txn(date="BAD"),
        ]
        result = validator.validate(txns)
        assert len(result.transactions) == 0
        assert len(result.rejected) == 2

    def test_empty_input(self, validator):
        result = validator.validate([])
        assert result.transactions == []
        assert result.errors == []

    def test_errors_contain_field_names(self, validator):
        result = validator.validate([_txn(date="")])
        for e in result.errors:
            assert "field" in e
            assert "error_type" in e
            assert "message" in e
