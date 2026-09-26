"""Email-first router: routes any email address to the correct mailbox connector.

Rules:
1. Split domain from address.
2. Direct domain check:
   - gmail.com / googlemail.com -> Google OAuth
   - outlook.com / hotmail.com / live.com / msn.com -> Microsoft OAuth
3. MX record lookup:
   - MX contains google.com -> Google OAuth
   - MX contains outlook.com / protection.outlook.com -> Microsoft OAuth
4. Fallback:
   - All other domains fall through to IMAP with pre-filled suggestions.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from app.mailbox.imap import suggest_imap_host

logger = logging.getLogger(__name__)

# Direct domain mapping for common consumer webmail providers
GOOGLE_DOMAINS = {
    "gmail.com",
    "googlemail.com",
}

MICROSOFT_DOMAINS = {
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "passport.com",
    "outlook.in",
    "hotmail.co.uk",
    "hotmail.fr",
    "live.co.uk",
}

GOOGLE_MX_SUFFIXES = {
    "google.com",
    "googlemail.com",
    "smtp.l.google.com",
}

MICROSOFT_MX_SUFFIXES = {
    "outlook.com",
    "protection.outlook.com",
    "office365.com",
    "microsoft.com",
    "hotmail.com",
}

EMAIL_REGEX = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")


def parse_email_domain(email_address: str) -> str:
    """Validate and extract the lowercased domain part from an email address."""
    addr = (email_address or "").strip().lower()
    match = EMAIL_REGEX.match(addr)
    if not match:
        raise ValueError(f"Invalid email address format: '{email_address}'")
    return match.group(1).strip().lower()


def _matches_suffix(hostname: str, target_suffixes: set[str]) -> bool:
    """Strict domain suffix matching: matches exact domain or subdomains.

    Examples:
    - 'aspmx.l.google.com' matches 'google.com' (True)
    - 'company.mail.protection.outlook.com' matches 'outlook.com' (True)
    - 'mx.notgoogle.com.attacker.net' does NOT match 'google.com' (False)
    - 'fake-outlook.com.evil.org' does NOT match 'outlook.com' (False)
    """
    clean_host = hostname.lower().strip().rstrip(".")
    for suffix in target_suffixes:
        clean_suffix = suffix.lower().strip().rstrip(".")
        if clean_host == clean_suffix or clean_host.endswith("." + clean_suffix):
            return True
    return False


def _resolve_mx_records_sync(domain: str, timeout: float = 3.0) -> List[str]:
    """Query MX records for a domain synchronously with bounded timeout."""
    try:
        import dns.resolver

        resolver = dns.resolver.Resolver()
        resolver.lifetime = timeout
        resolver.timeout = timeout
        answers = resolver.resolve(domain, "MX")
        return [r.exchange.to_text().strip().lower().rstrip(".") for r in answers]
    except Exception as exc:
        logger.debug("[Router] MX lookup failed/timed out for domain '%s': %s", domain, exc)
        return []


async def get_mx_records(domain: str, timeout: float = 3.0) -> List[str]:
    """Asynchronously query MX records for a domain with bounded timeout."""
    return await asyncio.to_thread(_resolve_mx_records_sync, domain, timeout)


def classify_by_mx(mx_hosts: List[str]) -> Optional[str]:
    """Classify provider key ('gmail' or 'microsoft') using strict suffix matching."""
    for host in mx_hosts:
        if _matches_suffix(host, GOOGLE_MX_SUFFIXES):
            return "gmail"
        if _matches_suffix(host, MICROSOFT_MX_SUFFIXES):
            return "microsoft"
    return None


async def route_email(email_address: str) -> Dict[str, Any]:
    """Determine the connector route for an email address.

    Google and Microsoft detections are terminal (never fall through to IMAP).
    Lookup failures/timeouts gracefully fall through to IMAP.

    Returns a dictionary with:
    - email_address: normalized address
    - domain: extracted domain
    - provider: 'gmail' | 'microsoft' | 'imap'
    - auth_type: 'oauth' | 'app_password'
    - imap_settings: (if provider == 'imap') dict with host, port, use_ssl
    - mx_records: list of MX hostnames discovered (if any)
    """
    clean_addr = email_address.strip().lower()
    domain = parse_email_domain(clean_addr)

    # 1. Direct domain match
    if domain in GOOGLE_DOMAINS:
        return {
            "email_address": clean_addr,
            "domain": domain,
            "provider": "gmail",
            "auth_type": "oauth",
            "matched_by": "direct_domain",
            "mx_records": [],
        }

    if domain in MICROSOFT_DOMAINS:
        return {
            "email_address": clean_addr,
            "domain": domain,
            "provider": "microsoft",
            "auth_type": "oauth",
            "matched_by": "direct_domain",
            "mx_records": [],
        }

    # 2. MX records lookup (strictly suffix-matched)
    mx_hosts = await get_mx_records(domain)
    detected_provider = classify_by_mx(mx_hosts)

    if detected_provider == "gmail":
        return {
            "email_address": clean_addr,
            "domain": domain,
            "provider": "gmail",
            "auth_type": "oauth",
            "matched_by": "mx_lookup",
            "mx_records": mx_hosts,
        }

    if detected_provider == "microsoft":
        return {
            "email_address": clean_addr,
            "domain": domain,
            "provider": "microsoft",
            "auth_type": "oauth",
            "matched_by": "mx_lookup",
            "mx_records": mx_hosts,
        }

    # 3. Fallback to IMAP for all other domains
    suggestion = suggest_imap_host(clean_addr)
    imap_settings = {
        "known": bool(suggestion and suggestion[0] != f"mail.{domain}"),
        "imap_host": suggestion[0] if suggestion else f"mail.{domain}",
        "imap_port": suggestion[1] if suggestion else 993,
        "use_ssl": True,
    }

    return {
        "email_address": clean_addr,
        "domain": domain,
        "provider": "imap",
        "auth_type": "app_password",
        "matched_by": "fallback_imap",
        "mx_records": mx_hosts,
        "imap_settings": imap_settings,
    }
