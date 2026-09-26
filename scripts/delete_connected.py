# delete_connected.py
import sys
import os

# Make the project root importable
PROJECT_ROOT = r"C:\Users\Saikalyan-Kredo\Desktop\backend"
sys.path.append(PROJECT_ROOT)

from app.database.session import SessionLocal
from app.email.models import ConnectedAccount

def main():
    db = SessionLocal()
    try:
        # Delete all rows (use .delete(synchronize_session=False) for speed)
        db.query(ConnectedAccount).delete(synchronize_session=False)
        db.commit()
        print("✅ All ConnectedAccount rows cleared")
    finally:
        db.close()

if __name__ == "__main__":
    main()