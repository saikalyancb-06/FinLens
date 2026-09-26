import os
import re
import hashlib
import base64
import logging
from typing import Optional, Dict, Any
from cryptography.fernet import Fernet, InvalidToken
from app.config import settings

logger = logging.getLogger(__name__)


def _get_encryption_key() -> bytes:
    """Return the Fernet key used to encrypt stored OAuth tokens.

    Prefers a dedicated TOKEN_ENCRYPTION_KEY so that token-at-rest encryption is
    independent of session signing: deriving it from JWT_SECRET_KEY means
    rotating the JWT secret (an ordinary security action) silently renders every
    stored refresh token undecryptable, and any leak of one key compromises both.

    Falls back to the JWT-derived key so existing rows stay readable during
    migration; see decrypt_token, which tries both.
    """
    dedicated = os.getenv("TOKEN_ENCRYPTION_KEY")
    if dedicated:
        digest = hashlib.sha256(dedicated.encode('utf-8')).digest()
        return base64.urlsafe_b64encode(digest)
    return _get_legacy_encryption_key()


def _get_legacy_encryption_key() -> bytes:
    """Legacy key derived from JWT_SECRET_KEY, kept for decrypting existing rows."""
    raw_key = settings.JWT_SECRET_KEY.encode('utf-8')
    digest = hashlib.sha256(raw_key).digest()
    return base64.urlsafe_b64encode(digest)

def encrypt_token(token_str: str) -> str:
    """Encrypts sensitive OAuth tokens before database persistence."""
    if not token_str:
        return ""
    fernet = Fernet(_get_encryption_key())
    return fernet.encrypt(token_str.encode('utf-8')).decode('utf-8')

def decrypt_token(encrypted_str: str) -> str:
    """Decrypts stored OAuth tokens.

    Raises ValueError when the value cannot be decrypted. Returning the raw
    ciphertext on failure (the previous behaviour) sent an unusable string to
    Google as if it were a live token, turning a key mismatch into a confusing
    downstream auth error instead of an actionable one.
    """
    if not encrypted_str:
        return ""

    for key in (_get_encryption_key(), _get_legacy_encryption_key()):
        try:
            return Fernet(key).decrypt(encrypted_str.encode('utf-8')).decode('utf-8')
        except InvalidToken:
            continue
        except Exception:
            continue

    logger.error(
        "[Token Decrypt] Unable to decrypt stored OAuth token with the current or legacy key. "
        "The encryption key has likely changed; the affected account must be reconnected."
    )
    raise ValueError("Stored OAuth token could not be decrypted")

def compute_file_sha256(file_bytes: bytes) -> str:
    """Computes SHA-256 hash of file content for deduplication."""
    return hashlib.sha256(file_bytes).hexdigest()

BANK_PATTERNS = [
    (re.compile(r'sbi|statebank|state bank', re.IGNORECASE), "SBI"),
    (re.compile(r'hdfc|hdfcbank', re.IGNORECASE), "HDFC Bank"),
    (re.compile(r'icici|icicibank', re.IGNORECASE), "ICICI Bank"),
    (re.compile(r'axis|axisbank', re.IGNORECASE), "Axis Bank"),
    (re.compile(r'canara|canarabank', re.IGNORECASE), "Canara Bank"),
    (re.compile(r'kotak', re.IGNORECASE), "Kotak Mahindra Bank"),
    (re.compile(r'yesbank|yes bank', re.IGNORECASE), "YES Bank"),
    (re.compile(r'indusind|indus ind', re.IGNORECASE), "IndusInd Bank"),
    (re.compile(r'idfc|idfcfirst', re.IGNORECASE), "IDFC FIRST Bank"),
    (re.compile(r'bankofbaroda|bob|baroda', re.IGNORECASE), "Bank of Baroda"),
    (re.compile(r'unionbank|union bank', re.IGNORECASE), "Union Bank of India"),
    (re.compile(r'pnb|punjab national', re.IGNORECASE), "Punjab National Bank"),
]

STATEMENT_SUBJECT_PATTERNS = [
    re.compile(r'statement', re.IGNORECASE),
    re.compile(r'account statement', re.IGNORECASE),
    re.compile(r'monthly statement', re.IGNORECASE),
    re.compile(r'bank statement', re.IGNORECASE),
    re.compile(r'e-statement|estatement', re.IGNORECASE),
    re.compile(r'mini statement', re.IGNORECASE),
    re.compile(r'transaction summary', re.IGNORECASE),
]

def detect_bank_from_email(sender: str, subject: str, body: str = "") -> str:
    """Detects bank name based on sender email address, subject line, or body content."""
    text_to_check = f"{sender} {subject} {body}"
    for pattern, bank_name in BANK_PATTERNS:
        if pattern.search(text_to_check):
            return bank_name
    return "Bank Statement"

def is_statement_email(sender: str, subject: str) -> bool:
    """Returns True if the subject or sender matches known bank statement patterns."""
    text_to_check = f"{sender} {subject}"
    if any(pat.search(text_to_check) for pat in STATEMENT_SUBJECT_PATTERNS):
        return True
    return any(bank_pat.search(sender) for bank_pat, _ in BANK_PATTERNS)
