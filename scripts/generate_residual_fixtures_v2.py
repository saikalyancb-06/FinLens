"""
generate_residual_fixtures_v2.py

Creates DETERMINISTIC bank statement PDFs (Nov 2026 clean period) and matching
ledger CSV for Test 3A / Test 3B.

ROOT CAUSE OF PREVIOUS FAILURE
-------------------------------
The reconciliation engine simultaneously:
  (A) reads bank_closing_paise from last_bank_txn.balance_paise  (actual bank)
  (B) adds every unmatched bank transaction's amount to computed_bank_closing

If the running balance in the balance column follows naturally from the
transaction amounts (correct bank maths), (A) == (B) and residual == 0.

SOLUTION
--------
Design PDFs where the balance column on the LAST row is deliberately
different from what the transaction amounts produce.  This simulates a
genuine unexplained discrepancy without adding any transaction that
arithmetically explains the gap.

FIXTURES
--------
Period: November 2026  (NO pre-existing DB transactions for this period)

Shared bank transactions (in both PDFs):
  TX1  10-Nov-2026  NEFT-Direct Credit    CR   6,100   balance=56,100
  TX2  18-Nov-2026  Bank Charge Debit     DR   1,000   balance=55,100
  TX3  25-Nov-2026  Interest Credit       CR   9,900   balance=DELIBERATELY_WRONG

Shared book ledger entries (books_ledger_test3_nov2026.csv):
  PMT-1101  15-Nov-2026  Supplier Payment   Credit=3,000   (money_out = 3,000)
  RCT-1102  15-Nov-2026  Customer Receipt   Debit=10,000   (money_in  = 10,000)

Accounting:
  Opening balance (set by user at reconciliation run) = 50,000

  Book closing:
    money_in  = 10,000  (RCT-1102 Debit)
    money_out =  3,000  (PMT-1101 Credit)
    book_closing = 50,000 + 10,000 - 3,000 = 57,000

  Book-side bridge:
    PMT-1101 (money_out=3,000) → unpresented_cheque → ADD   +3,000
    RCT-1102 (money_in=10,000) → uncleared_deposit  → SUB  -10,000
    net_bridge = -7,000

  Bank-only items (same for both PDFs):
    TX1 NEFT   CR 6,100 → direct_credit ADD   +6,100
    TX2 Charge DR 1,000 → bank_charge   SUB   -1,000
    TX3 Intst  CR 9,900 → interest_cr   ADD   +9,900
    net_bank_only = +15,000

  computed_bank_closing = 57,000 - 7,000 + 15,000 = 65,000

  TEST 3A:  last_txn.balance_paise = 70,000
    residual = 70,000 - 65,000 = +5,000  (bank shows more)
    verdict  = UNRECONCILED

  TEST 3B:  last_txn.balance_paise = 35,000
    residual = 35,000 - 65,000 = -30,000 ... WAIT — need to redesign 3B.

Wait: for 3B, user wanted computed=40,000, actual=35,000, residual=-5,000.
computed_bank must be 40,000, not 65,000.
So bank_only items must net to -10,000 (not +15,000).

TEST 3B redesign:
  TX1  10-Nov-2026  NEFT-Direct Credit    CR   9,900   balance=59,900
  TX2  20-Nov-2026  Supplier NEFT Out     DR  19,900   balance=35,000 ← deliberately wrong

  bank_only:
    TX1 CR 9,900 → interest_cr ADD +9,900
    TX2 DR 19,900 → direct_debit SUB -19,900
    net = -10,000

  computed = 57,000 - 7,000 - 10,000 = 40,000
  actual   = 35,000
  residual = 35,000 - 40,000 = -5,000 ✓
"""

import os
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm

# The fixtures the tests read live in the repository root, not beside this
# script. Joining "fixtures" onto this file's own directory pointed at
# scripts/fixtures/, so a regeneration wrote a fresh set somewhere nothing
# reads and left the real fixtures untouched — silently, with an [OK] line
# for every file.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(PROJECT_ROOT, "fixtures")
os.makedirs(FIXTURES_DIR, exist_ok=True)

styles = getSampleStyleSheet()


def _tbl_style(header_color=None):
    hc = header_color or colors.HexColor("#1a3c5e")
    return TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), hc),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 9),
        ("ALIGN",         (0, 0), (-1, 0), "CENTER"),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 8),
        ("ALIGN",         (3, 1), (-1, -1), "RIGHT"),
        ("ALIGN",         (0, 1), (2, -1),  "LEFT"),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
    ])


def make_pdf(filepath, transactions, label, period_str):
    """
    transactions: list of (date_str, description, ref, debit_str, credit_str, balance_str)
    The LAST row's balance_str is the value the engine will use as actual_bank_closing.
    """
    doc = SimpleDocTemplate(filepath, pagesize=A4,
                            rightMargin=1.5*cm, leftMargin=1.5*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    elements = []

    elements.append(Paragraph("<b>HDFC Bank Ltd — Statement of Account</b>", styles["Heading1"]))
    elements.append(Spacer(1, 0.2*cm))
    elements.append(Paragraph(f"Period: {period_str} | A/c: ****1234 | {label}", styles["Normal"]))
    elements.append(Spacer(1, 0.5*cm))

    headers = ["Date", "Description", "Ref No", "Debit", "Credit", "Balance"]
    rows = [headers] + [list(t) for t in transactions]
    col_widths = [2.5*cm, 6.5*cm, 3.0*cm, 2.3*cm, 2.3*cm, 2.8*cm]

    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(_tbl_style())
    elements.append(tbl)
    elements.append(Spacer(1, 0.8*cm))

    # Footer closing balance — informational only; engine reads last txn balance
    last_bal = transactions[-1][5]
    summary = Table(
        [["Closing Balance as on 30-Nov-2026", last_bal]],
        colWidths=[10*cm, 4*cm]
    )
    summary.setStyle(TableStyle([
        ("FONTNAME",      (0,0), (-1,-1), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 9),
        ("ALIGN",         (1,0), (1,-1),  "RIGHT"),
        ("GRID",          (0,0), (-1,-1), 0.5, colors.black),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
    ]))
    elements.append(summary)
    doc.build(elements)
    print(f"[OK] Created: {filepath}")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 3A — actual_bank = 70,000  (bank shows MORE; residual = +5,000)
# ──────────────────────────────────────────────────────────────────────────────
# Bank transactions:
#   TX1  10-Nov-2026  NEFT-Direct Credit    CR  6,100   running-bal correctly = 56,100
#   TX2  18-Nov-2026  Bank Charge Debit     DR  1,000   running-bal correctly = 55,100
#   TX3  25-Nov-2026  Interest Credit       CR  9,900   balance = 70,000 ← DELIBERATELY WRONG
#                                                         (correct would be 65,000)
#
# Bank-only items engine will extract:
#   +6,100 (direct_credit ADD)  −1,000 (bank_charge SUB)  +9,900 (interest_credit ADD)
#   net_bank_only = +15,000
#
# computed = 57,000 − 7,000 + 15,000 = 65,000
# actual   = 70,000  ← from last_bank_txn.balance_paise
# residual = +5,000

txns_3a = [
    ("10-Nov-2026", "NEFT-Direct Credit Client", "NEFT20261110001", "",       "6,100.00",  "56,100.00"),
    ("18-Nov-2026", "Bank Charge Debit",          "CHG20261118001", "1,000.00","",          "55,100.00"),
    ("25-Nov-2026", "Int.Cr:01-11-2026 To 30-11", "INT202611",      "",       "9,900.00",  "70,000.00"),  # WRONG balance
]

make_pdf(
    os.path.join(FIXTURES_DIR, "bank_stmt_test3a_v2_bank_high.pdf"),
    txns_3a,
    "Test 3A - Bank Higher (residual +5000)",
    "01-Nov-2026 to 30-Nov-2026"
)

# ──────────────────────────────────────────────────────────────────────────────
# TEST 3B — actual_bank = 35,000  (books show MORE; residual = -5,000)
# ──────────────────────────────────────────────────────────────────────────────
# Bank transactions:
#   TX1  10-Nov-2026  Int.Cr  CR  9,900   running-bal = 59,900
#   TX2  20-Nov-2026  Supplier NEFT  DR  19,900  balance = 35,000 ← DELIBERATELY WRONG
#                                                  (correct would be 40,000)
#
# Bank-only items engine will extract:
#   +9,900 (interest_credit ADD)  −19,900 (direct_debit SUB)
#   net_bank_only = −10,000
#
# computed = 57,000 − 7,000 − 10,000 = 40,000
# actual   = 35,000  ← from last_bank_txn.balance_paise
# residual = −5,000

txns_3b = [
    ("10-Nov-2026", "Int.Cr:01-11-2026 To 30-11", "INT202611",      "",        "9,900.00",  "59,900.00"),
    ("20-Nov-2026", "NEFT-Supplier Payment Out",   "NEFT20261120002","19,900.00","",         "35,000.00"),  # WRONG balance
]

make_pdf(
    os.path.join(FIXTURES_DIR, "bank_stmt_test3b_v2_books_high.pdf"),
    txns_3b,
    "Test 3B - Books Higher (residual -5000)",
    "01-Nov-2026 to 30-Nov-2026"
)

# ──────────────────────────────────────────────────────────────────────────────
# Shared book ledger — same for both tests
# ──────────────────────────────────────────────────────────────────────────────
# Convention: Debit = money_in, Credit = money_out  (Tally DEBIT_IN)
# PMT-1101: Credit=3,000 → money_out=3,000 → unpresented_cheque → bridge ADD +3,000
# RCT-1102: Debit=10,000 → money_in=10,000 → uncleared_deposit  → bridge SUB -10,000
# book_closing = 50,000 + 10,000 − 3,000 = 57,000
# net_bridge   = +3,000 − 10,000 = −7,000

csv_path = os.path.join(FIXTURES_DIR, "books_ledger_test3_nov2026.csv")
csv_lines = [
    "Date,Particulars,Vch Type,Vch No.,Cheque/Ref No.,Debit,Credit",
  "15-11-2026,Test Supplier Payment,Payment,PMT-1101,CHQ-1101,3000.00,",
  "15-11-2026,Customer Receipt Advance,Receipt,RCT-1102,DEP-1102,,10000.00",
    ",Closing Balance,,,,,",
]
with open(csv_path, "w", encoding="utf-8") as f:
    f.write("\n".join(csv_lines) + "\n")
print(f"[OK] Created: {csv_path}")
