"""Seed the Bank Master list of institutions.

The ``banks`` table was previously populated by hand (and, in the test suite, by
a fixture in conftest). That worked until the database was recreated, at which
point the Bank Master dropdown rendered with no options and there was no way to
get them back short of writing INSERTs — the application had no idea what a bank
was.

So the list lives here and is applied at startup. Idempotent by ``code``: an
existing row is left exactly as it is, including a name the user has edited, and
only genuinely missing institutions are inserted. Nothing is ever deleted or
renamed, because the table is referenced by ``accounts.bank_id`` and quietly
rewriting an institution under a user's account is worse than showing an
outdated name.

This list is a convenience for the account-setup dropdown. It has nothing to do
with statement discovery: mailbox scanning identifies institutions from the
documents themselves and works perfectly well for banks that never appear here.
"""
from __future__ import annotations

import logging
from typing import List, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: (code, name, SWIFT). Codes are stable identifiers — changing one orphans the
#: accounts that reference it, so add rather than edit.
DEFAULT_BANKS: List[Tuple[str, str, str]] = [
    ("HDFC", "HDFC Bank", "HDFCINBB"),
    ("ICICI", "ICICI Bank", "ICICINBB"),
    ("SBI", "State Bank of India", "SBININBB"),
    ("AXIS", "Axis Bank", "AXISINBB"),
    ("KOTAK", "Kotak Mahindra Bank", "KKBKINBB"),
    ("CANARA", "Canara Bank", "CNRBINBB"),
    ("BOB", "Bank of Baroda", "BARBINBB"),
    ("PNB", "Punjab National Bank", "PUNBINBB"),
    ("UNION", "Union Bank of India", "UBININBB"),
    ("INDUSIND", "IndusInd Bank", "INDBINBB"),
    ("IDFC", "IDFC FIRST Bank", "IDFBINBB"),
    ("YES", "YES Bank", "YESBINBB"),
    ("FEDERAL", "Federal Bank", "FDRLINBB"),
    ("IDBI", "IDBI Bank", "IBKLINBB"),
    ("RBL", "RBL Bank", "RATNINBB"),
    ("BANDHAN", "Bandhan Bank", "BDBLINBB"),
    ("AUBANK", "AU Small Finance Bank", "AUBLINBB"),
    ("INDIAN", "Indian Bank", "IDIBINBB"),
    ("CENTRAL", "Central Bank of India", "CBININBB"),
    ("IOB", "Indian Overseas Bank", "IOBAINBB"),
    ("UCO", "UCO Bank", "UCBAINBB"),
    ("BOI", "Bank of India", "BKIDINBB"),
    ("BOM", "Bank of Maharashtra", "MAHBINBB"),
    ("PSB", "Punjab & Sind Bank", "PSIBINBB"),
    ("CITI", "Citibank", "CITIINBX"),
    ("HSBC", "HSBC India", "HSBCINBB"),
    ("SCB", "Standard Chartered Bank", "SCBLINBB"),
    ("DBS", "DBS Bank India", "DBSSINBB"),
    ("DEUTSCHE", "Deutsche Bank", "DEUTINBB"),
    ("AMEX", "American Express", "AEIBINBB"),
    ("PAYTM", "Paytm Payments Bank", ""),
    ("AIRTEL", "Airtel Payments Bank", ""),
    ("FINO", "Fino Payments Bank", ""),
    ("JIO", "Jio Payments Bank", ""),
    # Catch-all so an institution that is not listed can still be recorded
    # against an account rather than blocking the user at setup.
    ("OTHER", "Other / Not listed", ""),
]


def seed_banks(db: Session) -> int:
    """Insert any missing institutions. Returns how many were added."""
    from app.models.entity import Bank

    existing = {code for (code,) in db.query(Bank.code).all()}
    added = 0
    for code, name, swift in DEFAULT_BANKS:
        if code in existing:
            continue
        db.add(Bank(name=name, code=code, swift_code=swift or None, is_active=True))
        added += 1

    if added:
        db.commit()
        logger.info("[Bank Master] seeded %s institution(s)", added)
    return added
