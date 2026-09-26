"""
generate_residual_fixtures.py

Creates deterministic bank statement PDFs and matching ledger CSVs for 
Test 3A (residual > 0, bank shows more) and Test 3B (residual < 0, books show more).

Accounting basis:
  - No book entries match bank entries (both are timing differences / unmatched).
  - Book opening: ₹50,000 (must be set on ImportBatch.book_opening_paise at import time).
  - Net book movement: 0 (book entries are not in the bank).
  - Book closing = ₹50,000 + 0 = ₹50,000

  Bridge items:
    + ₹3,000  (CHQ-0301: unpresented cheque, direction=ADD)
    − ₹10,000 (DEP-0302: uncleared deposit, direction=SUBTRACT)

  Bank-only items:
    + ₹9,900  (Interest Credit, direction=ADD)

  computed_bank_closing = 50,000 + 3,000 − 10,000 + 9,900 = ₹52,900

  Test 3A: actual_bank_closing = ₹65,000
    residual = 65,000 − 52,900 = +₹12,100 (> 0)
    verdict  = UNRECONCILED
    UI text  = "bank shows more than books"

  Test 3B: actual_bank_closing = ₹40,000
    residual = 40,000 − 52,900 = −₹12,900 (< 0)
    verdict  = UNRECONCILED
    UI text  = "books show more than bank"
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


def make_bank_statement_pdf(filepath: str, bank_closing: float, label: str):
    """
    Creates a bank statement PDF with a clean table that pdfplumber can parse.
    
    Rows in the bank statement:
      1. 15-Mar-2026 | Interest Credit    | Credit  | 9,900.00  | (bank_closing) 
         (this is the ONLY bank transaction; it's unmatched to any book entry)
    
    The running balance on the LAST row equals bank_closing.
    The pdfplumber table parser reads balance from the 'Balance' column.
    The reconciliation engine picks bank_closing_paise_raw = last_txn.balance_paise.
    """
    doc = SimpleDocTemplate(
        filepath,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    elements = []
    
    # Title
    title = Paragraph(f"<b>HDFC Bank — Statement of Account</b>", styles["Heading1"])
    elements.append(title)
    elements.append(Spacer(1, 0.3 * cm))
    
    subtitle = Paragraph(f"Period: 01-Mar-2026 to 31-Mar-2026 | Account: ****1234 | {label}", styles["Normal"])
    elements.append(subtitle)
    elements.append(Spacer(1, 0.5 * cm))

    # Table header + data
    headers = ["Date", "Description", "Ref No", "Debit", "Credit", "Balance"]
    
    rows = [
        headers,
        [
            "15-Mar-2026",
            "Interest Credit Mar 2026",
            "INT-MAR-2026",
            "",
            "9,900.00",
            f"{bank_closing:,.2f}",
        ],
    ]

    col_widths = [2.5 * cm, 6.5 * cm, 3.0 * cm, 2.3 * cm, 2.3 * cm, 2.8 * cm]

    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0), colors.HexColor("#1a3c5e")),
        ("TEXTCOLOR",    (0, 0), (-1, 0), colors.white),
        ("FONTNAME",     (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0), 9),
        ("ALIGN",        (0, 0), (-1, 0), "CENTER"),
        
        ("FONTNAME",     (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",     (0, 1), (-1, -1), 8),
        ("ALIGN",        (3, 1), (-1, -1), "RIGHT"),  # amounts right-aligned
        ("ALIGN",        (0, 1), (2, -1), "LEFT"),
        
        ("GRID",         (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("TOPPADDING",   (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
        ("LEFTPADDING",  (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))

    elements.append(tbl)
    elements.append(Spacer(1, 0.8 * cm))

    # Closing balance footer
    summary_data = [
        ["Closing Balance as on 31-Mar-2026", f"Rs. {bank_closing:,.2f}"],
    ]
    summary_tbl = Table(summary_data, colWidths=[10 * cm, 4 * cm])
    summary_tbl.setStyle(TableStyle([
        ("FONTNAME",    (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, -1), 9),
        ("ALIGN",       (1, 0), (1, -1), "RIGHT"),
        ("GRID",        (0, 0), (-1, -1), 0.5, colors.black),
        ("TOPPADDING",  (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    elements.append(summary_tbl)

    doc.build(elements)
    print(f"Created: {filepath}")


def make_ledger_csv(filepath: str, label: str):
    """
    Creates the ledger CSV with:
    - CHQ-0301: Cheque payment (money_out) of ₹3,000 — unpresented (not in bank)
    - DEP-0302: Deposit received (money_in) of ₹10,000 — uncleared (not in bank)
    
    IMPORTANT: These entries MUST NOT appear in the bank statement PDF.
    The bank statement only has the Interest Credit. 
    Both book entries will remain unmatched → timing differences.
    
    Also, neither book entry appears in the bank, so net_book_movement = 0.
    (Bank entry is unmatched to book → bank-only item.)
    
    User MUST set opening balance to ₹50,000 when running reconciliation.
    """
    lines = [
        "Date,Particulars,Vch Type,Vch No.,Cheque/Ref No.,Debit,Credit",
        # Cheque issued (money_out from books perspective = Debit column)
        "15-03-2026,Test Supplier Payment,Payment,PMT-0301,CHQ-0301,,3000.00",
        # Customer deposit received (money_in from books perspective = Credit column)  
        "15-03-2026,Customer Receipt,Receipt,RCT-0302,DEP-0302,10000.00,",
        # Closing balance row
        ",Closing Balance,,,,,",
    ]
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Created: {filepath}")


if __name__ == "__main__":
    # Test 3A: bank shows MORE than books
    # actual_bank_closing = ₹65,000
    # computed_bank_closing = ₹52,900  (50,000 + 3,000 - 10,000 + 9,900)
    # residual = +₹12,100
    make_bank_statement_pdf(
        os.path.join(FIXTURES_DIR, "bank_stmt_test3a_bank_high.pdf"),
        bank_closing=65000.00,
        label="Test 3A — Bank Shows More"
    )

    # Test 3B: books show MORE than bank
    # actual_bank_closing = ₹40,000
    # computed_bank_closing = ₹52,900  (50,000 + 3,000 - 10,000 + 9,900)
    # residual = -₹12,900
    make_bank_statement_pdf(
        os.path.join(FIXTURES_DIR, "bank_stmt_test3b_books_high.pdf"),
        bank_closing=40000.00,
        label="Test 3B — Books Show More"
    )

    # Single ledger (same for both tests)
    make_ledger_csv(
        os.path.join(FIXTURES_DIR, "books_ledger_test3_residual.csv"),
        label="Test 3A & 3B"
    )

    print("\n=== ACCOUNTING VERIFICATION ===")
    print(f"Book opening:            ₹50,000  (set book_opening_paise = 5000000)")
    print(f"Net book movement:       ₹0       (book entries not matched to bank)")
    print(f"Book closing:            ₹50,000")
    print(f"")
    print(f"Bridge ADD:              +₹3,000  (CHQ-0301 unpresented cheque)")
    print(f"Bridge SUBTRACT:         -₹10,000 (DEP-0302 uncleared deposit)")
    print(f"Bank-only ADD:           +₹9,900  (Interest Credit)")
    print(f"")
    print(f"computed_bank_closing:   ₹52,900")
    print(f"")
    print(f"TEST 3A actual_bank:     ₹65,000")
    print(f"TEST 3A residual:        +₹12,100 (> 0) → bank shows more than books")
    print(f"TEST 3A verdict:         UNRECONCILED")
    print(f"")
    print(f"TEST 3B actual_bank:     ₹40,000")
    print(f"TEST 3B residual:        -₹12,900 (< 0) → books show more than bank")
    print(f"TEST 3B verdict:         UNRECONCILED")
