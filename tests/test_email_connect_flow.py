"""Mailbox connect, end to end (2026-10-07).

Any address goes to the sign-in page of whoever hosts its mail — Gmail and
Google Workspace domains (kredo.in, bmsce.ac.in, ...) to Google, Outlook and
Microsoft 365 to Microsoft — the page opens on that address, the outcome is
recorded server-side for the app to read, and pick-up starts on its own.
"""
import asyncio
import urllib.parse
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.mailbox.errors import ConnectionConfigurationError
from app.mailbox.router import classify_by_spf, route_email
from tests.test_mailbox_multi_provider import register_and_login


def _route(addr, mx=(), spf=(), realm=None):
    with patch("app.mailbox.router.get_mx_records", new_callable=AsyncMock, return_value=list(mx)), \
         patch("app.mailbox.router.get_spf_records", new_callable=AsyncMock, return_value=list(spf)), \
         patch("app.mailbox.router.get_microsoft_realm", new_callable=AsyncMock, return_value=realm):
        return asyncio.run(route_email(addr))


# ----------------------------------------------------------------- routing

def test_google_workspace_domains_go_to_google():
    for addr, mx in (("cfo@kredo.in", ["aspmx.l.google.com"]),
                     ("hod@bmsce.ac.in", ["alt1.aspmx.l.google.com", "aspmx2.googlemail.com"]),
                     ("a@newco.in", ["smtp.google.com"])):
        r = _route(addr, mx=mx, realm="Managed")       # an idle MS tenant must not win over MX
        assert (r["provider"], r["auth_type"]) == ("gmail", "oauth"), addr


def test_gateway_domain_uses_spf_then_tenant():
    gw = ["eu-smtp-inbound-1.mimecast.com"]
    assert _route("x@a.com", mx=gw, spf=["v=spf1 include:_spf.google.com ~all"])["provider"] == "gmail"
    assert _route("x@b.com", mx=gw, spf=["v=spf1 include:spf.protection.outlook.com -all"])["provider"] == "microsoft"
    r = _route("x@c.com", mx=gw, realm="Federated")
    assert (r["provider"], r["matched_by"]) == ("microsoft", "microsoft_tenant")


def test_other_providers_stay_imap_and_still_offer_sign_in():
    r = _route("x@zoho.com", mx=["smtpin.zoho.com"], realm="Managed")
    assert r["provider"] == "imap" and r["alternatives"] == ["gmail", "microsoft"]


def test_spf_naming_both_is_not_a_signal():
    assert classify_by_spf(["v=spf1 include:_spf.google.com include:spf.protection.outlook.com ~all"]) is None


# ----------------------------------------------------------------- the flow

def _start(client, headers, addr="cfo@kredo.in"):
    with patch("app.mailbox.router.get_mx_records", new_callable=AsyncMock, return_value=["aspmx.l.google.com"]):
        r = client.post("/email/route", headers=headers, json={"email_address": addr})
    assert r.status_code == 200, r.text
    return r.json()


def test_sign_in_page_opens_on_the_typed_address(client):
    headers, _ = register_and_login(client, "hint")
    data = _start(client, headers)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(data["authorization_url"]).query)
    assert q["login_hint"] == ["cfo@kredo.in"]
    assert q["state"] == [data["state"]]
    assert "gmail.readonly" in q["scope"][0] and q["access_type"] == ["offline"]


def test_result_is_pending_then_connected_and_pickup_starts(client, monkeypatch):
    monkeypatch.setenv("MAILBOX_SCAN_ON_CONNECT", "true")
    started = []
    monkeypatch.setattr("app.email.routes.start_scan_in_background", lambda sid: started.append(sid))
    headers, _ = register_and_login(client, "flow")
    data = _start(client, headers)
    res = client.get(f"/email/oauth/result?state={data['state']}", headers=headers).json()
    assert res["status"] == "pending"

    page = client.get(f"/email/oauth/callback?code=demo_code_x1&state={data['state']}",
                      headers={"Accept": "text/html"})
    assert page.status_code == 200 and "Mailbox connected" in page.text
    assert "BroadcastChannel" in page.text and "window.close" in page.text

    res = client.get(f"/email/oauth/result?state={data['state']}", headers=headers).json()
    assert res["status"] == "connected" and res["connection_id"] and res["scan_id"]
    assert [str(s) for s in started] == [res["scan_id"]]


def test_cancelled_sign_in_is_reported_and_the_page_stays_open(client):
    headers, _ = register_and_login(client, "cancel")
    data = _start(client, headers)
    page = client.get(f"/email/oauth/callback?error=access_denied&state={data['state']}",
                      headers={"Accept": "text/html"})
    assert "You cancelled the connection." in page.text
    assert "if (false) setTimeout" in page.text
    res = client.get(f"/email/oauth/result?state={data['state']}", headers=headers).json()
    assert (res["status"], res["detail"]) == ("failed", "You cancelled the connection.")


def test_another_users_state_is_not_readable(client):
    h1, _ = register_and_login(client, "own1")
    h2, _ = register_and_login(client, "own2")
    data = _start(client, h1)
    assert client.get(f"/email/oauth/result?state={data['state']}", headers=h2).status_code == 404


def test_unticked_mail_permission_is_refused(client):
    headers, _ = register_and_login(client, "scope")
    data = _start(client, headers)
    bundle = {"access_token": "t", "refresh_token": "r", "expires_in": 3600,
              "scope": "openid https://www.googleapis.com/auth/userinfo.email", "email": "cfo@kredo.in"}
    with patch("app.mailbox.oauth.google.GoogleOAuthClient.exchange_code", new_callable=AsyncMock, return_value=bundle):
        r = client.get(f"/email/oauth/callback?code=real&state={data['state']}",
                       headers={"Accept": "application/json"})
    assert r.status_code == 400 and "Read your email" in r.json()["detail"]


def test_google_account_without_gmail_is_explained(client):
    headers, _ = register_and_login(client, "nogmail")
    data = _start(client, headers)
    bundle = {"access_token": "t", "refresh_token": "r", "expires_in": 3600,
              "scope": "openid https://www.googleapis.com/auth/gmail.readonly", "email": "cfo@kredo.in"}
    body = '{"error":{"code":400,"message":"Mail service not enabled","status":"FAILED_PRECONDITION"}}'

    async def fake_get(self, url, **kw):
        return httpx.Response(400, text=body, request=httpx.Request("GET", url))

    with patch("app.mailbox.oauth.google.GoogleOAuthClient.exchange_code", new_callable=AsyncMock, return_value=bundle), \
         patch("httpx.AsyncClient.get", fake_get):
        r = client.get(f"/email/oauth/callback?code=real&state={data['state']}",
                       headers={"Accept": "application/json"})
    assert r.status_code == 400 and "no Gmail mailbox" in r.json()["detail"]


# ----------------------------------------------------------------- redirect URI

class _Req:
    def __init__(self, host, scheme="http", **h):
        self.headers = {"host": host, **h}
        self.url = type("U", (), {"scheme": scheme})()


def test_redirect_uri_prefers_the_registered_one(monkeypatch):
    from app.email.routes import _callback_uri
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://app.kredo.in/email/oauth/callback")
    assert _callback_uri(_Req("127.0.0.1:8000"), "/email/oauth/callback", "", "GOOGLE_REDIRECT_URI") \
        == "https://app.kredo.in/email/oauth/callback"


def test_leftover_localhost_redirect_is_ignored_on_a_server(monkeypatch):
    from app.email.routes import _callback_uri
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/email/oauth/callback")
    got = _callback_uri(_Req("finlens.onrender.com", **{"x-forwarded-proto": "https"}),
                        "/email/oauth/callback", "", "GOOGLE_REDIRECT_URI")
    assert got == "https://finlens.onrender.com/email/oauth/callback"
