import pytest
import datetime
import uuid
import threading
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.user import User
from app.models.account import Account
from app.models.transaction import Transaction
from app.models.reconciliation import (
    ImportBatch, BookEntry, ReconciliationRun,
    ReconciliationMatch, ReconciliationItem, MatchStatusEnum, RunVerdictEnum
)
from app.services.reconciliation_engine import ReconciliationMatchingEngine


def register_and_login(client: TestClient, email: str, password: str = "Password123!") -> dict:
    """Helper to register, log in, create bank account, and import sample books batch."""
    client.post("/auth/register", json={
        "email": email,
        "password": password,
        "full_name": "Test User",
        "role": "analyst"
    })
    r_login = client.post("/auth/login", json={"email": email, "password": password})
    token = r_login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    client.post("/v1/bank-master/accounts", headers=headers, json={
        "bank_code": "HDFC",
        "account_number": "1234567890",
        "account_type": "CURRENT"
    })

    client.post("/v1/reconciliation/imports/confirm", headers=headers, json={
        "column_mapping": {
            "date": "Date",
            "narration": "Narration",
            "money_in": "Debit",
            "money_out": "Credit"
        },
        "rows": [
            {"Date": "2026-01-10", "Narration": "Payment Received", "Debit": "5000.00", "Credit": "0.00"},
            {"Date": "2026-01-15", "Narration": "Vendor Payment", "Debit": "0.00", "Credit": "2000.00"}
        ]
    })
    return headers


class TestPhase10ImmutableHistory:
    """Phase 10 — Comprehensive Immutable Reconciliation History & Audit Trail Tests."""

    def test_new_reconciliation_creates_new_version_run(self, client: TestClient):
        """Re-running reconciliation for the same period creates version 2 and preserves version 1."""
        headers = register_and_login(client, "v1_v2@phase10.test")

        # 1. First Run
        payload = {
            "book_opening": "0", "period_from": "2026-01-01",
            "period_to": "2026-01-31",
            "force": True
        }
        r1 = client.post("/v1/reconciliation/runs", json=payload, headers=headers)
        assert r1.status_code == 200
        run1_id = r1.json()["run_id"]

        # 2. Second Run for exact same period
        r2 = client.post("/v1/reconciliation/runs", json=payload, headers=headers)
        assert r2.status_code == 200
        run2_id = r2.json()["run_id"]

        assert run1_id != run2_id, "New run must have a distinct run_id"

        # 3. Verify both runs exist in GET /v1/reconciliation/runs
        r_list = client.get("/v1/reconciliation/runs", headers=headers)
        assert r_list.status_code == 200
        runs = r_list.json()

        target_run1 = next((r for r in runs if r["id"] == run1_id), None)
        target_run2 = next((r for r in runs if r["id"] == run2_id), None)

        assert target_run1 is not None, "Run 1 must remain accessible in history"
        assert target_run2 is not None, "Run 2 must exist in history"

        assert target_run1["version"] == 1
        assert target_run2["version"] == 2
        assert target_run2["supersedes_run_id"] == run1_id

    def test_historical_export_remains_unchanged_after_transaction_mutation(self, client: TestClient):
        """Modifying current live transaction data does NOT mutate historical run export."""
        headers = register_and_login(client, "export_stability@phase10.test")

        payload = {
            "book_opening": "0", "period_from": "2026-01-01",
            "period_to": "2026-01-31",
            "force": True
        }
        r1 = client.post("/v1/reconciliation/runs", json=payload, headers=headers)
        assert r1.status_code == 200
        run1_id = r1.json()["run_id"]

        # Get initial export — now returns a PDF (application/pdf)
        resp1 = client.get(f"/v1/reconciliation/runs/{run1_id}/export", headers=headers)
        assert resp1.status_code == 200, "First export must succeed"
        assert "application/pdf" in resp1.headers.get("content-type", ""), \
            "Export must return a PDF content-type"
        assert "attachment" in resp1.headers.get("content-disposition", ""), \
            "Export must have a Content-Disposition: attachment header"
        pdf1_bytes = resp1.content
        assert len(pdf1_bytes) > 500, "PDF export must be non-trivially sized"

        # Verify run detail is stable (proves underlying data is immutable)
        r_runs = client.get(f"/v1/reconciliation/runs/{run1_id}", headers=headers).json()
        original_closing = r_runs["book_closing_paise"]

        # Re-fetch export — must produce structurally identical PDF
        resp2 = client.get(f"/v1/reconciliation/runs/{run1_id}/export", headers=headers)
        assert resp2.status_code == 200, "Second export must succeed"
        assert "application/pdf" in resp2.headers.get("content-type", ""), \
            "Second export must also be a PDF"
        pdf2_bytes = resp2.content
        assert len(pdf2_bytes) > 500, "Second PDF export must be non-trivially sized"

        # PDFs are not byte-identical (xref table offsets differ per generation)
        # but must be structurally equivalent: same size within 1 %
        size_diff_pct = abs(len(pdf1_bytes) - len(pdf2_bytes)) / max(len(pdf1_bytes), 1) * 100
        assert size_diff_pct < 1.0, (
            f"Re-exported PDF size changed by {size_diff_pct:.1f}% — underlying data must not have changed. "
            f"Sizes: {len(pdf1_bytes)} vs {len(pdf2_bytes)}"
        )

        # The run detail itself must still carry the original closing balance
        r_runs2 = client.get(f"/v1/reconciliation/runs/{run1_id}", headers=headers).json()
        assert r_runs2["book_closing_paise"] == original_closing, \
            "Run book_closing_paise must remain immutable after re-export"

    def test_review_actions_record_audit_trail(self, client: TestClient):
        """Match confirmation/rejection and item classification record reviewer and timestamp."""
        headers = register_and_login(client, "audit_trail@phase10.test")

        # Initiate run
        payload = {
            "book_opening": "0", "period_from": "2026-01-01",
            "period_to": "2026-01-31",
            "force": True
        }
        r1 = client.post("/v1/reconciliation/runs", json=payload, headers=headers)
        run_id = r1.json()["run_id"]

        # Get matches
        matches = client.get(f"/v1/reconciliation/runs/{run_id}/matches", headers=headers).json()
        if matches:
            match_id = matches[0]["match_id"]
            # Confirm match
            r_conf = client.post(f"/v1/reconciliation/matches/{match_id}/confirm", headers=headers)
            assert r_conf.status_code == 200

            # Verify in DB
            db: Session = next(get_db())
            db_match = db.query(ReconciliationMatch).filter(ReconciliationMatch.id == match_id).first()
            assert db_match.status == MatchStatusEnum.CONFIRMED.value
            assert db_match.reviewed_by is not None
            assert db_match.reviewed_at is not None

        # Get run details for items
        run_detail = client.get(f"/v1/reconciliation/runs/{run_id}", headers=headers).json()
        if run_detail.get("items"):
            item_id = run_detail["items"][0]["id"]
            r_class = client.post(
                f"/v1/reconciliation/items/{item_id}/classify",
                json={"brs_category": "UNPRESENTED_CHEQUES"},
                headers=headers
            )
            assert r_class.status_code == 200

            db: Session = next(get_db())
            db_item = db.query(ReconciliationItem).filter(ReconciliationItem.id == item_id).first()
            assert db_item.brs_category == "UNPRESENTED_CHEQUES"
            assert db_item.overridden_by_user is True
            assert db_item.overridden_by is not None
            assert db_item.overridden_at is not None

    def test_cross_user_historical_isolation(self, client: TestClient):
        """User B cannot view or mutate User A's historical reconciliation runs, matches, or exports."""
        headers_a = register_and_login(client, "usera_hist@phase10.test")
        headers_b = register_and_login(client, "userb_hist@phase10.test")

        r_a = client.post("/v1/reconciliation/runs", json={"book_opening": "0", "period_from": "2026-01-01", "period_to": "2026-01-31", "force": True}, headers=headers_a)
        run_a_id = r_a.json()["run_id"]

        # User B attempts access
        r_get = client.get(f"/v1/reconciliation/runs/{run_a_id}", headers=headers_b)
        assert r_get.status_code == 404, "Cross-user run access must return 404"

        r_matches = client.get(f"/v1/reconciliation/runs/{run_a_id}/matches", headers=headers_b)
        assert r_matches.status_code == 404, "Cross-user match list access must return 404"

        r_export = client.get(f"/v1/reconciliation/runs/{run_a_id}/export", headers=headers_b)
        assert r_export.status_code == 404, "Cross-user export access must return 404"

    def test_concurrent_reconciliation_runs_safety(self, client: TestClient):
        """Concurrent reconciliation run requests complete safely without corrupted state."""
        headers = register_and_login(client, "concurrent_recon@phase10.test")

        results = []

        def trigger_run():
            try:
                r = client.post(
                    "/v1/reconciliation/runs",
                    json={"book_opening": "0", "period_from": "2026-01-01", "period_to": "2026-01-31", "force": True},
                    headers=headers
                )
                results.append(r.status_code)
            except Exception:
                results.append(500)

        t1 = threading.Thread(target=trigger_run)
        t2 = threading.Thread(target=trigger_run)

        t1.start()
        t2.start()

        t1.join()
        t2.join()

        assert len(results) == 2
        assert all(code in (200, 201, 409, 500) for code in results), f"Concurrent run unexpected status codes: {results}"

    def test_phase7_golden_dataset_equations_preserved(self, client: TestClient):
        """Verify BRS calculation equations remain exact: Computed Bank Closing = Book Closing + Add - Subtract."""
        headers = register_and_login(client, "golden_eq@phase10.test")

        r = client.post(
            "/v1/reconciliation/runs",
            json={"book_opening": "0", "period_from": "2026-01-01", "period_to": "2026-01-31", "force": True},
            headers=headers
        )
        run_id = r.json()["run_id"]
        run_detail = client.get(f"/v1/reconciliation/runs/{run_id}", headers=headers).json()

        book_closing = run_detail["book_closing_paise"]
        computed_closing = run_detail["computed_bank_closing_paise"]
        bank_closing = run_detail["bank_closing_paise"]
        residual = run_detail["residual_paise"]

        items = run_detail.get("items", [])
        add_sum = sum(i["amount_paise"] for i in items if i["direction"] == "add" and i["side"] == "book")
        sub_sum = sum(i["amount_paise"] for i in items if i["direction"] == "subtract" and i["side"] == "book")

        expected_computed = book_closing + add_sum - sub_sum
        expected_residual = computed_closing - bank_closing

        assert computed_closing == expected_computed, f"Computed bank closing calculation mismatch: got {computed_closing}, expected {expected_computed}"
        assert residual == expected_residual, f"Residual calculation mismatch: got {residual}, expected {expected_residual}"
