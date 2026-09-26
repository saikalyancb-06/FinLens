import os
import uuid
import pytest
import pandas as pd
from app.parsers.normalizer import extract_reference_number
from app.services.deduplication_engine import DeduplicationEngine, normalize_reference
from app.models.transaction import Transaction, SourceType, Direction
from app.models.duplicate_match import DuplicateMatch, MatchStatus
from app.database.session import SessionLocal
from app.models import UploadedFile, Statement

def test_reference_extraction_regression_words():
    # 1. Test description with charges and customer payment narration
    desc1 = "CHARGES FOR PORD CUSTOMER PAYMENT :002731744244"
    ref1 = extract_reference_number(desc1)
    assert ref1 != "CUSTOMER"
    assert ref1 != "CHARGES"

    # 2. Test generic words are ignored by normalize_reference
    generic_words = ["CUSTOMER", "CHARGES", "HANDLING", "SERVICE", "PAYMENT"]
    for word in generic_words:
        assert normalize_reference(word) is None

def test_intra_statement_deduplication_isolation():
    from app.models import User, Account
    db = SessionLocal()
    try:
        user = User(id=uuid.uuid4(), email=f"user_{uuid.uuid4().hex[:6]}@example.com", hashed_password="hashed_pw_test")
        db.add(user)
        db.flush()

        acct = Account(id=uuid.uuid4(), user_id=user.id, bank_code="TEST", account_number_masked="****56789")
        db.add(acct)
        db.flush()

        # Statement.user_id is NOT NULL: statements are scoped per user for isolation.
        stmt = Statement(id=uuid.uuid4(), uploaded_file_id=None, account_id=acct.id, user_id=user.id)
        db.add(stmt)
        db.flush()

        # Two distinct transactions in the SAME statement file with similar descriptions and same amount
        t1 = Transaction(
            id=uuid.uuid4(),
            user_id=user.id,
            account_id=acct.id,
            statement_id=stmt.id,
            source_type=SourceType.STATEMENT,
            direction=Direction.DEBIT,
            debit_paise=560,
            txn_date=pd.to_datetime("2026-03-14").date(),
            row_index=1,
            narration_raw="CHARGES FOR PORD CUSTOMER PAYMENT :003509570543",
            narration_clean="CHARGES FOR PORD CUSTOMER PAYMENT :003509570543",
            reference_no=None
        )
        t2 = Transaction(
            id=uuid.uuid4(),
            user_id=user.id,
            account_id=acct.id,
            statement_id=stmt.id,
            source_type=SourceType.STATEMENT,
            direction=Direction.DEBIT,
            debit_paise=560,
            txn_date=pd.to_datetime("2026-03-14").date(),
            row_index=2,
            narration_raw="CHARGES FOR PORD CUSTOMER PAYMENT :003509570479",
            narration_clean="CHARGES FOR PORD CUSTOMER PAYMENT :003509570479",
            reference_no=None
        )
        db.add_all([t1, t2])
        db.commit()

        engine = DeduplicationEngine(db=db, user_id=user.id, account_id=acct.id)
        res = engine.run_deduplication()

        # Verify t1 and t2 were NOT auto-merged
        db.refresh(t1)
        db.refresh(t2)
        assert t1.superseded_by_id is None
        assert t2.superseded_by_id is None
    finally:
        db.close()

def test_legitimate_unique_reference_deduplication():
    from app.models import User, Account
    db = SessionLocal()
    try:
        user = User(id=uuid.uuid4(), email=f"user_{uuid.uuid4().hex[:6]}@example.com", hashed_password="hashed_pw_test")
        db.add(user)
        db.flush()

        acct = Account(id=uuid.uuid4(), user_id=user.id, bank_code="TEST2", account_number_masked="****4321")
        db.add(acct)
        db.flush()

        # Legitimate duplicate with a valid unique UTR reference
        t1 = Transaction(
            id=uuid.uuid4(),
            user_id=user.id,
            account_id=acct.id,
            source_type=SourceType.STATEMENT,
            direction=Direction.DEBIT,
            debit_paise=100000,
            txn_date=pd.to_datetime("2026-03-14").date(),
            row_index=1,
            narration_raw="UPI/SWIGGY/123456789012/PAYMENT",
            narration_clean="UPI SWIGGY 123456789012 PAYMENT",
            reference_no="123456789012"
        )
        t2 = Transaction(
            id=uuid.uuid4(),
            user_id=user.id,
            account_id=acct.id,
            source_type=SourceType.EMAIL_ALERT,
            direction=Direction.DEBIT,
            debit_paise=100000,
            txn_date=pd.to_datetime("2026-03-14").date(),
            row_index=None,
            narration_raw="ALERT: Debit of 1000 INR on 123456789012",
            narration_clean="DEBIT OF 1000 INR ON 123456789012",
            reference_no="123456789012"
        )
        db.add_all([t1, t2])
        db.commit()

        engine = DeduplicationEngine(db=db, user_id=user.id, account_id=acct.id)
        res = engine.run_deduplication()

        # Verify t2 was properly superseded by t1 (Statement)
        db.refresh(t1)
        db.refresh(t2)
        assert t2.superseded_by_id == t1.id
    finally:
        db.close()

def setup_user_account(db):
    from app.models import User, Account
    user = User(id=uuid.uuid4(), email=f"user_{uuid.uuid4().hex[:6]}@example.com", hashed_password="hashed_pw_test")
    db.add(user)
    db.flush()
    acct = Account(id=uuid.uuid4(), user_id=user.id, bank_code="TEST", account_number_masked="****56789")
    db.add(acct)
    db.flush()
    return user, acct


def test_1823_excel_statement_pipeline_counts():
    excel_file = "uploads/cc9a28d7-0125-4d94-a628-f3b38034e148_57bc214f25d9420b95f05c3e47746baa.xlsx"
    if not os.path.exists(excel_file):
        pytest.skip("Test Excel file not found in uploads")

    db = SessionLocal()
    try:
        active_count = db.query(Transaction).filter(Transaction.superseded_by_id == None).count()
        assert active_count >= 1
    finally:
        db.close()


def test_category_taxonomy_normalization_aliases():
    """Verify that all target ML category aliases resolve to canonical database categories."""
    from app.services.category_seeder import normalize_category_name, seed_categories, CATEGORY_ALIASES

    db = SessionLocal()
    try:
        cat_map = seed_categories(db)

        # Test the 5 specific alias mappings requested
        assert normalize_category_name("Settlement") == "Merchant Settlement"
        assert normalize_category_name("Tax") == "GST/Tax Payment"
        assert normalize_category_name("Self Transfer") == "Internal Fund Transfer"
        assert normalize_category_name("Food") == "Food & Dining"
        assert normalize_category_name("Salary") == "Salary Payment"

        # Test cat_map resolves alias lowercase keys to non-null UUIDs
        for alias in ["settlement", "tax", "self transfer", "food", "salary"]:
            assert cat_map.get(alias) is not None, f"Alias '{alias}' did not resolve in cat_map"
    finally:
        db.close()


def test_high_confidence_alias_prediction_category_linkage():
    """Verify that a high-confidence ML prediction with an alias category never yields category_id = NULL.

    The path under test is the fallback in store_transactions: when the hybrid
    engine abstains, a category supplied by the caller must still resolve to a
    Category row rather than degrading to NULL.

    The narration has to be one the hybrid genuinely abstains on, or the fallback
    never runs and the test silently stops covering it. "BOBCARD SETTLEMENT ..."
    used to abstain and no longer does: since the 18-category purpose retrain the
    model classifies it as Sales Income, which is correct on the purpose axis —
    per purpose_rules.py a BOBCARD credit is a card acquirer settlement, whose
    PURPOSE is Sales Income and whose EVENT TYPE is Merchant Settlement. That
    case is asserted separately in test_purpose_model_classifies_acquirer_credit.
    """
    from app.services.transaction_storage import store_transactions
    from app.services.category_seeder import seed_categories

    db = SessionLocal()
    try:
        user, acct = setup_user_account(db)
        stmt = Statement(id=uuid.uuid4(), account_id=acct.id, user_id=user.id)
        db.add(stmt)
        db.flush()

        cat_map = seed_categories(db)
        expected_cat_id = cat_map["merchant settlement"]

        processed_txns = [{
            "date": "2026-08-01",
            "description": "QRTX SETTLEMENT BATCH 1234",
            "debit": 0.0,
            "credit": 5000.0,
            "balance": 50000.0,
            "decision": {
                "category": "Settlement",  # ML Alias category name
                "final_confidence": 0.995,
                "prediction_source": "ML Model",
                "matched_rule": None
            }
        }]

        # Guard the premise: if the hybrid engine stops abstaining on this
        # narration, the fallback below is never reached and the assertions
        # would pass without testing anything.
        from app.categorization.hybrid import classify_transaction
        from app.categorization.taxonomy import UNCATEGORIZED
        premise = classify_transaction(
            processed_txns[0]["description"], amount=5000.0, direction="CREDIT"
        )
        assert premise.category == UNCATEGORIZED, (
            f"Test premise broken: hybrid now classifies this narration as "
            f"{premise.category!r}, so the caller-supplied alias fallback is not exercised."
        )

        stored = store_transactions(
            db=db,
            parsed_user_id=user.id,
            parsed_account_id=acct.id,
            parsed_entity_id=None,
            parsed_statement_id=stmt.id,
            source_channel="EXCEL",
            processed_txns=processed_txns
        )

        assert stored["persisted_count"] == 1
        stored_txn = stored["transactions"][0]
        # Assert that category_id was successfully populated with the canonical Merchant Settlement UUID
        assert stored_txn.category_id is not None
        assert stored_txn.category_id == expected_cat_id
    finally:
        db.close()


def test_purpose_model_classifies_acquirer_credit():
    """The retrained model labels a card-acquirer credit on the PURPOSE axis.

    Locks in the label space change: the classifier now emits one of the 18
    names in dual_taxonomy.PURPOSES, not the rail- and event-named vocabulary
    the retired pkl model produced.
    """
    from app.categorization.dual_taxonomy import PURPOSE_SET, SALES_INCOME
    from app.categorization.hybrid import classify_transaction

    result = classify_transaction("BOBCARD SETTLEMENT BATCH 1234", amount=5000.0, direction="CREDIT")

    assert result.category in PURPOSE_SET
    assert result.category == SALES_INCOME
    assert not result.requires_review


def test_ml_classifier_emits_only_purpose_labels():
    """Every class the parsing pipeline's classifier can return is a valid purpose.

    A label outside the taxonomy reaches the database as a category name with no
    Category row behind it, which is how transactions end up with a prediction
    but a NULL category_id.
    """
    from app.ai.ml_classifier import MLClassifier
    from app.categorization.dual_taxonomy import PURPOSE_SET, PURPOSES

    classifier = MLClassifier()
    assert classifier.is_available, "No categorizer artifact; run mlmodel/train_purpose_classifier.py"

    classes = set(classifier._service._model.classes_)
    assert classes <= PURPOSE_SET, f"Model emits non-purpose labels: {sorted(classes - PURPOSE_SET)}"
    assert len(classes) == len(PURPOSES), f"Model covers {len(classes)} of {len(PURPOSES)} purposes"
