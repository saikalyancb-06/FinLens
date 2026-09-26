"""
scripts/backfill_pdf.py

Register mlmodel/Current Account.pdf as an UploadedFile row (if not already)
and run it through the full parsing pipeline.

After completion, counts are read DIRECTLY from the database (not from the
in-process summary) so you can trust the numbers.

Run:
    python scripts/backfill_pdf.py
"""
import sys, pathlib, uuid, hashlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from sqlalchemy import text
from app.database.session import SessionLocal
from app.models.uploaded_file import UploadedFile
from app.models.statement import Statement
from app.models.transaction import Transaction
from app.services.parsing_queue import process_file_parsing_task

PDF_PATH = pathlib.Path(__file__).parent.parent / "mlmodel" / "Current Account.pdf"

if not PDF_PATH.exists():
    print(f"[ERROR] PDF not found at: {PDF_PATH}")
    sys.exit(1)

db = SessionLocal()

# --- Step 0: show what's in the DB right now -----------------------------------
stmt_before = db.query(Statement).count()
txn_before  = db.query(Transaction).count()
print(f"Before backfill: statements={stmt_before}, transactions={txn_before}")

# --- Step 1: find or create an UploadedFile row for this PDF -------------------
abs_path = str(PDF_PATH.resolve())
sha256   = hashlib.sha256(PDF_PATH.read_bytes()).hexdigest()

existing_uf = db.query(UploadedFile).filter(UploadedFile.file_path == abs_path).first()
if existing_uf is None:
    # Try matching by sha256 if the path differs (e.g. relocation)
    existing_uf = db.query(UploadedFile).filter(
        UploadedFile.filename == PDF_PATH.name
    ).first()

# Resolve user_id: pick the first user in the DB
from app.models.user import User
user = db.query(User).first()
if user is None:
    print("[ERROR] No users in the database. Create a user first (e.g. run demo.py).")
    db.close()
    sys.exit(1)

if existing_uf is None:
    print(f"[INFO] No UploadedFile row found for {PDF_PATH.name}. Creating one.")
    existing_uf = UploadedFile(
        id=uuid.uuid4(),
        user_id=user.id,
        filename=PDF_PATH.name,
        file_path=abs_path,
        mime_type="application/pdf",
        status="PENDING",
    )
    db.add(existing_uf)
    db.commit()
    db.refresh(existing_uf)
    print(f"[INFO] Created UploadedFile id={existing_uf.id}")
else:
    # Update path in case it moved
    existing_uf.file_path = abs_path
    db.commit()
    print(f"[INFO] Reusing existing UploadedFile id={existing_uf.id}, status={existing_uf.status}")

user_id_val = user.id         # capture before db.close() detaches the object
uf_id_val   = existing_uf.id  # same

db.close()  # close before calling the pipeline (it opens its own session)

# --- Step 2: run the pipeline --------------------------------------------------
print(f"\n[INFO] Processing: {abs_path}")
result = process_file_parsing_task(
    file_id=uf_id_val,
    file_path=abs_path,
    user_id=user_id_val,
)
print(f"\nPipeline result:")
for k, v in result.items():
    if k != "validation_errors":
        print(f"  {k}: {v}")
if result.get("validation_errors"):
    print(f"  validation_errors ({len(result['validation_errors'])}):")
    for e in result["validation_errors"][:10]:
        safe = str(e).encode("ascii", errors="replace").decode("ascii")
        print(f"    {safe}")

# --- Step 3: verify counts DIRECTLY from DB (trust these, not the pipeline) ----
db2 = SessionLocal()
stmt_after = db2.query(Statement).count()
txn_after  = db2.query(Transaction).count()
pt_after   = db2.execute(text("SELECT COUNT(*) FROM processed_transactions")).scalar()

print(f"\n=== Database counts (direct SQL) ===")
print(f"  statements              {stmt_after:>6}   (was {stmt_before})")
print(f"  transactions            {txn_after:>6}   (was {txn_before})")
print(f"  processed_transactions  {pt_after:>6}")

# Show opening/closing balance for any new statement
new_stmts = db2.query(Statement).filter(
    Statement.original_filename == PDF_PATH.name
).all()
if new_stmts:
    print(f"\n=== Statement record(s) for {PDF_PATH.name} ===")
    for s in new_stmts:
        ob = f"Rs.{s.opening_balance_paise/100:.2f}" if s.opening_balance_paise is not None else "None"
        cb = f"Rs.{s.closing_balance_paise/100:.2f}" if s.closing_balance_paise is not None else "None"
        print(f"  id={s.id}  period={s.period_from} to {s.period_to}")
        print(f"  opening_balance={ob}  closing_balance={cb}  reconciled={s.reconciled}")

db2.close()
