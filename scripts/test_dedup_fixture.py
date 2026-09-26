import sys
import os
# Ensure project root is on sys.path (scripts/ is one level below root)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
# Also add scripts/ itself so run_fixtures_unforced can be imported as a sibling module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uuid
from datetime import datetime, date
from sqlalchemy import text
from app.database.session import SessionLocal
from app.models.user import User
from app.models.account import Account
from app.models.transaction import Transaction, Direction, SourceType
from app.models.duplicate_match import DuplicateMatch, MatchStatus
from app.services.deduplication_engine import DeduplicationEngine
from run_fixtures_unforced import clear_db, import_bank_statement

def main():
    clear_db()
    db = SessionLocal()
    user = db.query(User).filter(User.email == 'demo@kredo.in').first()
    account = db.query(Account).filter(Account.user_id == user.id).first()

    import_bank_statement('uploads/7844a803-18f2-482f-87b2-db43f0edbc8e_47a2d6308be949138b7c3d46e13e131b.csv', user.id)

    print('=== LOADING 10 SYNTHETIC EMAIL ALERTS ===')

    fixture_data = [
        ('01/01/2025', 'Rs.49603.00 credited to A/c XX0108 UPI Ref 50010053224 RESILIENT INNOVATIONS', 4960300, Direction.CREDIT, 'YESCB50010053224', '1'),
        ('02/01/2025', 'Rs.42865.00 credited to A/c XX0108 UPI Ref 50020228216 RESILIENT INNOVATIONS', 4286500, Direction.CREDIT, 'YESCB50020228216', '2'),
        ('02/01/2025', 'Rs.185000.00 credited to A/c XX0108 Ref BARBZ50020118834 COASTAL DINE', 18500000, Direction.CREDIT, 'BARBZ50020118834', '3'),
        ('05/01/2025', 'Rs.21630.00 credited to your account RESILIENT INNOVATIONS PVT LTD', 2163000, Direction.CREDIT, None, '4'),
        ('09/01/2025', 'Rs.6265.00 credited RESILIENT INNOVATIONS', 626500, Direction.CREDIT, None, '5'),
        ('03/01/2025', 'Rs.11812.00 credited to A/c XX0108 UPI Ref 99999999999 DIFFERENT PAYER', 1181200, Direction.CREDIT, 'YESCB99999999999', '6'),
        ('04/01/2025', 'Rs.31120.00 debited from A/c XX0108 to VENDOR', 3112000, Direction.DEBIT, None, '7'),
        ('06/01/2025', 'Rs.42486.50 credited to A/c XX0108 RESILIENT INNOVATIONS', 4248650, Direction.CREDIT, None, '8'),
        ('20/01/2025', 'Rs.7500.00 credited to A/c XX0108 UPI Ref 50200111111 CUSTOMER A', 750000, Direction.CREDIT, 'YESCB50200111111', '9'),
        ('20/01/2025', 'Rs.7500.00 credited to A/c XX0108 UPI Ref 50200222222 CUSTOMER B', 750000, Direction.CREDIT, 'YESCB50200222222', '10'),
    ]

    alerts = []
    for dt_str, narr, amt_paise, direction, ref_no, tag in fixture_data:
        dt = datetime.strptime(dt_str, '%d/%m/%Y').date()
        is_debit = (direction == Direction.DEBIT)
        tx = Transaction(
            id=uuid.uuid4(),
            user_id=user.id,
            account_id=account.id,
            source_type=SourceType.EMAIL_ALERT,
            direction=direction,
            debit_paise=amt_paise if is_debit else None,
            credit_paise=None if is_debit else amt_paise,
            reference_no=ref_no,
            narration_raw=narr,
            narration_clean=narr.upper(),
            txn_date=dt,
            superseded_by_id=None
        )
        db.add(tx)
        alerts.append((tag, tx))

    db.commit()
    print(f'Inserted {len(alerts)} email alerts into DB.')

    engine = DeduplicationEngine(db, user.id, account.id)
    summary = engine.run_deduplication()
    print('\nDeduplication Engine Result Summary:', summary)

    superseded_alerts_count = 0
    for tag, tx in alerts:
        db.refresh(tx)
        sup_id = str(tx.superseded_by_id) if tx.superseded_by_id else 'NOT SUPERSEDED (ACTIVE)'
        if tx.superseded_by_id:
            superseded_alerts_count += 1
        print(f'  Alert #{tag}: {sup_id}')

    stmt_superseded = db.query(Transaction).filter(
        Transaction.user_id == user.id,
        Transaction.source_type == SourceType.STATEMENT,
        Transaction.superseded_by_id != None
    ).count()

    print(f'\nStatement Rows Superseded Count: {stmt_superseded} (Expected: 0)')

    double_consumption = db.execute(text('SELECT kept_txn_id, COUNT(*) FROM duplicate_matches GROUP BY 1 HAVING COUNT(*) > 1')).fetchall()
    print(f'Double Consumption Query Result (kept_txn_id > 1): {double_consumption}')

    alert_9 = alerts[8][1]
    alert_10 = alerts[9][1]
    db.refresh(alert_9)
    db.refresh(alert_10)
    print(f'Alert #9 superseded: {alert_9.superseded_by_id} | Alert #10 superseded: {alert_10.superseded_by_id}')

    assert summary["auto_merged"] == 5, f'Expected 5 auto_merged, got {summary["auto_merged"]}'
    assert summary["pending_review"] >= 1, f'Expected >= 1 pending_review, got {summary["pending_review"]}'
    assert superseded_alerts_count == 5, f'Expected 5 alert rows superseded, got {superseded_alerts_count}'
    assert stmt_superseded == 0, f'Expected 0 statement rows superseded, got {stmt_superseded}'
    assert len(double_consumption) == 0, f'Expected empty double consumption result, got {double_consumption}'
    assert alert_9.superseded_by_id is None, 'Alert #9 should not be superseded!'
    assert alert_10.superseded_by_id is None, 'Alert #10 should not be superseded!'

    print('\nALL EXPECTED_RESULTS.MD ASSERTIONS PASSED PERFECTLY!')
    db.close()

if __name__ == '__main__':
    main()
