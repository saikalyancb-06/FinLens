import sys; sys.path.insert(0, '.')
from app.database.session import SessionLocal
from sqlalchemy import text

db = SessionLocal()

# Add engine_version column to reconciliation_runs table if missing
try:
    db.execute(text("ALTER TABLE reconciliation_runs ADD COLUMN engine_version VARCHAR DEFAULT 'v2.0'"))
    db.commit()
    print('Added engine_version column to reconciliation_runs.')
except Exception as e:
    print('Column engine_version exception:', e)
    db.rollback()

# Delete stale runs matching 4863414
db.execute(text("DELETE FROM reconciliation_items WHERE run_id IN (SELECT id FROM reconciliation_runs WHERE book_closing_paise = 4863414 AND bank_closing_paise = 4863414)"))
db.execute(text("DELETE FROM reconciliation_match_lines WHERE match_id IN (SELECT id FROM reconciliation_matches WHERE run_id IN (SELECT id FROM reconciliation_runs WHERE book_closing_paise = 4863414 AND bank_closing_paise = 4863414))"))
db.execute(text("DELETE FROM reconciliation_matches WHERE run_id IN (SELECT id FROM reconciliation_runs WHERE book_closing_paise = 4863414 AND bank_closing_paise = 4863414)"))
db.execute(text("DELETE FROM reconciliation_runs WHERE book_closing_paise = 4863414 AND bank_closing_paise = 4863414"))
db.commit()

print('Stale runs deleted successfully.')
db.close()
