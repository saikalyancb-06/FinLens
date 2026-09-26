import asyncio
import socket
import ssl
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from app.mailbox.router import parse_email_domain, classify_by_mx, route_email, _matches_suffix, GOOGLE_MX_SUFFIXES, MICROSOFT_MX_SUFFIXES
from app.mailbox.imap import suggest_imap_host, ImapProvider, KNOWN_IMAP_HOSTS
from app.mailbox.errors import AuthenticationRevoked, ProviderUnavailable


def test_parse_email_domain():
    assert parse_email_domain("alice@example.com") == "example.com"
    assert parse_email_domain("Bob.Smith@CORP.ACME.COM") == "corp.acme.com"
    with pytest.raises(ValueError):
        parse_email_domain("invalid-address")
    with pytest.raises(ValueError):
        parse_email_domain("@nodomain")


def test_classify_by_mx_valid():
    assert classify_by_mx(["aspmx.l.google.com", "alt1.aspmx.l.google.com"]) == "gmail"
    assert classify_by_mx(["custom-domain.mail.protection.outlook.com"]) == "microsoft"
    assert classify_by_mx(["smtp.acme.org"]) is None


def test_classify_by_mx_adversarial_hostnames():
    """Adversarial hostnames embedding keywords as substrings must NOT match."""
    adversarial_google = [
        "mx.notgoogle.com.attacker.net",
        "google.com.attacker.com",
        "fake-googlemail.com.phishing.org",
    ]
    assert classify_by_mx(adversarial_google) is None

    adversarial_ms = [
        "mx.protection.outlook.com.attacker.net",
        "fake-outlook.com.evil.org",
        "office365.com.phishing.xyz",
    ]
    assert classify_by_mx(adversarial_ms) is None


def test_route_email_direct_domains():
    res_gmail = asyncio.run(route_email("user@gmail.com"))
    assert res_gmail["provider"] == "gmail"
    assert res_gmail["auth_type"] == "oauth"
    assert res_gmail["matched_by"] == "direct_domain"
    assert "imap_settings" not in res_gmail

    res_outlook = asyncio.run(route_email("user@outlook.com"))
    assert res_outlook["provider"] == "microsoft"
    assert res_outlook["auth_type"] == "oauth"
    assert res_outlook["matched_by"] == "direct_domain"
    assert "imap_settings" not in res_outlook


def test_route_email_mx_lookup_terminal():
    """Google and Microsoft MX matches are terminal and never fall through to IMAP."""
    with patch("app.mailbox.router.get_mx_records", new_callable=AsyncMock) as mock_mx:
        mock_mx.return_value = ["aspmx.l.google.com"]
        res = asyncio.run(route_email("cfo@techstartup.io"))
        assert res["provider"] == "gmail"
        assert res["auth_type"] == "oauth"
        assert res["matched_by"] == "mx_lookup"
        assert "imap_settings" not in res

    with patch("app.mailbox.router.get_mx_records", new_callable=AsyncMock) as mock_mx:
        mock_mx.return_value = ["enterprise-mail.protection.outlook.com"]
        res = asyncio.run(route_email("accounting@bigfirm.com"))
        assert res["provider"] == "microsoft"
        assert res["auth_type"] == "oauth"
        assert res["matched_by"] == "mx_lookup"
        assert "imap_settings" not in res


def test_route_email_mx_timeout_and_errors_fallthrough_to_imap():
    """DNS timeout, NXDOMAIN, or lookup errors must degrade gracefully to IMAP."""
    # 1. Empty MX list
    with patch("app.mailbox.router.get_mx_records", new_callable=AsyncMock) as mock_mx:
        mock_mx.return_value = []
        res = asyncio.run(route_email("user@customcorp.net"))
        assert res["provider"] == "imap"
        assert res["auth_type"] == "app_password"
        assert res["matched_by"] == "fallback_imap"
        assert res["imap_settings"]["imap_host"] == "mail.customcorp.net"

    # 2. DNS timeout / exception in _resolve_mx_records_sync
    with patch("dns.resolver.Resolver.resolve", side_effect=Exception("DNS Timeout")):
        res = asyncio.run(route_email("user@slowdomain.com"))
        assert res["provider"] == "imap"
        assert res["auth_type"] == "app_password"
        assert res["matched_by"] == "fallback_imap"


def test_suggest_imap_host_coverage():
    """Verify common webmail providers and generic cPanel default fallback."""
    assert suggest_imap_host("user@yahoo.com") == ("imap.mail.yahoo.com", 993)
    assert suggest_imap_host("user@zoho.com") == ("imap.zoho.com", 993)
    assert suggest_imap_host("user@fastmail.com") == ("imap.fastmail.com", 993)
    assert suggest_imap_host("user@icloud.com") == ("imap.mail.me.com", 993)
    assert suggest_imap_host("user@me.com") == ("imap.mail.me.com", 993)
    assert suggest_imap_host("user@gmx.com") == ("imap.gmx.com", 993)
    assert suggest_imap_host("user@aol.com") == ("imap.aol.com", 993)
    assert suggest_imap_host("user@protonmail.com") == ("127.0.0.1", 1143)
    assert suggest_imap_host("treasury@mycustomdomain.org") is None


def test_imap_connection_error_translations():
    """Verify low-level connection errors translate to friendly user messages."""
    provider = ImapProvider(
        host="imap.example.com",
        username="user@example.com",
        password="bad-password",
        port=993,
        use_ssl=True,
    )

    # 1. Host not found (socket.gaierror)
    with patch("imaplib.IMAP4_SSL", side_effect=socket.gaierror(11001, "getaddrinfo failed")):
        with pytest.raises(ProviderUnavailable) as exc:
            provider._connect_blocking()
        assert "Could not find mail server" in str(exc.value.detail)

    # 2. Timeout
    with patch("imaplib.IMAP4_SSL", side_effect=socket.timeout("timed out")):
        with pytest.raises(ProviderUnavailable) as exc:
            provider._connect_blocking()
        assert "timed out" in str(exc.value.detail)

    # 3. SSL Error
    with patch("imaplib.IMAP4_SSL", side_effect=ssl.SSLError("certificate verify failed")):
        with pytest.raises(ProviderUnavailable) as exc:
            provider._connect_blocking()
        assert "SSL/TLS connection failed" in str(exc.value.detail)

    # 4. Connection Refused
    with patch("imaplib.IMAP4_SSL", side_effect=ConnectionRefusedError("Connection refused")):
        with pytest.raises(ProviderUnavailable) as exc:
            provider._connect_blocking()
        assert "connection was refused" in str(exc.value.detail)
