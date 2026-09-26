import pytest
import uuid
import datetime
from decimal import Decimal
from app.database.session import Base
from app.models import Transaction, Prediction, User
from app.models.transaction import Direction, SourceType
from tests.pgtestdb import make_isolated_engine

# A dedicated PostgreSQL database, so these model-level assertions exercise the
# same type system and constraints as production rather than SQLite's looser one.
engine, TestingSessionLocal = make_isolated_engine("models")

@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)

def test_models_creation(db):
    user_id = uuid.uuid4()

    # Create dummy user
    user = User(id=user_id, email="test@example.com", hashed_password="hashed_pass")
    db.add(user)

    # Create transaction using current schema column names
    tx = Transaction(
        user_id=user_id,
        txn_date=datetime.date.today(),
        narration_raw="Amazon Purchase",
        debit_paise="4999",        # ₹49.99 in paise
        credit_paise=None,
        balance_paise="150000",    # ₹1500.00 in paise
        direction=Direction.DEBIT,
        source_type=SourceType.STATEMENT,
    )
    db.add(tx)
    db.flush()

    # Create prediction
    pred = Prediction(
        transaction_id=tx.id,
        predicted_category="Shopping",
        confidence=0.95,
        rule_used="RULE_AMAZON_MATCH",
        model_version="v1.0.0"
    )
    db.add(pred)
    db.commit()

    # Query back & verify
    saved_tx = db.query(Transaction).filter(Transaction.id == tx.id).first()
    assert saved_tx is not None
    assert saved_tx.narration_raw == "Amazon Purchase"
    assert saved_tx.debit_paise == 4999
    assert saved_tx.direction == Direction.DEBIT


    saved_pred = saved_tx.prediction
    assert saved_pred is not None
    assert saved_pred.predicted_category == "Shopping"
    assert saved_pred.confidence == 0.95
    assert saved_pred.rule_used == "RULE_AMAZON_MATCH"
    assert saved_pred.model_version == "v1.0.0"
