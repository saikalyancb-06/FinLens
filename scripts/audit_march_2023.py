import os
import uuid
import csv
import datetime
from datetime import date
from sqlalchemy.orm import Session

from app.database.session import Base, SessionLocal
from app.models.user import User
from app.models.account import Account
from app.models.uploaded_file import UploadedFile
from app.models.statement import Statement
from app.models.transaction import Transaction, Direction
from app.models.reconciliation import ImportBatch, BookEntry
from app.services.parsing_queue import process_file_parsing_task
from app.services.books_importer import BooksImporterService
from app.services.reconciliation_engine import ReconciliationMatchingEngine

def run_audit():
    db = SessionLocal()
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()

        user = User(
            id=user_id,
            email="audit_march_2023@example.com",
            hashed_password="secure_password",
            full_name="March 2023 Audit User"
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

        # 1. Bank PDF Statement path
        pdf_path = os.path.abspath("C:/Users/Saikalyan-Kredo/Desktop/PROJECT/uploads/03f25c49-9699-4a87-bbdf-a5e4546d9af6_98e101233e78421da632f20cea3f5511.pdf")
        if not os.path.exists(pdf_path):
            print("PDF file not found at", pdf_path)
            return

        # Record uploaded file metadata
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

        # Parse bank statement
        process_file_parsing_task(uploaded_file.id, pdf_path, user_id, account_id)

        total_parsed_bank_txns = db.query(Transaction).filter(
            Transaction.user_id == user_id,
            Transaction.account_id == account_id
        ).count()

        # 2. Ledger CSV Import (March 2023)
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

        # 3. Run Reconciliation for March 2023 (2023-03-01 to 2023-03-31)
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

        # In-period bank transactions query
        march_bank_txns = db.query(Transaction).filter(
            Transaction.user_id == user_id,
            Transaction.account_id == account_id,
            Transaction.txn_date >= period_from,
            Transaction.txn_date <= period_to,
            Transaction.superseded_by_id == None
        ).all()

        march_bank_debits = sum(int(t.debit_paise or 0) for t in march_bank_txns)
        march_bank_credits = sum(int(t.credit_paise or 0) for t in march_bank_txns)

        # In-period ledger transactions query
        march_ledger_entries = db.query(BookEntry).filter(
            BookEntry.user_id == user_id,
            BookEntry.account_id == account_id,
            BookEntry.entry_date >= period_from,
            BookEntry.entry_date <= period_to
        ).all()

        march_ledger_debits = sum(b.money_out_paise for b in march_ledger_entries)
        march_ledger_credits = sum(b.money_in_paise for b in march_ledger_entries)

        print("\n" + "="*70)
        print("RECONCILIATION PERIOD AUDIT REPORT (2023-03-01 to 2023-03-31)")
        print("="*70)
        print(f"1. Total parsed bank transactions = {total_parsed_bank_txns}")
        print(f"2. Number of bank transactions selected for March period = {len(march_bank_txns)}")
        print(f"3. Number of ledger transactions selected for March period = {len(march_ledger_entries)}")
        print(f"4. Sum of March bank debits = Rs. {march_bank_debits / 100:,.2f} ({march_bank_debits} paise)")
        print(f"5. Sum of March bank credits = Rs. {march_bank_credits / 100:,.2f} ({march_bank_credits} paise)")
        print(f"6. March bank opening balance = Rs. {(run.bank_opening_paise or 0) / 100:,.2f} ({run.bank_opening_paise} paise)")
        print(f"7. March bank closing balance = Rs. {(run.bank_closing_paise or 0) / 100:,.2f} ({run.bank_closing_paise} paise)")
        print(f"8. Sum of March ledger debits = Rs. {march_ledger_debits / 100:,.2f} ({march_ledger_debits} paise)")
        print(f"9. Sum of March ledger credits = Rs. {march_ledger_credits / 100:,.2f} ({march_ledger_credits} paise)")
        print(f"10. Exact matched/unmatched counts:")
        print(f"    - Matched count = {run.matched_count}")
        print(f"    - Unmatched book count = {run.unmatched_book_count}")
        print(f"    - Unmatched bank count = {run.unmatched_bank_count}")
        print(f"    - Verdict = {run.verdict}")
        print(f"    - Residual = Rs. {(run.residual_paise or 0) / 100:,.2f} ({run.residual_paise} paise)")
        print("="*70)

    finally:
        db.close()

if __name__ == "__main__":
    run_audit()
