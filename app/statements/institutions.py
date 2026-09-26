"""Identify which financial institution a document came from.

Two rules govern this module:

1. **A predefined list is never a prerequisite.** The known-institution table is
   one signal among several; a document from an institution absent from it is
   still identified, from its own letterhead, by generic patterns.
2. **Failure is recorded, not fatal.** When nothing is confident enough, the
   institution is ``UNKNOWN`` and the statement is kept. Discarding a statement
   because we could not name its bank would throw away exactly the transactions
   the user is asking for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

#: Known institutions, used as a *hint*. Extending it improves labelling; it
#: never gates detection. Keys are matched case-insensitively as whole words.
KNOWN_INSTITUTIONS: Dict[str, Tuple[str, ...]] = {
    "HDFC Bank": ("hdfc bank", "hdfcbank", "hdfc"),
    "ICICI Bank": ("icici bank", "icicibank", "icici"),
    "State Bank of India": ("state bank of india", "sbi", "statebank"),
    "Axis Bank": ("axis bank", "axisbank"),
    "Kotak Mahindra Bank": ("kotak mahindra", "kotak"),
    "Canara Bank": ("canara bank", "canarabank"),
    "Bank of Baroda": ("bank of baroda", "bankofbaroda", "bob bank"),
    "Punjab National Bank": ("punjab national bank", "pnb"),
    "Union Bank of India": ("union bank of india", "unionbank"),
    "IndusInd Bank": ("indusind bank", "indusind"),
    "IDFC FIRST Bank": ("idfc first bank", "idfc first", "idfcfirst", "idfc"),
    "YES Bank": ("yes bank", "yesbank"),
    "Federal Bank": ("federal bank", "federalbank"),
    "IDBI Bank": ("idbi bank", "idbi"),
    "RBL Bank": ("rbl bank", "rblbank"),
    "Bandhan Bank": ("bandhan bank", "bandhan"),
    "AU Small Finance Bank": ("au small finance bank", "au bank"),
    "Citibank": ("citibank", "citi bank"),
    "HSBC": ("hsbc",),
    "Standard Chartered": ("standard chartered", "stanchart"),
    "Deutsche Bank": ("deutsche bank",),
    "DBS Bank": ("dbs bank", "dbs"),
    "Barclays": ("barclays",),
    "American Express": ("american express", "amex"),
    "Paytm Payments Bank": ("paytm payments bank", "paytm bank"),
    "Airtel Payments Bank": ("airtel payments bank",),
    "Zerodha": ("zerodha", "kite by zerodha"),
    "Groww": ("groww",),
    "Upstox": ("upstox",),
    "ICICI Direct": ("icicidirect", "icici direct"),
    "HDFC Securities": ("hdfc securities", "hdfcsec"),
    "Angel One": ("angel one", "angelbroking", "angel broking"),
    "Kredo": ("kredo",),
}

#: Suffixes and words that mark a company name as a financial institution.
#: These are what let an unlisted institution be recognised: "Sahyadri Sahakari
#: Bank Ltd" is identified as a bank by the word "Bank", not by being on a list.
_INSTITUTION_TOKENS = (
    "bank", "banking", "bancorp", "banca", "banco",
    "credit union", "cooperative", "co-operative", "sahakari",
    "financial services", "finserv", "finance", "financial",
    "securities", "broking", "brokerage", "capital", "wealth",
    "asset management", "mutual fund", "amc", "investments",
    "payments bank", "small finance bank", "nbfc", "building society",
)

_LEGAL_SUFFIX = r"(?:ltd\.?|limited|plc|inc\.?|llc|llp|pvt\.?\s*ltd\.?|n\.a\.|nv|ag|sa)"

#: "<Capitalised words> Bank" and friends. Bounded to five leading words so a
#: whole sentence is never captured as a name.
_NAMED_INSTITUTION_RE = re.compile(
    r"\b((?:[A-Z][\w&.'-]*\s+){0,4}"
    r"(?:Bank|Banking|Securities|Broking|Brokerage|Capital|Finance|Financial|"
    r"Finserv|Investments|Mutual\s+Fund|Asset\s+Management|Credit\s+Union|"
    r"Payments\s+Bank|Small\s+Finance\s+Bank)"
    rf"(?:\s+{_LEGAL_SUFFIX})?)\b"
)

_NOISE_NAMES = {
    "the bank", "your bank", "a bank", "this bank", "our bank", "any bank",
    "internet banking", "net banking", "mobile banking", "online banking",
    "core banking", "banking services", "banking partner", "the finance",
}

#: Public mail domains: seeing one tells us nothing about the institution.
_PUBLIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "ymail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com", "me.com",
    "aol.com", "protonmail.com", "proton.me", "zoho.com", "gmx.com",
    "rediffmail.com", "mail.com", "fastmail.com",
}

UNKNOWN = "UNKNOWN"


@dataclass
class InstitutionMatch:
    name: str = UNKNOWN
    confidence: float = 0.0
    evidence: str = ""

    @property
    def is_known(self) -> bool:
        return self.name != UNKNOWN


def _match_known(text: str) -> Optional[Tuple[str, str]]:
    lowered = f" {re.sub(r'[^a-z0-9]+', ' ', (text or '').lower())} "
    best: Optional[Tuple[str, str, int]] = None
    for canonical, aliases in KNOWN_INSTITUTIONS.items():
        for alias in aliases:
            needle = f" {re.sub(r'[^a-z0-9]+', ' ', alias)} "
            if needle in lowered:
                # Longest alias wins: "hdfc securities" must not be reported as
                # "HDFC Bank" merely because "hdfc" also matched.
                if best is None or len(alias) > best[2]:
                    best = (canonical, alias, len(alias))
    if best:
        return best[0], best[1]
    return None


def _match_generic(text: str) -> Optional[Tuple[str, str]]:
    for candidate in _NAMED_INSTITUTION_RE.findall(text or ""):
        cleaned = re.sub(r"\s+", " ", candidate).strip(" .,:;")
        if len(cleaned) < 4 or len(cleaned) > 80:
            continue
        if cleaned.lower() in _NOISE_NAMES:
            continue
        # A bare institution token with no name in front of it ("Bank",
        # "Finance") is a column header, not an institution.
        if cleaned.lower() in {t for t in _INSTITUTION_TOKENS}:
            continue
        if len(cleaned.split()) < 2:
            continue
        return cleaned, cleaned
    return None


def _from_domain(domain: str) -> Optional[str]:
    domain = (domain or "").strip().lower()
    if not domain or domain in _PUBLIC_MAIL_DOMAINS:
        return None
    label = domain.split(".")[0]
    # Strip the mail-subdomain noise banks put in front of their name.
    for prefix in ("mail", "email", "smtp", "no-reply", "noreply", "alerts",
                   "estatement", "estatements", "statements", "notify", "notification"):
        if label == prefix:
            parts = domain.split(".")
            label = parts[1] if len(parts) > 1 else label
    if len(label) < 3:
        return None
    return label


def identify_institution(
    document_text: str = "",
    *,
    sender_domain: str = "",
    sender_name: str = "",
    subject: str = "",
    filename: str = "",
) -> InstitutionMatch:
    """Name the institution behind a statement, or return ``UNKNOWN``.

    Evidence is weighed in order of trustworthiness: what the document says
    about itself outranks what the email envelope says about it, because a
    statement can be forwarded from any address but its letterhead travels with
    it.
    """
    # 1. The document's own text, matched against known institutions.
    if document_text:
        known = _match_known(document_text[:20_000])
        if known:
            return InstitutionMatch(known[0], 0.95,
                                    f"document text names {known[0]} ('{known[1]}')")

    # 2. The document's own text, matched generically. This is the path that
    #    handles institutions nobody has listed.
    if document_text:
        generic = _match_generic(document_text[:20_000])
        if generic:
            return InstitutionMatch(generic[0], 0.70,
                                    f"document letterhead reads '{generic[0]}'")

    # 3. Sender display name and subject.
    envelope = f"{sender_name} {subject} {filename}"
    known = _match_known(envelope)
    if known:
        return InstitutionMatch(known[0], 0.65,
                                f"sender/subject names {known[0]} ('{known[1]}')")
    generic = _match_generic(envelope)
    if generic:
        return InstitutionMatch(generic[0], 0.50,
                                f"sender name reads '{generic[0]}'")

    # 4. Sender domain, if it is not a public mail provider.
    known = _match_known(sender_domain)
    if known:
        return InstitutionMatch(known[0], 0.60, f"sender domain matches {known[0]}")
    label = _from_domain(sender_domain)
    if label:
        pretty = label.replace("-", " ").replace("_", " ").title()
        return InstitutionMatch(pretty, 0.35,
                                f"derived from sender domain '{sender_domain}'")

    return InstitutionMatch(UNKNOWN, 0.0,
                            "no institution could be identified from the document or the sender")


def looks_like_institution(text: str) -> bool:
    """True when ``text`` reads like the name of a financial institution."""
    lowered = (text or "").lower()
    return any(token in lowered for token in _INSTITUTION_TOKENS)
