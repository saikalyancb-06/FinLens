"""Tests for the provider-agnostic mailbox and statement-discovery system.

Grouped by the property being defended:

* the provider abstraction really is an abstraction (every connector satisfies
  the same interface; the scan engine never names one);
* discovery does not depend on knowing the sender or the institution;
* the same mailbox scanned twice does not produce duplicate transactions;
* one user can never reach another user's mailbox, documents, scans or tokens;
* credentials are encrypted at rest and never leave the backend.
"""
from __future__ import annotations

import base64
import datetime
import email.message
import uuid

import pytest
from fastapi.testclient import TestClient

from app.email.models import ConnectedAccount
from app.mailbox.base import EmailProvider
from app.mailbox.criteria import SearchCriteria
from app.mailbox.registry import PROVIDER_CLASSES, normalise_provider
from app.models.statement import Statement
from app.models.transaction import Transaction


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def register_and_login(client: TestClient, prefix: str):
    address = f"{prefix}_{uuid.uuid4().hex[:6]}@example.com"
    reg = client.post("/auth/register", json={"email": address, "password": "Password123!"})
    assert reg.status_code == 201, reg.text
    login = client.post("/auth/login", json={"email": address, "password": "Password123!"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}, reg.json()["id"]


def connect_demo_mailbox(client: TestClient, headers: dict, provider: str = "gmail") -> str:
    """Run the real OAuth route pair against the demo credential path."""
    import urllib.parse

    start = client.post(f"/email/connections/{provider}/authorize", headers=headers)
    assert start.status_code == 200, start.text
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(start.json()["authorization_url"]).query
    )["state"][0]

    callback = "/email/oauth/callback" if provider == "gmail" else f"/email/oauth/{provider}/callback"
    done = client.get(f"{callback}?code=demo_code_{uuid.uuid4().hex[:6]}&state={state}",
                      headers={**headers, "Accept": "application/json"})
    assert done.status_code == 200, done.text
    return done.json()["connection_id"]


def register_account(client: TestClient, headers: dict, number: str = "001234567890"):
    res = client.post("/v1/bank-master/accounts", headers=headers, json={
        "bank_code": "HDFC", "account_number": number,
        "account_type": "CURRENT", "currency": "INR",
    })
    assert res.status_code == 201, res.text
    return res.json()


# ---------------------------------------------------------------------------
# 1. The abstraction
# ---------------------------------------------------------------------------

class TestProviderAbstraction:
    def test_every_registered_connector_implements_the_interface(self):
        """A connector that misses a method must fail here, not mid-scan."""
        required = [
            "connect", "authenticate", "refresh_authentication", "get_account_info",
            "search_messages", "get_message", "get_attachments", "download_attachment",
            "disconnect", "revoke_access",
        ]
        assert set(PROVIDER_CLASSES) == {"gmail", "microsoft", "imap"}
        for key, cls in PROVIDER_CLASSES.items():
            assert issubclass(cls, EmailProvider), key
            for method in required:
                assert callable(getattr(cls, method, None)), f"{key} is missing {method}()"

    def test_legacy_provider_names_still_resolve(self):
        assert normalise_provider("google") == "gmail"
        assert normalise_provider("outlook") == "microsoft"
        assert normalise_provider("hotmail") == "microsoft"
        assert normalise_provider("yahoo") == "imap"
        assert normalise_provider(None) == "gmail"

    def test_the_discovery_engine_names_no_provider(self):
        """The point of the abstraction, asserted directly.

        If a provider name ever appears in the scan engine, someone has special-
        cased a transport in code that is supposed to be transport-agnostic.
        """
        import pathlib

        # encoding is explicit: these modules contain non-ASCII characters, and
        # Path.read_text() otherwise uses the platform's locale encoding, which
        # is cp1252 on Windows and raises before the assertion is ever reached.
        source = pathlib.Path("app/statements/discovery.py").read_text(
            encoding="utf-8").lower()
        body = source.split('"""', 2)[-1]  # skip the module docstring's diagram
        for name in ("gmail", "graph", "imaplib", "googleapis", "outlook"):
            assert name not in body, f"discovery.py refers to {name!r}"

    def test_no_smtp_anywhere_in_the_mailbox_path(self):
        """SMTP sends mail; it cannot retrieve it. It must not appear here."""
        import pathlib

        for folder in ("app/mailbox", "app/statements"):
            for path in pathlib.Path(folder).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                # The library, not the word: these modules discuss SMTP in prose
                # precisely to record that it is not used for retrieval, so the
                # test looks for an actual import rather than a mention.
                assert "smtplib" not in text, f"{path} imports smtplib"
                assert "SMTP(" not in text, f"{path} opens an SMTP connection"


# ---------------------------------------------------------------------------
# 2. Per-provider payload shaping (no network)
# ---------------------------------------------------------------------------

class TestGmailShaping:
    def test_urlsafe_base64_without_padding_decodes(self):
        from app.mailbox.gmail import decode_b64url

        raw = b"Date,Description,Amount\n01/07/2026,SALARY,100.00\n"
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        assert decode_b64url(encoded) == raw

    def test_nested_and_forwarded_parts_are_all_found(self):
        """Gmail nests forwarded mail; a flat walk loses the statement inside."""
        from app.mailbox.gmail import _detail_from_full

        payload = {
            "id": "m1",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "Subject", "value": "Fwd: your statement"},
                    {"name": "From", "value": "Someone <someone@example.com>"},
                    {"name": "Date", "value": "Tue, 07 Jul 2026 10:00:00 +0000"},
                ],
                "parts": [
                    {"mimeType": "text/plain", "filename": "",
                     "body": {"data": base64.urlsafe_b64encode(b"see attached").decode()}},
                    {"mimeType": "multipart/mixed", "filename": "", "body": {},
                     "parts": [
                         {"mimeType": "application/pdf", "filename": "deep_statement.pdf",
                          "body": {"attachmentId": "att-deep", "size": 4242}},
                     ]},
                ],
            },
        }
        detail = _detail_from_full(payload)
        assert detail.subject == "Fwd: your statement"
        assert detail.body_text.strip() == "see attached"
        assert [a.filename for a in detail.attachments] == ["deep_statement.pdf"]
        assert detail.attachments[0].ref == "att-deep"
        assert detail.received_at == datetime.datetime(2026, 7, 7, 10, 0)

    def test_the_search_query_names_no_bank(self):
        from app.mailbox.gmail import build_query

        query = build_query(SearchCriteria(require_attachment=True,
                                           since=datetime.datetime(2026, 1, 1)))
        assert "has:attachment" in query
        assert "after:2026/01/01" in query
        for bank in ("hdfc", "icici", "sbi", "axis", "kredo", "from:"):
            assert bank not in query.lower()


class TestMicrosoftShaping:
    def test_filter_covers_attachments_and_the_date_window(self):
        from app.mailbox.microsoft import build_filter

        params = build_filter(SearchCriteria(require_attachment=True,
                                             since=datetime.datetime(2026, 1, 1)))
        assert "hasAttachments eq true" in params["$filter"]
        assert "receivedDateTime ge 2026-01-01T00:00:00Z" in params["$filter"]

    def test_graph_message_maps_onto_the_common_type(self):
        from app.mailbox.microsoft import _detail_from_graph, _summary_from_graph

        item = {
            "id": "AAMk", "subject": "Your account statement",
            "from": {"emailAddress": {"address": "no-reply@bank.example", "name": "Bank"}},
            "receivedDateTime": "2026-07-07T10:00:00Z",
            "bodyPreview": "statement attached", "hasAttachments": True,
            "body": {"contentType": "html", "content": "<p>statement</p>"},
            "toRecipients": [{"emailAddress": {"address": "me@example.com"}}],
        }
        summary = _summary_from_graph(item)
        assert summary.sender_domain == "bank.example"
        assert summary.has_attachments is True
        assert summary.received_at == datetime.datetime(2026, 7, 7, 10, 0)

        detail = _detail_from_graph(item)
        assert detail.body_html == "<p>statement</p>"
        assert detail.to == "me@example.com"


class TestImapShaping:
    def test_a_forwarded_message_yields_the_inner_attachment(self):
        """message/rfc822 is the forwarded-statement case."""
        from app.mailbox.imap import message_to_detail

        inner = email.message.EmailMessage()
        inner["Subject"] = "Your statement"
        inner["From"] = "bank@example.test"
        inner.set_content("statement attached")
        inner.add_attachment(b"Date,Amount\n01/07/2026,10.00\n",
                             maintype="text", subtype="csv", filename="inner.csv")

        outer = email.message.EmailMessage()
        outer["Subject"] = "Fwd: Your statement"
        outer["From"] = "Colleague <c@example.com>"
        outer["Date"] = "Tue, 07 Jul 2026 10:00:00 +0000"
        outer.set_content("forwarding this")
        # A message/rfc822 part has to be attached as a Message, not encoded as
        # bytes: that is exactly the structure a forwarded statement arrives in.
        outer.make_mixed()
        outer.attach(inner)

        detail = message_to_detail("42", outer)
        assert detail.subject == "Fwd: Your statement"
        assert "inner.csv" in [a.filename for a in detail.attachments]

    def test_host_suggestions_are_hints_not_requirements(self):
        from app.mailbox.imap import suggest_imap_host

        assert suggest_imap_host("a@yahoo.com") == ("imap.mail.yahoo.com", 993)
        assert suggest_imap_host("a@my-own-domain.example") is None


# ---------------------------------------------------------------------------
# 3. Discovery: unknown institutions, filenames, bodies, non-statements
# ---------------------------------------------------------------------------

class TestDiscoverySignals:
    def test_an_unlisted_institution_is_still_identified(self):
        from app.statements.institutions import identify_institution

        match = identify_institution(
            "Sahyadri Sahakari Bank Ltd\nAccount Statement\nA/C No: 001234567890",
        )
        assert match.name == "Sahyadri Sahakari Bank"
        assert match.confidence > 0.5

    def test_an_unidentifiable_institution_becomes_unknown_not_a_rejection(self):
        from app.statements.classifier import classify_content
        from app.statements.document_text import DocumentContent

        content = DocumentContent(kind="csv", text=(
            "Date,Description,Debit,Credit,Balance\n"
            "01/07/2026,SALARY,0.00,100.00,100.00\n"
            "02/07/2026,RENT,50.00,0.00,50.00\n"
        ), rows=[
            ["Date", "Description", "Debit", "Credit", "Balance"],
            ["01/07/2026", "SALARY", "0.00", "100.00", "100.00"],
            ["02/07/2026", "RENT", "50.00", "0.00", "50.00"],
        ])
        verdict = classify_content(content, filename="export.csv")
        assert verdict.is_transactional is True
        assert verdict.institution_name == "UNKNOWN"

    def test_the_filename_does_not_decide(self, tmp_path):
        """'monthly_document.pdf' is a statement; 'statement.pdf' need not be."""
        from app.statements.classifier import classify_file

        misleading = tmp_path / "monthly_document.csv"
        misleading.write_text(
            "Some Unlisted Bank Ltd\nAccount Statement\n"
            "Date,Narration,Withdrawal,Deposit,Balance\n"
            "01/07/2026,SALARY,0.00,100.00,100.00\n"
            "02/07/2026,RENT,50.00,0.00,50.00\n"
        )
        assert classify_file(str(misleading), "monthly_document.csv").is_transactional is True

        flattering = tmp_path / "statement.csv"
        flattering.write_text(
            "Tax Invoice GSTIN 27AAAAA0000A1Z5\nOrder ID 998877\n"
            "Cinema ticket, Seat A12, Convenience Fee 40.00\nTotal 450.00\n"
        )
        verdict = classify_file(str(flattering), "statement.csv")
        assert verdict.is_transactional is False
        assert verdict.document_type == "NOT_FINANCIAL"

    def test_mime_type_qualifies_an_attachment_with_no_extension(self):
        from app.statements.signals import attachment_is_candidate

        ok, _ = attachment_is_candidate("document", "application/pdf", 1000)
        assert ok is True
        rejected, why = attachment_is_candidate("logo.png", "image/png", 1000)
        assert rejected is False and "png" in why.lower()

    def test_a_statement_in_the_email_body_is_recovered(self):
        from app.mailbox.types import MessageDetail
        from app.statements.body_extractor import extract_body_statement

        detail = MessageDetail(id="1", subject="Your account summary", body_html="""
            <table>
              <tr><th>Date</th><th>Description</th><th>Debit</th><th>Credit</th><th>Balance</th></tr>
              <tr><td>04/07/2026</td><td>CARD PAYMENT</td><td>1899.00</td><td>0.00</td><td>44101.00</td></tr>
              <tr><td>07/07/2026</td><td>INTEREST</td><td>0.00</td><td>312.00</td><td>44413.00</td></tr>
            </table>""")
        body = extract_body_statement(detail)
        assert body is not None and body.is_ledger
        assert b"CARD PAYMENT" in body.to_csv_bytes()

    def test_external_links_are_recorded_and_never_followed(self):
        from app.mailbox.types import MessageDetail
        from app.statements.body_extractor import (
            extract_body_statement, mentions_external_statement_link,
        )

        detail = MessageDetail(
            id="1", subject="Your statement is ready",
            body_text="Please log in to view your statement at https://bank.example/login",
        )
        assert mentions_external_statement_link(detail) is True
        assert extract_body_statement(detail) is None

    def test_a_single_transaction_month_is_still_a_statement(self):
        """A quiet month is not a reason to reject a statement."""
        from app.statements.classifier import analyse_text

        shape = analyse_text(
            "HDFC Bank Statement\nDate Particulars Debit Credit Balance\n"
            "01/08/2026 FEE 100.00 0.00 9900.00\n"
        )
        assert shape.is_ledger is True


# ---------------------------------------------------------------------------
# 4. End-to-end scan through the demo provider
# ---------------------------------------------------------------------------

class TestScanEndToEnd:
    def test_scan_finds_statements_and_rejects_the_invoice(self, client):
        headers, _ = register_and_login(client, "scan_user")
        connect_demo_mailbox(client, headers)

        res = client.post("/email/scan", headers=headers)
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "success"

        docs = client.get("/email/statements", headers=headers).json()
        assert docs, "the scan found nothing at all"

        by_name = {d["filename"]: d for d in docs}

        # Filename gives nothing away; content identifies it.
        unknown = by_name.get("monthly_document.csv")
        assert unknown is not None, f"unknown-bank statement missing from {list(by_name)}"
        assert unknown["classification"] != "NOT_BANK_STATEMENT"
        assert unknown["institution"] == "Sahyadri Sahakari Bank"

        # The invoice came from a bank-sounding sender and is still rejected.
        invoice = next((d for d in docs if "EatSure" in d["filename"]), None)
        if invoice is not None:
            assert invoice["classification"] == "NOT_BANK_STATEMENT"
            assert invoice["import_status"] == "SKIPPED"

        # The body-only statement produced a document with no attachment.
        assert any(d["source_kind"] == "BODY" for d in docs), \
            "the HTML-body statement was not recovered"

    def test_the_same_document_in_two_messages_is_stored_once(self, client):
        headers, _ = register_and_login(client, "dedupe_user")
        connect_demo_mailbox(client, headers)
        client.post("/email/scan", headers=headers)

        docs = client.get("/email/statements", headers=headers).json()
        # demo_msg_unknown_bank and demo_msg_forwarded_duplicate carry identical
        # bytes under different filenames in different messages.
        assert not any(d["filename"] == "statement_copy.csv" for d in docs)

    def test_rescanning_creates_no_duplicate_documents_or_transactions(self, client, request):
        headers, user_id = register_and_login(client, "idem_user")
        account = register_account(client, headers, "001234567890")
        connect_demo_mailbox(client, headers)

        client.post("/email/scan", headers=headers)
        first = client.get("/email/statements", headers=headers).json()
        target = next(d for d in first if d["classification"] != "NOT_BANK_STATEMENT")

        imported = client.post(f"/email/import/{target['id']}", headers=headers,
                               json={"bank_account_id": account["id"]})
        assert imported.status_code == 200, imported.text
        created = imported.json()["transactions_created"]
        assert created >= 1

        # Scan again, then import the same document again.
        client.post("/email/scan", headers=headers)
        second = client.get("/email/statements", headers=headers).json()
        assert len(second) == len(first), "a second scan created duplicate documents"

        client.post(f"/email/import/{target['id']}", headers=headers,
                    json={"bank_account_id": account["id"]})

        from tests.conftest import TestingSessionLocal

        db = TestingSessionLocal()
        try:
            statements = db.query(Statement).filter(
                Statement.user_id == uuid.UUID(user_id)).all()
            assert len(statements) == 1, "re-import created a second statement"
            total = db.query(Transaction).filter(
                Transaction.user_id == uuid.UUID(user_id)).count()
            assert total == created, "re-import duplicated transactions"
        finally:
            db.close()

    def test_a_background_scan_reports_progress_and_finishes(self, client):
        headers, _ = register_and_login(client, "bg_user")
        connect_demo_mailbox(client, headers)

        started = client.post("/email/scans?auto_import=false", headers=headers)
        assert started.status_code == 200, started.text
        scan_id = started.json()["scans"][0]["id"]

        import time

        deadline = time.time() + 60
        payload = {}
        while time.time() < deadline:
            payload = client.get(f"/email/scans/{scan_id}", headers=headers).json()
            if payload["status"] in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.3)

        assert payload.get("status") == "COMPLETED", payload
        assert payload["progress_pct"] == 100
        assert payload["messages_scanned"] >= 1
        assert payload["documents_downloaded"] >= 1

    def test_a_mailbox_with_nothing_in_it_reports_that_plainly(self, client, monkeypatch):
        headers, _ = register_and_login(client, "empty_user")
        connect_demo_mailbox(client, headers)

        import app.mailbox.demo as demo

        monkeypatch.setattr(demo, "DEMO_MESSAGES", [])
        res = client.post("/email/scan", headers=headers)
        assert res.status_code == 200
        body = res.json()
        assert body["total_found"] == 0
        assert body["reason"] == "no_statements_found"


# ---------------------------------------------------------------------------
# 5. Multi-user isolation
# ---------------------------------------------------------------------------

class TestUserIsolation:
    def test_one_user_cannot_reach_another_users_mailbox_or_documents(self, client):
        headers_a, user_a = register_and_login(client, "iso_a")
        headers_b, user_b = register_and_login(client, "iso_b")

        connection_a = connect_demo_mailbox(client, headers_a)
        connect_demo_mailbox(client, headers_b)

        client.post("/email/scan", headers=headers_a)
        docs_a = client.get("/email/statements", headers=headers_a).json()
        assert docs_a

        # B's own list must not contain A's documents.
        docs_b = client.get("/email/statements", headers=headers_b).json()
        assert not ({d["id"] for d in docs_a} & {d["id"] for d in docs_b})

        # B cannot see, scan, import or disconnect A's connection.
        assert connection_a not in [c["id"] for c in
                                    client.get("/email/connections", headers=headers_b).json()]
        assert client.post(f"/email/scans?connection_id={connection_a}",
                           headers=headers_b).status_code == 404
        assert client.delete(f"/email/connections/{connection_a}",
                             headers=headers_b).status_code == 404

        target = docs_a[0]["id"]
        assert client.post(f"/email/import/{target}", headers=headers_b,
                           json={}).status_code == 404
        assert client.post(f"/email/statements/{target}/map-account", headers=headers_b,
                           json={"account_id": str(uuid.uuid4())}).status_code == 404

    def test_a_scan_belonging_to_another_user_is_not_readable(self, client):
        headers_a, _ = register_and_login(client, "scaniso_a")
        headers_b, _ = register_and_login(client, "scaniso_b")
        connect_demo_mailbox(client, headers_a)

        res = client.post("/email/scan", headers=headers_a)
        scan_id = res.json()["scan_ids"][0]

        assert client.get(f"/email/scans/{scan_id}", headers=headers_a).status_code == 200
        assert client.get(f"/email/scans/{scan_id}", headers=headers_b).status_code == 404

    def test_a_users_statement_cannot_be_filed_into_another_users_account(self, client):
        headers_a, _ = register_and_login(client, "acct_a")
        headers_b, _ = register_and_login(client, "acct_b")
        account_b = register_account(client, headers_b, "999988887777")

        connect_demo_mailbox(client, headers_a)
        client.post("/email/scan", headers=headers_a)
        docs = client.get("/email/statements", headers=headers_a).json()
        target = next(d for d in docs if d["classification"] != "NOT_BANK_STATEMENT")

        res = client.post(f"/email/import/{target['id']}", headers=headers_a,
                          json={"bank_account_id": account_b["id"]})
        assert res.status_code == 403
        assert "another user" in res.json()["detail"].lower()

    def test_disconnecting_stops_access_for_that_user_only(self, client):
        headers_a, _ = register_and_login(client, "disc_a")
        headers_b, _ = register_and_login(client, "disc_b")
        connection_a = connect_demo_mailbox(client, headers_a)
        connect_demo_mailbox(client, headers_b)

        assert client.delete(f"/email/connections/{connection_a}",
                             headers=headers_a).status_code == 200

        status_a = client.get("/email/status", headers=headers_a).json()
        assert status_a["connected"] is False
        assert client.get("/email/status", headers=headers_b).json()["connected"] is True

        # With no active connection, a scan has nothing to reach.
        assert client.post("/email/scan", headers=headers_a).json()["reason"] == \
            "no_connected_account"


# ---------------------------------------------------------------------------
# 6. Credentials
# ---------------------------------------------------------------------------

class TestCredentialHandling:
    def test_tokens_are_encrypted_at_rest_and_never_returned(self, client):
        headers, user_id = register_and_login(client, "cred_user")
        connect_demo_mailbox(client, headers)

        body = client.get("/email/connections", headers=headers).text.lower()
        for leak in ("access_token", "refresh_token", "encrypted_secret",
                     "client_secret", "demo_access_token"):
            assert leak not in body, f"{leak} appears in the connections response"

        from tests.conftest import TestingSessionLocal

        db = TestingSessionLocal()
        try:
            connection = db.query(ConnectedAccount).filter(
                ConnectedAccount.user_id == uuid.UUID(user_id)).first()
            assert connection is not None
            assert connection.access_token
            # Fernet ciphertext, not the plaintext demo token.
            assert not connection.access_token.startswith("demo_")
            assert connection.encrypted_refresh_token.startswith("gAAAAA")
        finally:
            db.close()

    def test_a_connection_whose_key_changed_asks_to_be_reconnected(self, client):
        """An undecryptable credential must be an instruction, not a stack trace."""
        headers, user_id = register_and_login(client, "rot_user")
        connect_demo_mailbox(client, headers)

        from tests.conftest import TestingSessionLocal

        db = TestingSessionLocal()
        try:
            connection = db.query(ConnectedAccount).filter(
                ConnectedAccount.user_id == uuid.UUID(user_id)).first()
            connection.access_token = "gAAAAABnot-a-valid-ciphertext"
            connection.encrypted_refresh_token = "gAAAAABnot-a-valid-ciphertext"
            db.commit()
        finally:
            db.close()

        res = client.post("/email/scan", headers=headers)
        assert res.status_code == 200
        assert res.json()["reason"] == "credentials_unreadable"

        connections = client.get("/email/connections", headers=headers).json()
        assert connections[0]["status"] == "NEEDS_REAUTH"
        assert "reconnect" in (connections[0]["status_detail"] or "").lower()

    def test_the_oauth_scopes_requested_are_read_only(self):
        from app.mailbox.oauth.google import GMAIL_SCOPES
        from app.mailbox.oauth.microsoft import MICROSOFT_SCOPES

        assert "https://www.googleapis.com/auth/gmail.readonly" in GMAIL_SCOPES
        assert not any("send" in s or "modify" in s or "compose" in s for s in GMAIL_SCOPES)
        assert "https://graph.microsoft.com/Mail.Read" in MICROSOFT_SCOPES
        assert not any("Send" in s or "ReadWrite" in s for s in MICROSOFT_SCOPES)

    def test_an_imap_connection_refuses_to_be_created_without_a_credential(self):
        from app.mailbox.errors import ConnectionConfigurationError
        from app.mailbox.imap import ImapProvider

        with pytest.raises(ConnectionConfigurationError):
            ImapProvider(host="imap.example.test", username="a@example.test")


# ---------------------------------------------------------------------------
# 7. Provider catalogue surfaced to the UI
# ---------------------------------------------------------------------------

def test_the_provider_catalogue_offers_three_ways_to_connect(client):
    headers, _ = register_and_login(client, "cat_user")
    providers = client.get("/email/providers", headers=headers).json()
    keys = {p["key"] for p in providers}
    assert keys == {"gmail", "microsoft", "imap"}
    assert next(p for p in providers if p["key"] == "imap")["auth"] == "app_password"
    assert next(p for p in providers if p["key"] == "gmail")["auth"] == "oauth"
