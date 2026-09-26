import os
import uuid
import datetime
from datetime import date
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.models.user import User
from app.models.account import Account
from app.models.uploaded_file import UploadedFile
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.models.reconciliation import ImportBatch, BookEntry, ReconciliationRun
from app.services.parsing_queue import process_file_parsing_task
from app.services.books_importer import BooksImporterService
from app.services.reconciliation_engine import ReconciliationMatchingEngine

def audit_opening_balance_derivation():
    db = SessionLocal()
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()

        user = User(
            id=user_id,
            email="audit_opening_bal@example.com",
            hashed_password="secure_password",
            full_name="Opening Balance Audit User"
        )
        account = Account(
            id=account_id,
            user_id=user_id,
            bank_code="BOB",
            account_number_masked="****0108",
            account_type="CURRENT"
        )
        db.add_all([user, account])
        db.commit()

        # Parse bank statement PDF
        pdf_path = os.path.abspath("C:/Users/Saikalyan-Kredo/Desktop/PROJECT/uploads/03f25c49-9699-4a87-bbdf-a5e4546d9af6_98e101233e78421da632f20cea3f5511.pdf")
        uploaded_file = UploadedFile(
            id=uuid.uuid4(),
            user_id=user_id,
            filename="Current Account.pdf",
            file_path=pdf_path,
            file_size=os.path.getsize(pdf_path),
            mime_type="application/pdf",
            file_sha256=f"audit_pdf_sha256_{uuid.uuid4().hex}",
            status="QUEUED"
        )
        db.add(uploaded_file)
        db.commit()

        process_file_parsing_task(uploaded_file.id, pdf_path, user_id, account_id)

        # 1. Raw bank balance immediately preceding 2023-03-01 (last txn on or before 2023-02-28)
        prev_bank_txn = db.query(Transaction).filter(
            Transaction.user_id == user_id,
            Transaction.account_id == account_id,
            Transaction.txn_date < date(2023, 3, 1),
            Transaction.superseded_by_id == None,
            Transaction.balance_paise != None
        ).order_by(
            Transaction.txn_date.desc(),
            Transaction.row_index.desc().nullslast(),
            Transaction.created_at.desc()
        ).first()

        prev_balance_paise = int(prev_bank_txn.balance_paise) if (prev_bank_txn and prev_bank_txn.balance_paise is not None) else None

        # 2. Add March 2023 ledger import
        ledger_csv_content = """Date,Description,Debit,Credit
2023-03-28,NEFT-YESB30874053679-RESILIENT INNOVATIONS PVT LTD,0,17969
2023-03-27,UPI/308629700569/10:08:05/UPI/bharatpayouts@yes,0,17468
2023-03-27,UPI/308528499116/07:23:16/UPI/bharatpayouts@yes,0,23459
2023-03-25,EBANK:SELF/1345997481/COASTAL108 TO COASTAL24,127000,0
2023-03-25,UPI/308427031660/07:25:33/UPI/bharatpayouts@yes,0,19357
2023-03-24,UPI/308325685566/07:42:04/UPI/bharatpayouts@yes,0,10190
2023-03-23,UPI/308224406184/07:02:18/UPI/bharatpayouts@yes,0,14349
2023-03-22,UPI/308123184865/07:55:52/UPI/bharatpayouts@yes,0,10122
2023-03-21,IMPS/P2A/308007678047/RESILIENTINNOVA/BP151262417,0,16803
2023-03-20,IMPS/P2A/307907617300/RESILIENTINNOVA/BP149911321,0,19142
2023-03-20,IMPS/P2A/307807734249/RESILIENTINNOVA/BP148662701,0,7284
2023-03-18,IMPS/P2A/307707636305/RESILIENTINNOVA/BP147362635,0,4768
2023-03-17,IMPS/P2A/307606747038/RESILIENTINNOVA/BP145964540,0,13603
2023-03-16,UPI/307517170558/07:25:24/UPI/12club@yesbank/BP14,0,7463
2023-03-15,EBANK:SELF/1344523989/COASTAL 108 TO COASTAL 24,195000,0
2023-03-15,IMPS/P2A/307406856713/RESILIENTINNOVA/BP143138043,0,6646
2023-03-14,UPI/307314751171/06:51:50/UPI/12club@yesbank/BP14,0,47345
2023-03-13,IMPS/P2A/307206868539/RESILIENTINNOVA/BP140415284,0,14367
2023-03-13,UPI/307112251021/06:59:56/UPI/12club@yesbank/BP13,0,5300
2023-03-11,UPI/307010952618/06:05:31/UPI/12club@yesbank/BP13,0,12652
2023-03-10,UPI/306909684277/06:55:49/UPI/12club@yesbank/BP13,0,12408
2023-03-09,IMPS/P2A/306806750052/RESILIENTINNOVA/BP135176326,0,30434
2023-03-08,IMPS/P2A/306706831956/RESILIENTINNOVA/BP134062431,0,11665
2023-03-07,IMPS/P2A/306606653833/RESILIENTINNOVA/BP132838033,0,3785
2023-03-06,UPI/306504733910/06:50:33/UPI/12club@yesbank/BP13,0,21518
2023-03-04,NEFT-YESB30639736815-RESILIENT INNOVATIONS PRIVATE,0,14529
2023-03-03,UPI/306200541459/07:08:18/UPI/12club@yesbank/BP12,0,2220
2023-03-02,IMPS/P2A/306107989725/RESILIENTINNOVA/BP126158038,0,8755
2023-03-01,UPI/306097899105/07:03:47/UPI/12club@yesbank/BP12,0,6560"""

        preview = BooksImporterService.preview_file(ledger_csv_content.encode('utf-8'), "ledger_march_2023.csv")
        column_mapping = preview["detected_mapping"]
        rows = preview["sample_rows"]

        import_batch = ImportBatch(
            id=uuid.uuid4(),
            user_id=user_id,
            account_id=account_id,
            filename="ledger_march_2023.csv",
            file_sha256=f"audit_ledger_sha256_{uuid.uuid4().hex}",
            column_mapping_json=column_mapping,
            row_count=len(rows)
        )
        db.add(import_batch)
        db.flush()

        for idx, r in enumerate(rows):
            dt_str = r.get("Date")
            dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d").date()
            debit = int(float(r.get("Debit", 0)) * 100)
            credit = int(float(r.get("Credit", 0)) * 100)
            desc = r.get("Description", "")
            be = BookEntry(
                id=uuid.uuid4(),
                user_id=user_id,
                account_id=account_id,
                import_batch_id=import_batch.id,
                entry_date=dt,
                narration=desc,
                money_out_paise=debit,
                money_in_paise=credit,
                row_index=idx + 1,
                source_row_hash=f"hash_{idx+1}"
            )
            db.add(be)
        db.commit()

        # 3. Execute run for March 2023
        period_from = date(2023, 3, 1)
        period_to = date(2023, 3, 31)

        engine = ReconciliationMatchingEngine(
            db=db,
            user_id=user_id,
            account_id=account_id,
            period_from=period_from,
            period_to=period_to
        )

        run = engine.execute_run(force=True)

        print("\n" + "="*70)
        print("READ-ONLY OPENING BALANCE AUDIT REPORT")
        print("="*70)
        print(f"1. Raw bank balance preceding 2023-03-01: {prev_balance_paise} paise (Rs. {prev_balance_paise / 100 if prev_balance_paise is not None else 0:,.2f})")
        print(f"   Preceding txn date: {prev_bank_txn.txn_date if prev_bank_txn else None}, narration: {prev_bank_txn.narration_clean if prev_bank_txn else None}")
        print(f"2. opening_balance_paise stored on run (bank_opening_paise): {run.bank_opening_paise} paise (Rs. {run.bank_opening_paise / 100 if run.bank_opening_paise is not None else 0:,.2f})")
        print(f"   book_opening_paise on run: {run.book_opening_paise} paise")
        print(f"3. net_movement_paise (book in - out): {sum(b.money_in_paise - b.money_out_paise for b in db.query(BookEntry).filter(BookEntry.import_batch_id == import_batch.id).all())} paise")
        print(f"4. book_closing_paise stored on run: {run.book_closing_paise} paise (Rs. {run.book_closing_paise / 100 if run.book_closing_paise is not None else 0:,.2f})")
        print(f"5. bank_closing_paise stored on run: {run.bank_closing_paise} paise (Rs. {run.bank_closing_paise / 100 if run.bank_closing_paise is not None else 0:,.2f})")
        print(f"6. computed_bank_balance stored on run: {run.computed_bank_closing_paise} paise (Rs. {run.computed_bank_closing_paise / 100 if run.computed_bank_closing_paise is not None else 0:,.2f})")
        print(f"7. residual_paise stored on run: {run.residual_paise} paise (Rs. {run.residual_paise / 100 if run.residual_paise is not None else 0:,.2f})")
        print("="*70)

    finally:
        db.close()

if __name__ == "__main__":
    audit_opening_balance_derivation()
