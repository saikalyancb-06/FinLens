#!/usr/bin/env python
"""Wipe all data from the PostgreSQL database and create a single login.

Schema is left completely untouched — tables, columns, foreign keys, indexes,
constraints and ENUM types all survive. Only the rows go. Every table is emptied
with a single `TRUNCATE ... RESTART IDENTITY CASCADE`, which is why the foreign
keys between them are not a problem.

Master data is then re-seeded, because the application cannot function without
it: with no `banks` rows you cannot register a bank account, and with no
`categories` rows a confident classification has no category to point at.

Usage
-----
    # Show what would happen. This is the default — it changes nothing.
    python scripts/reset_database.py --email you@kredo.in

    # Do it. Prompts for the password; it is never passed on the command line.
    python scripts/reset_database.py --email you@kredo.in --confirm

    # Also create an entity and a bank account, so uploads work immediately.
    python scripts/reset_database.py --email you@kredo.in --confirm \\
        --with-account 502000123456 --bank-code HDFC

The password is read with getpass: it is not echoed, not stored in your shell
history, and not written to any log. Choose it yourself — nothing in this script
generates or defaults one.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

# The six banks the Bank Master shipped with. Codes are what `bank_code` on an
# account refers to, so they must stay stable; the ids are regenerated.
SYSTEM_BANKS = [
    ("Axis Bank", "AXIS"),
    ("Canara Bank", "CANARA"),
    ("HDFC Bank", "HDFC"),
    ("ICICI Bank", "ICICI"),
    ("Kotak Mahindra Bank", "KOTAK"),
    ("State Bank of India", "SBI"),
]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--email", required=True, help="email address for the single login")
    ap.add_argument("--full-name", default="Kredo Admin")
    ap.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    ap.add_argument("--confirm", action="store_true",
                    help="actually wipe. Without this the script only reports.")
    ap.add_argument("--with-account", metavar="ACCOUNT_NUMBER",
                    help="also create an entity and bank account (9-18 digits)")
    ap.add_argument("--bank-code", default="HDFC",
                    help="bank code for --with-account (default HDFC)")
    ap.add_argument("--entity-name", default="Default Entity")
    ap.add_argument("--keep-categories", action="store_true",
                    help="do not re-seed the category taxonomy")
    args = ap.parse_args()

    if not args.database_url:
        ap.error(
            "no target database. Set DATABASE_URL in the project's .env file, "
            "export it in this shell, or pass --database-url"
        )
    if not args.database_url.split("://", 1)[0].lower().startswith("postgres"):
        ap.error("target must be PostgreSQL")
    if args.with_account and not (args.with_account.isdigit() and 9 <= len(args.with_account) <= 18):
        ap.error("--with-account must be 9 to 18 digits (the app enforces this too)")

    os.environ["DATABASE_URL"] = args.database_url
    os.environ.setdefault("DB_AUTO_CREATE", "false")

    from app.database.session import Base  # noqa: E402
    import app.models  # noqa: F401,E402
    import app.aa.models  # noqa: F401,E402
    from app.models.user import User  # noqa: E402
    from app.models.entity import Bank, Entity  # noqa: E402
    from app.models.account import Account  # noqa: E402
    from app.utils.security import hash_password  # noqa: E402

    metadata = Base.metadata
    engine = create_engine(args.database_url, future=True)
    Session = sessionmaker(bind=engine, future=True)

    tables = [t for t in metadata.sorted_tables if t.name != "alembic_version"]

    print(f"Target: {args.database_url.split('@')[-1]}")
    print()

    # Current contents, so you can see exactly what is about to go.
    with engine.connect() as conn:
        counts = []
        total = 0
        for t in tables:
            n = conn.execute(text(f'SELECT COUNT(*) FROM "{t.name}"')).scalar()
            total += n
            if n:
                counts.append((t.name, n))

    print(f"{len(tables)} tables, {total:,} rows currently stored.")
    if counts:
        print("Non-empty tables:")
        for name, n in counts:
            print(f"  {name:<34}{n:>9,}")
    print()
    print("The schema is NOT touched: tables, columns, foreign keys, indexes,")
    print("constraints and ENUM types all remain exactly as they are.")
    print()
    print("After the wipe:")
    print(f"  - {len(SYSTEM_BANKS)} banks re-seeded (Bank Master)")
    if not args.keep_categories:
        print("  - category taxonomy re-seeded")
    print(f"  - 1 user created: {args.email}")
    if args.with_account:
        print(f"  - 1 entity ('{args.entity_name}') and 1 {args.bank_code} account "
              f"ending {args.with_account[-4:]}")

    if not args.confirm:
        print()
        print("Dry run — nothing changed. Re-run with --confirm to wipe.")
        return 0

    # Read the password only once we know we are actually proceeding.
    print()
    password = getpass.getpass(f"Password for {args.email}: ")
    confirm_pw = getpass.getpass("Repeat password: ")
    if not password:
        print("Empty password — aborted. Nothing was changed.")
        return 1
    if password != confirm_pw:
        print("Passwords do not match — aborted. Nothing was changed.")
        return 1
    if len(password) < 8:
        print("Password must be at least 8 characters — aborted. Nothing was changed.")
        return 1

    session = Session()
    try:
        # One statement, so no foreign key ordering to get wrong, and the whole
        # thing is a single transaction: it either all happens or none of it does.
        names = ", ".join(f'"{t.name}"' for t in tables)
        session.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        print()
        print(f"Truncated {len(tables)} tables.")

        for name, code in SYSTEM_BANKS:
            session.add(Bank(name=name, code=code, is_active=True))
        session.flush()
        print(f"Seeded {len(SYSTEM_BANKS)} banks.")

        if not args.keep_categories:
            from app.services.category_seeder import seed_categories
            cat_map = seed_categories(session)
            session.flush()
            print(f"Seeded category taxonomy ({len(set(cat_map.values()))} categories).")

        user = User(
            email=args.email,
            hashed_password=hash_password(password),
            full_name=args.full_name,
            is_active=True,
        )
        session.add(user)
        session.flush()
        print(f"Created user {user.email} ({user.id}).")

        if args.with_account:
            bank = session.query(Bank).filter(Bank.code == args.bank_code.upper()).first()
            if not bank:
                raise SystemExit(
                    f"Unknown bank code '{args.bank_code}'. One of: "
                    + ", ".join(c for _, c in SYSTEM_BANKS)
                )
            entity = Entity(user_id=user.id, name=args.entity_name)
            session.add(entity)
            session.flush()
            account = Account(
                user_id=user.id,
                entity_id=entity.id,
                bank_id=bank.id,
                bank_code=bank.code,
                account_number_masked=f"****{args.with_account[-4:]}",
                account_type="CURRENT",
                currency="INR",
            )
            session.add(account)
            session.flush()
            print(f"Created entity '{entity.name}' and {bank.code} account "
                  f"{account.account_number_masked}.")

        session.commit()
    except Exception:
        session.rollback()
        print("\nFAILED — rolled back. The database is unchanged.")
        raise
    finally:
        session.close()

    # Show the result rather than asserting it.
    with engine.connect() as conn:
        remaining = 0
        kept = []
        for t in tables:
            n = conn.execute(text(f'SELECT COUNT(*) FROM "{t.name}"')).scalar()
            remaining += n
            if n:
                kept.append((t.name, n))

    print()
    print(f"Done. {remaining:,} rows remain, all of it seed data:")
    for name, n in kept:
        print(f"  {name:<34}{n:>9,}")
    print()
    print(f"Log in at http://localhost:8000 with {args.email} and the password you just set.")
    if not args.with_account:
        print("Add a bank account under Bank Master before uploading a statement — "
              "/files/upload rejects uploads until one exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
