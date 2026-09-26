#!/usr/bin/env python
"""Generate a realistic foreign-currency transaction corpus for the purpose model.

Why this exists
---------------
The deployed purpose classifier was trained on a domestic corpus whose own
metadata carries this warning:

    "Trained on synthetic data whose 180,000 rows collapse to 278 narration
     templates. The honest generalisation estimate is macro F1 0.7488."

That corpus contains no cross-border payments, so a SWIFT outward remittance for
an import invoice currently has to be classified by a model that has never seen
the words SWIFT, IBAN, FIRC or a purpose code. This module produces the missing
half.

Labelling decision
------------------
Foreign payments are labelled with the SAME 18 purposes as domestic ones. That
follows the rule already written into dual_taxonomy.py: the payment rail is not
the purpose. A wire transfer for an import invoice is Cost of Goods, exactly as
an NEFT for a domestic invoice is; the fact that it crossed a border is carried
by the currency and event fields, not by inventing a "Foreign Remittance"
category that would split the P&L in two.

Template diversity
------------------
The generator is built around distinct *templates*, not distinct rows. Multiplying
rows off a handful of phrasings is what produced the 278-template ceiling in the
original corpus and the mediocre generalisation score that came with it. Here,
narration shape, bank dialect, beneficiary, currency, purpose code and optional
fragments are drawn independently, so the template count scales with the product
of those choices rather than with the row count.

Usage
-----
    python mlmodel/generate_fx_dataset.py --rows 40000 --out mlmodel/fx_dataset.csv
    python mlmodel/generate_fx_dataset.py --report        # template stats only
"""
from __future__ import annotations

import argparse
import csv
import inspect
import os
import random
import re
import sys
from collections import Counter
from typing import Callable, Dict, List, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.categorization.dual_taxonomy import (  # noqa: E402
    BANK_FEES, COST_OF_GOODS, FINANCE_COST, INTERNAL_MOVEMENT, LOANS,
    OTHER_INCOME, OTHER_PURPOSE, OWNER_FUNDING, PROFESSIONAL_FEES,
    RENT_PREMISES, SALARY_WAGES, SALES_INCOME, TAXES_STATUTORY, TRAVEL,
    UTILITIES,
)

# ---------------------------------------------------------------------------
# Currencies. Rates are indicative mid-market levels used only to make the
# amounts in the corpus plausible; nothing downstream depends on their accuracy.
# ---------------------------------------------------------------------------
CURRENCIES: List[Tuple[str, float]] = [
    ("USD", 88.0), ("EUR", 95.5), ("GBP", 112.0), ("AED", 24.0),
    ("SGD", 65.0), ("JPY", 0.58), ("AUD", 57.5), ("CAD", 63.0),
    ("CHF", 101.0), ("HKD", 11.3), ("SAR", 23.5), ("SEK", 8.4),
]
CURRENCY_WEIGHTS = [38, 16, 11, 7, 6, 4, 4, 3, 3, 3, 3, 2]

# RBI purpose codes that genuinely appear on Indian outward remittance advices.
PURPOSE_CODES = {
    COST_OF_GOODS:     ["P0103", "P0104", "P0107", "P0004"],
    PROFESSIONAL_FEES: ["P0802", "P0803", "P0805", "P1006"],
    UTILITIES:         ["P0807", "P1403"],
    SALES_INCOME:      ["P0101", "P0102"],
    TRAVEL:            ["P0301", "P0304"],
    SALARY_WAGES:      ["P1301", "P1302"],
    LOANS:             ["P0501", "P0502"],
    FINANCE_COST:      ["P1405"],
    OTHER_INCOME:      ["P1401"],
    OWNER_FUNDING:     ["P0006"],
    RENT_PREMISES:     ["P1005"],
}

# A purpose code is a *declared* reason, so it has to agree with the words next
# to it. P0101/P0102 are goods exports; P0801/P0802 are hardware and software
# services. Emitting "PURP CODE P0801 MERCHANDISE EXPORT" would teach the model
# that a services code predicts a goods narration, which is backwards.
DESC_CODE_OVERRIDES = [
    (re.compile(r"SOFTWARE"), ["P0802"]),
    (re.compile(r"SERVICE"), ["P0801", "P0802"]),
]

# Own-account movement and bank charges carry no RBI purpose code at all: an
# EEFC-to-INR sweep is not a remittance to anyone. Rather than invent one, these
# purposes are routed only through dialects that never print a code, so no
# spurious code-to-class association is learned.
CODELESS_PURPOSES = {INTERNAL_MOVEMENT}


def pick_code(rng: random.Random, purpose: str, desc: str) -> str:
    for pattern, codes in DESC_CODE_OVERRIDES:
        if pattern.search(desc) and purpose == SALES_INCOME:
            return rng.choice(codes)
    return rng.choice(PURPOSE_CODES.get(purpose, ["P1099"]))

# ---------------------------------------------------------------------------
# Beneficiaries, grouped so the counterparty is consistent with the purpose.
# A model that learns "MULLER GMBH -> Cost of Goods" is learning something real;
# random pairings would teach it nothing but noise.
# ---------------------------------------------------------------------------
BENEFICIARIES: Dict[str, List[str]] = {
    COST_OF_GOODS: [
        "SHENZHEN HUAYI ELECTRONICS CO LTD", "MULLER MASCHINEN GMBH",
        "NIPPON STEEL TRADING", "PT SINAR MAS AGRO", "AL FUTTAIM TRADING LLC",
        "GUANGZHOU TEXTILE IMP EXP", "VIETNAM CASHEW PROCESSING JSC",
        "KOREA POLYMER IND CO", "SIAM CHEMICALS PCL", "TAIWAN PRECISION TOOLS",
        "ROTTERDAM COMMODITIES BV", "ANTWERP DIAMOND SUPPLY NV",
    ],
    PROFESSIONAL_FEES: [
        "BAKER TILLY LLP", "CLIFFORD ADVISORY LTD", "KPMG SINGAPORE PTE",
        "DELOITTE TOUCHE DUBAI", "MARSH INSURANCE BROKERS", "IPWATCH IP ATTORNEYS",
        "NORTON PATENT SERVICES", "GLOBAL TAX PARTNERS LLC",
        "MCKINSEY COMPANY UK", "ERNST YOUNG MENA",
    ],
    UTILITIES: [
        "AMAZON WEB SERVICES INC", "GOOGLE CLOUD EMEA LTD",
        "MICROSOFT AZURE IRELAND", "ATLASSIAN PTY LTD", "SALESFORCE COM INC",
        "DIGITALOCEAN LLC", "CLOUDFLARE INC", "SLACK TECHNOLOGIES",
        "ADOBE SYSTEMS IRELAND", "ORACLE NETSUITE INC",
    ],
    SALES_INCOME: [
        "WALMART GLOBAL SOURCING", "TESCO STORES PLC", "CARREFOUR SA",
        "TARGET SOURCING SERVICES", "METRO CASH CARRY GMBH",
        "COSTCO WHOLESALE CORP", "LULU HYPERMARKET LLC",
        "WOOLWORTHS GROUP LTD", "ALDI EINKAUF GMBH", "SEVEN ELEVEN JAPAN",
    ],
    SALARY_WAGES: [
        "R KRISHNAN", "A MENON", "S IYER", "D FERNANDES", "M QURESHI",
        "J WILLIAMS", "T NAKAMURA", "L SCHMIDT",
    ],
    TRAVEL: [
        "EMIRATES AIRLINE", "SINGAPORE AIRLINES LTD", "LUFTHANSA AG",
        "BOOKING COM BV", "MARRIOTT INTERNATIONAL", "QATAR AIRWAYS QCSC",
        "AGODA COMPANY PTE LTD", "HILTON WORLDWIDE",
    ],
    RENT_PREMISES: [
        "JEBEL ALI FREE ZONE AUTH", "REGUS SINGAPORE PTE",
        "WEWORK LONDON LTD", "DMCC BUSINESS CENTRE", "SPACES AMSTERDAM BV",
    ],
    LOANS: [
        "STANDARD CHARTERED BANK SG", "HSBC BANK PLC LONDON",
        "MUFG BANK LTD", "DBS BANK SINGAPORE", "EMIRATES NBD PJSC",
    ],
    OWNER_FUNDING: [
        "KREDO HOLDINGS PTE LTD", "APEX GLOBAL VENTURES LLC",
        "NORDIC CAPITAL PARTNERS AB", "SEQUOIA INDIA HOLDINGS",
    ],
    OTHER_INCOME: [
        "KREDO USA INC", "SUBSIDIARY HOLDINGS BV", "APEX SINGAPORE PTE",
    ],
    INTERNAL_MOVEMENT: [
        "SELF EEFC ACCOUNT", "OWN ACCOUNT DBS SG", "KREDO EEFC USD AC",
    ],
}

# ---------------------------------------------------------------------------
# Scenarios: (purpose, direction, beneficiary pool, descriptor fragments)
# `direction` is 'out' (debit) or 'in' (credit).
# ---------------------------------------------------------------------------
Scenario = Tuple[str, str, str, List[str]]

SCENARIOS: List[Scenario] = [
    (COST_OF_GOODS, "out", COST_OF_GOODS,
     ["IMPORT PAYMENT", "IMPORT ADVANCE", "GOODS IMPORT", "INV SETTLEMENT",
      "RAW MATERIAL IMPORT", "MERCHANT TRADE", "BILL OF ENTRY SETTLEMENT",
      "ADVANCE AGAINST PROFORMA", "COMPONENT PURCHASE"]),
    (PROFESSIONAL_FEES, "out", PROFESSIONAL_FEES,
     ["PROFESSIONAL FEES", "CONSULTANCY FEES", "AUDIT FEES", "LEGAL FEES",
      "ADVISORY RETAINER", "IP FILING FEES", "TRADEMARK RENEWAL",
      "TAX ADVISORY", "DUE DILIGENCE FEES"]),
    (UTILITIES, "out", UTILITIES,
     ["CLOUD SUBSCRIPTION", "SAAS SUBSCRIPTION", "SOFTWARE LICENCE RENEWAL",
      "ANNUAL SUBSCRIPTION", "HOSTING CHARGES", "SEAT LICENCE RENEWAL",
      "PLATFORM USAGE"]),
    (SALES_INCOME, "in", SALES_INCOME,
     ["EXPORT PROCEEDS", "EXPORT REALISATION", "INV REALISATION",
      "SALE PROCEEDS", "SHIPMENT PROCEEDS", "MERCHANDISE EXPORT",
      "SOFTWARE EXPORT PROCEEDS", "SERVICE EXPORT RECEIPT"]),
    (SALARY_WAGES, "out", SALARY_WAGES,
     ["SALARY REMITTANCE", "EXPAT SALARY", "OVERSEAS PAYROLL",
      "CONTRACTOR PAYOUT", "DEPUTATION ALLOWANCE"]),
    (TRAVEL, "out", TRAVEL,
     ["BUSINESS TRAVEL", "AIR TICKET", "HOTEL BOOKING", "CONFERENCE TRAVEL",
      "TRAVEL BOOKING", "ACCOMMODATION"]),
    (RENT_PREMISES, "out", RENT_PREMISES,
     ["OFFICE RENT", "FREEZONE LEASE", "COWORKING LEASE", "PREMISES RENT",
      "FACILITY LEASE"]),
    (LOANS, "out", LOANS,
     ["ECB PRINCIPAL REPAYMENT", "FOREIGN LOAN REPAYMENT",
      "TERM LOAN INSTALMENT", "BUYERS CREDIT REPAYMENT",
      "TRADE CREDIT SETTLEMENT"]),
    (LOANS, "in", LOANS,
     ["ECB DRAWDOWN", "FOREIGN LOAN DISBURSEMENT", "BUYERS CREDIT AVAILED",
      "TRADE CREDIT DRAWDOWN"]),
    (FINANCE_COST, "out", LOANS,
     ["INTEREST ON ECB", "INTEREST ON BUYERS CREDIT", "LC INTEREST",
      "TRADE CREDIT INTEREST", "INTEREST PAYMENT"]),
    (OWNER_FUNDING, "in", OWNER_FUNDING,
     ["EQUITY INFUSION", "FDI SHARE CAPITAL", "CAPITAL CONTRIBUTION",
      "SHARE SUBSCRIPTION MONEY", "FDI INFLOW"]),
    (OTHER_INCOME, "in", OTHER_INCOME,
     ["DIVIDEND RECEIVED", "ROYALTY RECEIPT", "INTERCO DIVIDEND",
      "MANAGEMENT FEE RECEIPT"]),
    (INTERNAL_MOVEMENT, "out", INTERNAL_MOVEMENT,
     ["TRANSFER TO EEFC", "OWN ACCOUNT TRANSFER", "SELF TRANSFER",
      "EEFC CONVERSION"]),
    (INTERNAL_MOVEMENT, "in", INTERNAL_MOVEMENT,
     ["EEFC TO INR CONVERSION", "TRANSFER FROM OWN ACCOUNT",
      "SELF FUNDING TRANSFER"]),
]

# Bank charges and taxes are narration shapes of their own: they carry no
# beneficiary and no purpose code, and they must not be learned as imports.
CHARGE_SCENARIOS = [
    (BANK_FEES, ["SWIFT CHARGES", "CORRESPONDENT BANK CHARGES", "WIRE TRANSFER FEE",
                 "OUTWARD REMITTANCE COMMISSION", "FX CONVERSION CHARGES",
                 "CABLE CHARGES", "NOSTRO CHARGES", "BENE BANK CHARGES",
                 "FIRC ISSUANCE CHARGES", "A2 PROCESSING FEE",
                 "CROSS BORDER HANDLING FEE", "INWARD REMITTANCE COMMISSION"]),
    (TAXES_STATUTORY, ["TCS ON LRS REMITTANCE", "GST ON FX CONVERSION",
                       "GST ON REMITTANCE COMMISSION", "TDS ON FOREIGN PAYMENT",
                       "EQUALISATION LEVY", "WITHHOLDING TAX ON REMITTANCE",
                       "TCS COLLECTED U/S 206C"]),
]

# ---------------------------------------------------------------------------
# Narration dialects. Indian banks format cross-border advices differently, and
# a model that has only seen one bank's layout fails on the next one.
# ---------------------------------------------------------------------------

def _ref(rng: random.Random, prefix: str, n: int = 10) -> str:
    return prefix + "".join(rng.choice("0123456789") for _ in range(n))


def _amount(rng: random.Random, code: str, rate: float) -> str:
    inr_target = rng.choice([50_000, 2_00_000, 8_00_000, 25_00_000, 75_00_000])
    raw = max(1.0, inr_target / rate) * rng.uniform(0.5, 1.8)
    return f"{raw:,.2f}"


DIALECTS: List[Callable[..., str]] = []


def dialect(fn):
    fn.needs_code = "pcode" in inspect.signature(fn).parameters and _prints_code(fn)
    DIALECTS.append(fn)
    return fn


def _prints_code(fn) -> bool:
    """True when the dialect actually renders pcode into its output string."""
    src = inspect.getsource(fn)
    body = src.split("return", 1)[-1]
    return "{pcode}" in body


@dialect
def _hdfc(rng, *, desc, bene, code, rate, pcode, direction, **_):
    tag = "OUTWARD" if direction == "out" else "INWARD"
    return (f"FCY {tag} REMIT/{_ref(rng,'HDFCN')}/{bene}/{code} "
            f"{_amount(rng, code, rate)}/{desc}")


@dialect
def _icici(rng, *, desc, bene, code, rate, pcode, direction, **_):
    verb = "OUT" if direction == "out" else "IN"
    return (f"WIRE TRF {verb}-{_ref(rng,'ICIC')}-{bene}-{desc}-{code} "
            f"{_amount(rng, code, rate)}")


@dialect
def _axis(rng, *, desc, bene, code, rate, pcode, direction, **_):
    body = f"OUTWARD TT {code} {_amount(rng, code, rate)}" if direction == "out" \
        else f"INWARD TT {code} {_amount(rng, code, rate)}"
    return f"{body} @ {rng.uniform(rate*0.97, rate*1.03):.2f} {desc} REF {_ref(rng,'ORM')}"


@dialect
def _sbi(rng, *, desc, bene, code, rate, pcode, direction, **_):
    form = "A2" if direction == "out" else "FIRC"
    return (f"{form} REMITTANCE-{desc}-{bene}-{code} {_amount(rng, code, rate)}"
            f"-PURP {pcode}")


@dialect
def _kotak(rng, *, desc, bene, code, rate, pcode, direction, **_):
    return (f"SWIFT {'OUT' if direction=='out' else 'IN'}/{_ref(rng,'KKBK')}/"
            f"IBAN {_ref(rng,'DE',18)}/{bene}/{desc}")


@dialect
def _generic_swift(rng, *, desc, bene, code, rate, pcode, direction, **_):
    return (f"{'OUTWARD' if direction=='out' else 'INWARD'} REMITTANCE {code} "
            f"{_amount(rng, code, rate)} BENE {bene} PURPOSE {pcode} {desc}")


@dialect
def _forex_desk(rng, *, desc, bene, code, rate, pcode, direction, **_):
    return (f"FOREX {'OUTWARD' if direction=='out' else 'INWARD'} REMITTANCE "
            f"PURP CODE {pcode} {desc} {bene}")


@dialect
def _crossborder(rng, *, desc, bene, code, rate, pcode, direction, **_):
    return (f"CROSS BORDER PAYMENT {code} {_amount(rng, code, rate)} "
            f"{'TO' if direction=='out' else 'FROM'} {bene} - {desc}")


CHARGE_DIALECTS: List[Callable[..., str]] = []


def charge_dialect(fn):
    CHARGE_DIALECTS.append(fn)
    return fn


@charge_dialect
def _chg_plain(rng, *, desc, code, rate, **_):
    return f"{desc} {code} {rng.uniform(5, 60):.2f}"


@charge_dialect
def _chg_ref(rng, *, desc, code, rate, **_):
    return f"{desc}-{_ref(rng,'CHG')}-{code}"


@charge_dialect
def _chg_inr(rng, *, desc, code, rate, **_):
    return f"{desc} INR {rng.uniform(200, 9000):.2f} ON {code} REMITTANCE"


@charge_dialect
def _chg_gst(rng, *, desc, code, rate, **_):
    return f"{desc} + GST REF {_ref(rng,'FX')}"


def generate(rows: int, seed: int = 42) -> List[Dict[str, str]]:
    rng = random.Random(seed)
    out: List[Dict[str, str]] = []
    codes = [c for c, _ in CURRENCIES]
    rates = dict(CURRENCIES)

    # ~15% of the corpus is charges and taxes, which is roughly their real share
    # of a cross-border-active account's line count.
    n_charges = int(rows * 0.15)
    n_main = rows - n_charges

    for _ in range(n_main):
        purpose, direction, bene_pool, descriptors = rng.choice(SCENARIOS)
        code = rng.choices(codes, weights=CURRENCY_WEIGHTS, k=1)[0]
        rate = rates[code]
        bene = rng.choice(BENEFICIARIES[bene_pool])
        desc = rng.choice(descriptors)
        pcode = pick_code(rng, purpose, desc)
        pool = [d for d in DIALECTS if not (purpose in CODELESS_PURPOSES and d.needs_code)]
        narration = rng.choice(pool)(
            rng, desc=desc, bene=bene, code=code, rate=rate,
            pcode=pcode, direction=direction,
        )
        out.append({
            "transaction_description": narration,
            "category": purpose,
            "currency": code,
            "direction": "debit" if direction == "out" else "credit",
        })

    for _ in range(n_charges):
        purpose, descriptors = rng.choice(CHARGE_SCENARIOS)
        code = rng.choices(codes, weights=CURRENCY_WEIGHTS, k=1)[0]
        desc = rng.choice(descriptors)
        narration = rng.choice(CHARGE_DIALECTS)(
            rng, desc=desc, code=code, rate=rates[code],
        )
        out.append({
            "transaction_description": narration,
            "category": purpose,
            "currency": code,
            "direction": "debit",
        })

    rng.shuffle(out)
    return out


def sentence_shape(text: str, codes: List[str], benes: List[str]) -> str:
    """Strip the interchangeable slots, leaving the dialect skeleton.

    Distinct-template count is the number the trainer's held-out split uses, but
    on its own it flatters this generator: two templates can differ only by
    beneficiary name and still share every structural cue. Shape count is the
    pessimistic companion figure - how many genuinely different sentence
    layouts exist - and both are printed so neither can be quoted alone.
    """
    from mlmodel.categorizer.purpose_data_quality import narration_template
    t = narration_template(text)
    for b in benes:
        t = t.replace(narration_template(b), "<BENE>")
    for c in codes:
        t = re.sub(rf"\b{c}\b", "<CCY>", t)
    return t


def template_report(rows: List[Dict[str, str]]) -> Dict[str, object]:
    """Template diversity is the number that actually predicts generalisation."""
    from mlmodel.categorizer.purpose_data_quality import narration_template
    templates = {narration_template(r["transaction_description"]) for r in rows}
    ccy = [c for c, _ in CURRENCIES]
    all_benes = sorted({b for pool in BENEFICIARIES.values() for b in pool},
                       key=len, reverse=True)
    shapes = {sentence_shape(r["transaction_description"], ccy, all_benes)
              for r in rows}
    per_class = Counter(r["category"] for r in rows)
    tpl_per_class = Counter()
    seen = set()
    for r in rows:
        key = (r["category"], narration_template(r["transaction_description"]))
        if key not in seen:
            seen.add(key)
            tpl_per_class[r["category"]] += 1
    return {
        "rows": len(rows),
        "distinct_templates": len(templates),
        "distinct_shapes": len(shapes),
        "rows_per_template": round(len(rows) / max(1, len(templates)), 1),
        "classes": len(per_class),
        "templates_per_class": dict(sorted(tpl_per_class.items())),
        "rows_per_class": dict(sorted(per_class.items())),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", type=int, default=40000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "fx_dataset.csv"))
    ap.add_argument("--report", action="store_true", help="print diversity stats and exit")
    args = ap.parse_args()

    rows = generate(args.rows, args.seed)
    rep = template_report(rows)

    print(f"rows                 {rep['rows']:,}")
    print(f"distinct templates   {rep['distinct_templates']:,}")
    print(f"rows per template    {rep['rows_per_template']}")
    print(f"distinct shapes      {rep['distinct_shapes']:,}   (slot-fillers masked)")
    print(f"classes covered      {rep['classes']}")
    print("templates per class:")
    for k, v in rep["templates_per_class"].items():
        print(f"    {k:<26}{v:>6}")

    if args.report:
        return 0

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["transaction_description", "category",
                                           "currency", "direction"])
        w.writeheader()
        w.writerows(rows)
    print(f"\nwritten -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
