"""Category 1 / Category 2 for the consolidated output.

Category 1 is a flat, lending-oriented label (EMI, Loan Deduction, Food
Expenses, Travel Expenses, ...). Category 2 is the extra detail when there is
one — the lender behind an EMI, the other account of an internal transfer, the
counterparty of a transfer, the employer on a salary credit — and the literal
string "No" when there is not, as the spec asks.

Why a dedicated rule layer instead of the app's general classifier alone: that
classifier was built for spend analytics and, on the real statements supplied,
called an `ACH-DR-DEUTSCHE BANK` debit an "External Transfer" and a Bajaj
Finance ECS "Transportation". For a credit decision the EMI, loan, bounce, tax
and cash lines are the ones that matter most, so they are decided here by
explicit Indian-banking narration patterns, first. The general classifier is
consulted only for the consumer-spend long tail (food, shopping, bills...) and
only when it is confident.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

NO = "No"

_LENDER_WORDS = re.compile(
    r"(BANK|FINANCE|FINSERV|FINCORP|CAPITAL|CREDIT|LOANS?|HOUSING|HFC|NBFC|LEASING|"
    r"PRIME|MOTOR|FIN\b|FINANCIAL|LENDING|MICROFIN|HOME\s*FIN)", re.I)
# Banks lend too, but on a CREDIT their name is usually just the remitter's
# bank ('.../ALSTOM TRANSPORT/STANDARD CHARTERED B/'), so they only count as a
# lender on mandate debits.
_LENDING_BANKS = ["DEUTSCHE", "STAN CHART", "STANDARD CHARTERED", "IDFC FIRST", "HSBC", "CITI"]
_KNOWN_LENDERS = [
    "BAJAJ FIN", "BAJAJ FINANCE", "BAJAJ FINSERV", "TATA CAPITAL", "ADITYA BIRLA", "ADITYA BIRLA CAPITAL",
    "KOTAK PRIME", "KOTAK MAHINDRA PRIME",
    "HDB FIN", "HDFC LTD", "LIC HOUSING", "MAHINDRA FIN", "SHRIRAM", "CHOLAMANDALAM", "MUTHOOT",
    "MANAPPURAM", "IIFL", "L&T FIN", "L AND T FIN", "FULLERTON", "PIRAMAL", "POONAWALLA",
    "HERO FINCORP", "TVS CREDIT", "IDFC FIRST", "CAPRI", "AAVAS", "INDIABULLS", "SMFG", "CLIX",
    "LENDINGKART", "NEOGROWTH", "UGRO", "RACPC", "RASMECC", "SME LOAN", "ABFL", "ABCL", "TCFSL",
]


def _squash(s: str) -> str:
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def _lender_in(u: str, include_banks: bool) -> Optional[str]:
    """A lender named in the narration, matched without spaces ('BAJAJFINSERV')."""
    sq = _squash(u)
    names = _KNOWN_LENDERS + (_LENDING_BANKS if include_banks else [])
    for name in sorted(names, key=len, reverse=True):      # most specific name wins
        if _squash(name) in sq:
            return name
    return None

_RAIL_TOKENS = {
    "UPI", "NEFT", "IMPS", "RTGS", "IFT", "INB", "EMB", "ACH", "ECS", "NACH", "DR", "CR", "D", "C",
    "P2A", "P2P", "MB", "IB", "TPARTY", "TRANSFER", "TRF", "TFR", "DEP", "WDL", "BY", "TO", "FROM",
    "PAYMENT", "PAY", "SENT", "USIN", "USING", "PAID", "VIA", "RECEIVED", "REF", "EB", "NBSM", "BIL",
    "ONL", "MMT", "BILLPAY", "REV", "FT", "INF", "INFT", "CMS", "RETURN", "RTN", "SAK", "CASH",
    "WDL", "SELF", "CHQ", "CLG", "TRANSFE", "TP",
}
_BANK_CODES = {
    "SBI", "SBIN", "IOB", "IOBA", "HDFC", "ICIC", "ICICI", "AXIS", "UTIB", "BAR", "BARB", "BOB", "CNR",
    "CNRB", "KKBK", "KKB", "KOTAK", "YES", "YESB", "IBK", "IDFB", "PKG", "IPO", "FDR", "UBI", "UTI",
    "KAR", "RAT", "PUNB", "PNB", "IDIB", "CBI", "UCO", "INDB", "FED", "SIB", "KVB", "BANK", "LIMITED",
    "OF", "INDIA", "STATE", "PAYTM", "PTYS", "YBL", "IBL", "OKAXIS", "OKSBI", "OKHDFCBANK", "OKICICI",
}


def _u(s: str) -> str:
    return (s or "").upper()


def counterparty(narration: str) -> Optional[str]:
    """Best-effort name of the other party, from the common Indian narration shapes."""
    n = narration or ""
    u = _u(n)
    # UPI VPA-only shapes (Bank of Baroda): take the handle before '@'
    m = re.search(r"UPI/\d+/\d{2}:\d{2}:\d{2}/UPI/([A-Za-z0-9.\-_]+)@", n)
    if m:
        vpa = m.group(1)
        return None if re.fullmatch(r"[\d\-]+", vpa) else vpa
    parts = [p.strip() for p in re.split(r"[/]|(?<=\w)-(?=[A-Z])|\s-\s", n) if p.strip()]
    for p in parts:
        pu = _u(p)
        words = [w for w in re.split(r"\s+", pu) if w]
        if not words:
            continue
        if all(w in _RAIL_TOKENS or w in _BANK_CODES for w in words):
            continue
        if re.search(r"\d{5,}", pu) and not re.search(r"[A-Z]{3,}\s+[A-Z]{2,}", pu):
            continue      # reference numbers, account numbers, UTRs
        if re.fullmatch(r"[A-Z]{1,2}\d*", pu) or re.fullmatch(r"[A-Z]{1,3}\d+", pu) or re.fullmatch(r"X+\d+", pu):
            continue
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", pu):
            continue
        cleaned = re.sub(r"\s+", " ", p).strip(" -:.,()")
        if len(re.sub(r"[^A-Za-z]", "", cleaned)) >= 3:
            return cleaned.title() if cleaned.isupper() else cleaned
    return None


def _lender(narration: str) -> Optional[str]:
    name = _lender_in(_u(narration), include_banks=True)
    if name:
        return name.title()
    return counterparty(narration)


def _has(u: str, *pats: str) -> bool:
    return any(re.search(p, u) for p in pats)


def categorize(narration: str, direction: str, amount_paise: int) -> Tuple[str, str]:
    """(category_1, category_2) for one row. `direction` is CREDIT or DEBIT."""
    u = _u(narration)
    debit = direction == "DEBIT"
    cp = counterparty(narration)

    # --- bounces and returns: the single most important credit signal
    bounce = _has(u, r"(ACH|ECS|NACH|CHQ|CHEQUE|CLG|\bSI\b|MANDATE)\W.{0,25}(RETURN|RTN|BOUNCE|DISHONOU?R|UNPAID|REJECT)",
                  r"(RETURN|RTN|BOUNCE|DISHONOU?R|UNPAID|REJECT)\W.{0,25}(ACH|ECS|NACH|CHQ|CHEQUE|CLG|MANDATE)",
                  r"OW\s*RTN|INW\s*RTN|I/W\s*RTN|O/W\s*RTN|\bDHR\b|INSUFF")
    if bounce:
        if _has(u, r"CHG|CHRG|CHARGE|FEE|PENAL"):
            return "Bounce Charges", NO
        return ("Cheque/EMI Bounce" if debit else "Cheque/EMI Bounce Reversal"), cp or NO
    if not debit and _has(u, r"(NEFT|RTGS|IMPS|UPI)\W{0,3}(RETURN|RTN|REV)", r"REVERSAL", r"\bREVERSED\b"):
        return "Payment Returned", cp or NO

    # --- loans
    if debit and _has(u, r"LOAN\s*RECOVERY", r"LOAN\s*REC\b", r"\bLN\s*REC", r"LOAN\s*INST",
                      r"INT\.?\s*COLL", r"INTEREST\s*COLL", r"\bOD\s*INT", r"\bCC\s*INT",
                      r"TO\s*LOAN", r"LOAN\s*A/?C", r"RECOVERY\s*FOR"):
        acct = re.search(r"(\d{9,18})", narration or "")
        if _has(u, r"INT\.?\s*COLL", r"INTEREST\s*COLL", r"\bOD\s*INT", r"\bCC\s*INT"):
            return "Loan Deduction", ("Interest on OD/CC a/c " + acct.group(1)) if acct else "Interest on OD/CC"
        return "Loan Deduction", ("Loan a/c " + acct.group(1)) if acct else NO
    if debit and _has(u, r"\bACH\b", r"\bNACH\b", r"\bECS\b", r"ACH-DR", r"ACH/D", r"\bSI[-/ ]",
                      r"STANDING\s*INSTR", r"\bEMI\b", r"MANDATE"):
        if _has(u, r"MUTUAL|\bMF\b|\bSIP\b|\bAMC\b|ZERODHA|GROWW|CAMS|KFIN"):
            return "Investment", cp or NO
        if _has(u, r"\bLIC\b|INSURANCE|POLICY|PREMIUM|PMSBY|PMJJBY"):
            return "Insurance", cp or NO
        return "EMI", _lender(narration) or NO
    if debit and _lender_in(u, include_banks=False):
        # A payment to a lender that is not a mandate debit: a part-payment,
        # prepayment or manual EMI.
        return "Loan Repayment", _lender(narration) or NO
    if not debit:
        lender = _lender_in(_u(cp or ""), include_banks=False) or (
            _lender_in(u, include_banks=False) if _has(u, r"DISB|LOAN\s*A/?C|SANCTION") else None)
        if lender or _has(u, r"LOAN\s*DISB|DISBURSE"):
            return "Loan Received", (lender.title() if lender else cp or NO)

    # --- salary
    if _has(u, r"SALARY|\bSAL\b|PAYROLL|\bSAL\s*CR|SALARIES"):
        return ("Salary Paid" if debit else "Salary Received"), cp if cp and "SALARY" not in _u(cp) else NO

    # --- tax and statutory
    if not debit and _has(u, r"ITD\s*REFUND|TAX\s*REFUND|CBDT|INCOME\s*TAX\s*REFUND|GST\s*REFUND"):
        return "Tax Refund", "GST" if "GST" in u else "Income Tax"
    if debit and _has(u, r"\bGST\b|GSTN|GST\s*COLLECTION|\bCBDT\b|TIN\s*2\.0|INCOME\s*TAX|ADVANCE\s*TAX|\bTDS\b"):
        detail = "GST" if "GST" in u else "Income Tax" if _has(u, r"CBDT|INCOME TAX|TIN 2.0|ADVANCE TAX") else "TDS"
        return "Tax Payment", detail
    if _has(u, r"\bESIC\b|\bEPFO?\b|PROVIDENT\s*FUND|KHAJANE|PROFESSIONAL\s*TAX|\bPT\b"):
        detail = "ESIC" if "ESI" in u else "EPF" if "EPF" in u or "PROVIDENT" in u else "Professional Tax"
        return "Statutory Payment", detail

    # --- cash
    if debit and _has(u, r"\bATM\b|CWDR|CASH\s*WDL|CASH\s*WITHDRAW|^YOURSELF|^SELF\b|\bSELF\s*CHQ|NWD|ATW"):
        return "Cash Withdrawal", NO
    if not debit and _has(u, r"-BNA-|\bBNA\b|\bCDM\b"):
        return "Cash Deposit", "Cash deposit machine"
    if not debit and _has(u, r"BY\s*CASH|CASH\s*DEP|CSH\s*DEP|CASH\s*DEPOSIT"):
        name = re.sub(r"^(BY\s*CASH|CASH\s*DEP\w*)\s*", "", narration.strip(), flags=re.I).strip()
        return "Cash Deposit", name.title() if name else NO

    # --- interest, charges
    if not debit and _has(u, r"INT\.?\s*PD|INTEREST\s*PAID|INT\s*CREDIT|SB\s*INT|CREDIT\s*INTEREST|INT\.PD"):
        return "Interest Received", NO
    if _has(u, r"CREDIT\s*CARD|CREDITCARD|\bCC\s*PAYMENT|CARD\s*PAYMENT|CARD\s*BILL"):
        return ("Credit Card Payment" if debit else "Credit Card Refund"), NO
    if _has(u, r"\bLIC\b|INSURANCE|PMSBY|PMJJBY|POLICY|PREMIUM|SURAKSHA"):
        return "Insurance", cp or NO
    if debit and _has(u, r"CHARGES|\bCHGS?\b|CHRGS?|SMS\s*CHARGE|\bAMC\b|MIN\s*BAL|AVG\s*BAL|NON\s*MAINT|"
                         r"ANNUAL\s*FEE|PROCESSING\s*FEE|SERVICE\s*CHARGE|CONSOLIDATED\s*CHG|DEBIT\s*CARD|"
                         r"CARDFEE|DCARD|\bFEE\b"):
        return "Bank Charges", NO
    if _has(u, r"MUTUAL\s*FUND|\bMF\b|\bSIP\b|ZERODHA|GROWW|UPSTOX|CLEARING\s*CORP|\bNSE\b|\bBSE\b|"
               r"FIXED\s*DEPOSIT|\bFD\b|AUTOSWEEP|SWEEP"):
        return "Investment", cp or NO

    # --- consumer spend (merchant names)
    if _has(u, r"SWIGGY|ZOMATO|RESTAURANT|\bHOTEL\b|\bCAFE|BAKERY|BAKERS|\bFOOD|DOMINO|\bKFC\b|MCDONALD|"
               r"PIZZA|CATERING|CATERER|BIGBASKET|BLINKIT|ZEPTO|DMART|GROCER|SUPERMARKET|\bFRESH\b|"
               r"\bMILK|DAIRY|NANDINI|KITCHEN|BIRYANI|SWEETS|DARSHINI|CHAI|TEA\b"):
        return "Food Expenses", cp or NO
    if _has(u, r"IRCTC|\bUBER|\bOLA\b|RAPIDO|REDBUS|MAKEMYTRIP|GOIBIBO|INDIGO|AIR\s*INDIA|AKASA|"
               r"FASTAG|\bTOLL|PETROL|\bFUEL|HPCL|BPCL|IOCL|INDIAN\s*OIL|\bSHELL\b|\bMETRO\b|KSRTC|"
               r"TRAVEL|YATRA|CLEARTRIP|\bBUS\b|RAILWAY|PARKING|FILLING\s*STATION"):
        detail = "Fuel" if _has(u, r"PETROL|FUEL|HPCL|BPCL|IOCL|INDIAN OIL|SHELL|FILLING") else (cp or NO)
        return "Travel Expenses", detail
    if _has(u, r"ELECTRICITY|BESCOM|CESC|MESCOM|HESCOM|GESCOM|CHESCOM|\bWATER\b|BWSSB|AIRTEL|\bJIO\b|"
               r"VODAFONE|\bVI\b|BSNL|BROADBAND|\bDTH\b|TATA\s*PLAY|\bGAS\b|RECHARGE|INDANE|BHARATGAS|HP\s*GAS"):
        return "Utility Bills", cp or NO
    if _has(u, r"HOSPITAL|PHARMA|MEDICAL|MEDICALS|CLINIC|APOLLO|DIAGNOS|\bLAB\b|MULTISPECIA|HEALTH|"
               r"MEDPLUS|NETMEDS|1MG|DENTAL|NURSING"):
        return "Medical Expenses", cp or NO
    if _has(u, r"\bPOS\b|POS-|AMAZON|FLIPKART|MYNTRA|AJIO|CROMA|RELIANCE\s*DIGITAL|ZUDIO|TRENDS|\bMALL\b|"
               r"MEESHO|NYKAA|LIFESTYLE|DECATHLON|\bMART\b|\bSTORES?\b|JEWEL|FOOTWEAR"):
        return "Shopping", cp or NO
    if _has(u, r"SCHOOL|COLLEGE|UNIVERSITY|TUITION|ACADEMY|EDUCATION|BYJU|\bFEES\b|VIDYA"):
        return "Education Expenses", cp or NO
    if _has(u, r"\bRENT\b|\bRENTAL\b"):
        return ("Rent Paid" if debit else "Rent Received"), cp or NO

    # --- cheques
    if _has(u, r"\bCLG\b|CLEARING|\bCHQ\b|CHEQUE|\bCTS\b|INW\s*CLG|OW\s*CLG|^TO\s*CLG"):
        return ("Cheque Payment" if debit else "Cheque Deposit"), cp or NO

    # --- business counterparties
    if cp and _has(_u(cp), r"\b(LTD|LIMI\w*|PVT|PRIVATE|ENTERPRISES?|TRADERS?|AGENC(Y|IES)|INDUSTRIES|"
                           r"CORPORATION|CORP|CO\b|COMPANY|ASSOCIATES|SOLUTIONS|SERVICES|ELECTRICALS|"
                           r"INFRA|INFOTECH|TECHNOLOGIES|SYSTEMS|SUPPLIERS|DISTRIBUTORS|MFG|LLP)\b"):
        return ("Vendor Payment" if debit else "Business Receipt"), cp

    # --- plain transfers
    if _has(u, r"UPI|IMPS|NEFT|RTGS|\bIFT\b|\bINB\b|TRANSFER|\bTRF\b|\bTFR\b|FT\b"):
        return ("Transfer Out" if debit else "Transfer In"), cp or NO

    # --- the general classifier, for anything still unplaced
    try:
        from app.categorization.deep import classify_deep
        r = classify_deep(narration, direction=direction, amount=amount_paise / 100.0)
        if r.path and r.confidence >= 0.6:
            return _from_tree(r.path, debit), (r.merchant or r.counterparty or cp or NO)
    except Exception:  # noqa: BLE001 - the rules above already answered the important rows
        pass
    return ("Other Debit" if debit else "Other Credit"), cp or NO


_TREE_MAP = {
    "Food & Dining": "Food Expenses", "Travel": "Travel Expenses", "Transportation": "Travel Expenses",
    "Shopping": "Shopping", "Bills & Utilities": "Utility Bills", "Healthcare": "Medical Expenses",
    "Education": "Education Expenses", "Entertainment": "Entertainment", "Insurance": "Insurance",
    "Investments": "Investment", "Taxes & Government": "Tax Payment", "Financial": "Bank Charges",
    "Fees & Charges": "Bank Charges", "Donations & Charity": "Donation", "Personal & Family": "Family Transfer",
    "Refunds & Reversals": "Refund / Reversal", "Housing": "Housing Expenses",
}


def _from_tree(path, debit: bool) -> str:
    root = path[0]
    leaf = path[1] if len(path) > 1 else ""
    if root == "Loans & Credit":
        return {"EMI": "EMI", "Loan Repayment": "Loan Deduction", "Loan Disbursement": "Loan Received"}.get(
            leaf, "Credit Card Payment" if leaf == "Credit Card" else "Loan Deduction")
    if root == "Income":
        return {"Salary": "Salary Received", "Interest Income": "Interest Received",
                "Rental Income": "Rent Received", "Business Revenue": "Business Receipt",
                "Refund": "Refund / Reversal"}.get(leaf, "Other Credit")
    if root == "Cash":
        return "Cash Withdrawal" if debit else "Cash Deposit"
    if root == "Business & Professional":
        if leaf == "Salaries & Wages":
            return "Salary Paid"
        return "Vendor Payment" if debit else "Business Receipt"
    if root == "Transfers":
        return "Transfer Out" if debit else "Transfer In"
    return _TREE_MAP.get(root, "Other Debit" if debit else "Other Credit")
