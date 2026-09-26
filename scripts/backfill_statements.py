import sys, pathlib
sys.path.insert(0, '.')
from app.database.session import SessionLocal
from app.models import UploadedFile
from app.services.parsing_queue import process_file_parsing_task
from app.models.statement import Statement
from app.models.transaction import Transaction

db = SessionLocal()

uploaded_files = db.query(UploadedFile).all()
print(f"Found {len(uploaded_files)} UploadedFile record(s) to process/backfill.")

for uf in uploaded_files:
    print(f"Processing File ID: {uf.id}, path: {uf.file_path}, status: {uf.status}")
    if uf.file_path and pathlib.Path(uf.file_path).exists():
        try:
            res = process_file_parsing_task(uf.id, uf.file_path, uf.user_id)
            print(f"Backfilled File ID {uf.id} -> Status: {res.get('status')}, Extracted: {res.get('total_extracted')}")
        except Exception as e:
            print(f"Failed processing file {uf.id}: {e}")

stmt_count = db.query(Statement).count()
txn_count = db.query(Transaction).count()
print(f"\nBackfill Complete!")
print(f"Total Statements in DB: {stmt_count}")
print(f"Total Transactions in DB: {txn_count}")

db.close()
