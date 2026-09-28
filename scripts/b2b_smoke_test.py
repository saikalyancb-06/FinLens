"""End-to-end smoke test for the B2B Financial Analysis API (/v1).

Runs against a LIVE server — local or deployed — exactly as an integrator
would call it, and exits non-zero if anything is wrong. Use it:

  * locally before a deploy:   python scripts/b2b_smoke_test.py
  * against production after:  python scripts/b2b_smoke_test.py \
        --base-url https://<your-app>.onrender.com --admin-token <B2B_ADMIN_TOKEN>

What it does (every step is an assertion, not a print):
  1. health / ready / version / formats
  2. creates a throwaway client `smoke-<timestamp>` and issues it a key
     (disabled again at the end, so it cannot be used afterwards)
  3. analyses the same statement as CSV, TSV, JSON, OFX, XLSX and PDF and checks
     every format produces the same totals — 24 rows, credits 468000, debits
     273000, salary 78000 — and that every row carries a category
  4. loan affordability (EMI 11122.22), content-based detection (a CSV named
     .pdf), idempotent replay + key-reuse conflict, async + poll, include_
     transactions=false, password-protected PDF (none / wrong / right)
  5. every documented error: no key, bad key, zip, partial loan params, empty
     file, unknown request id, wrong scope, revoked key, per-client rate limit

Needs only what the app already installs (httpx, pandas, openpyxl, fpdf2,
pymupdf). Writes nothing outside a temp directory.
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "postman" / "samples"

EXPECTED_ROWS = 24
EXPECTED_CREDITS = 468000.0
EXPECTED_DEBITS = 273000.0
EXPECTED_SALARY = 78000.0
EXPECTED_EMI = 11122.22

_results: list = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    _results.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    return bool(cond)


def val(block, key):
    """Metric value from a provenance block ({"value": ..., "source": ...})."""
    m = (block or {}).get(key)
    return m.get("value") if isinstance(m, dict) else m


# ----------------------------------------------------------------- fixtures

def make_inputs(tmp: Path) -> dict:
    """Derive XLSX, PDF, encrypted PDF and edge-case files from the CSV sample."""
    import pandas as pd

    csv_path = SAMPLES / "sample_statement.csv"
    df = pd.read_csv(csv_path)
    files = {
        "csv": csv_path,
        "tsv": SAMPLES / "sample_statement.tsv",
        "json": SAMPLES / "sample_statement.json",
        "ofx": SAMPLES / "sample_statement.ofx",
    }

    xlsx = tmp / "statement.xlsx"
    df.to_excel(xlsx, index=False)
    files["xlsx"] = xlsx

    # A digital PDF with a real table layout, generated from the same rows.
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=8)
    pdf.cell(0, 8, "ACME BANK - ACCOUNT STATEMENT", new_x="LMARGIN", new_y="NEXT")
    widths = (24, 80, 26, 26, 30)
    for i, col in enumerate(df.columns):
        pdf.cell(widths[i], 6, str(col), border=1)
    pdf.ln()
    for _, row in df.iterrows():
        cells = [str(row["Date"]), str(row["Narration"]),
                 "" if pd.isna(row["Debit"]) else f"{row['Debit']:.2f}",
                 "" if pd.isna(row["Credit"]) else f"{row['Credit']:.2f}",
                 f"{row['Balance']:.2f}"]
        for i, c in enumerate(cells):
            pdf.cell(widths[i], 6, c, border=1)
        pdf.ln()
    pdf_path = tmp / "statement.pdf"
    pdf.output(str(pdf_path))
    files["pdf"] = pdf_path

    import fitz  # pymupdf
    enc = tmp / "statement_locked.pdf"
    doc = fitz.open(str(pdf_path))
    doc.save(str(enc), encryption=fitz.PDF_ENCRYPT_AES_256,
             owner_pw="owner-secret", user_pw="1234")
    doc.close()
    files["pdf_locked"] = enc

    # CSV content under a .pdf name: detection must go by content.
    disguised = tmp / "really_a_csv.pdf"
    disguised.write_bytes(csv_path.read_bytes())
    files["csv_as_pdf"] = disguised

    empty = tmp / "empty.csv"
    empty.write_bytes(b"")
    files["empty"] = empty

    files["zip"] = SAMPLES / "not_supported.zip"
    return files


# ------------------------------------------------------------------- client

class Api:
    def __init__(self, base: str, admin_token: str, timeout: float = 180.0):
        self.base = base.rstrip("/")
        self.admin = {"X-Admin-Token": admin_token}
        self.http = httpx.Client(base_url=self.base, timeout=timeout)

    def analyze(self, key, path, data=None, headers=None, name=None):
        h = {"Authorization": f"Bearer {key}"} if key else {}
        h.update(headers or {})
        with open(path, "rb") as fh:
            return self.http.post("/v1/analyze", headers=h, data=data or {},
                                  files={"file": (name or Path(path).name, fh)})


def assert_statement(label: str, r: httpx.Response, expect_recon=("PASSED",)):
    ok = check(f"{label}: 200", r.status_code == 200,
               f"{r.status_code} {r.text[:300]}")
    if not ok:
        return None
    body = r.json()
    d = body["data"]
    txns = d.get("transactions") or []
    credits = val(d.get("cashflow"), "total_credits") or val(d.get("income"), "total_credits")
    debits = val(d.get("cashflow"), "total_debits") or val(d.get("expenses"), "total_debits")
    salary = val(d.get("income"), "salary")
    check(f"{label}: {EXPECTED_ROWS} transactions", len(txns) == EXPECTED_ROWS, str(len(txns)))
    check(f"{label}: total credits {EXPECTED_CREDITS:.0f}",
          credits is not None and abs(float(credits) - EXPECTED_CREDITS) < 0.01, str(credits))
    check(f"{label}: total debits {EXPECTED_DEBITS:.0f}",
          debits is not None and abs(float(debits) - EXPECTED_DEBITS) < 0.01, str(debits))
    check(f"{label}: salary {EXPECTED_SALARY:.0f}",
          salary is not None and abs(float(salary) - EXPECTED_SALARY) < 0.01, str(salary))
    uncategorised = [t for t in txns if not t.get("category")]
    check(f"{label}: every row classified", not uncategorised,
          f"{len(uncategorised)} rows without category")
    recon = body["quality"]["reconciliation_status"]
    check(f"{label}: reconciliation {'/'.join(expect_recon)}", recon in expect_recon, recon)
    degraded = [w for w in body["quality"]["warnings"] if w.get("code") == "CLASSIFIER_DEGRADED"]
    check(f"{label}: ML classifier loaded (no CLASSIFIER_DEGRADED)", not degraded)
    return body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.getenv("BASE_URL", "http://localhost:8000"))
    ap.add_argument("--admin-token", default=os.getenv("B2B_ADMIN_TOKEN", ""))
    ap.add_argument("--keep-client", action="store_true",
                    help="leave the smoke client enabled afterwards")
    args = ap.parse_args()
    if not args.admin_token:
        print("Set --admin-token or B2B_ADMIN_TOKEN (the server's admin token).")
        return 2

    api = Api(args.base_url, args.admin_token)
    tmp = Path(tempfile.mkdtemp(prefix="b2b_smoke_"))
    files = make_inputs(tmp)
    slug = "smoke-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S")

    print(f"\n== Operations ({api.base})")
    r = api.http.get("/v1/health"); check("GET /v1/health 200", r.status_code == 200)
    r = api.http.get("/v1/ready")
    check("GET /v1/ready 200 + database ok",
          r.status_code == 200 and r.json()["checks"]["database"] == "ok", r.text)
    r = api.http.get("/v1/version"); check("GET /v1/version", r.status_code == 200)
    r = api.http.get("/v1/formats")
    check("GET /v1/formats lists pdf/xlsx/csv",
          r.status_code == 200 and {"pdf", "xlsx", "csv"} <= {
              f.get("format") or f.get("id") for f in r.json()["supported"]}
          if isinstance(r.json().get("supported"), list) else r.status_code == 200)

    print("\n== Admin: client + keys")
    r = api.http.post("/internal/clients", headers=api.admin,
                      json={"name": "Smoke Test", "slug": slug,
                            "rate_limit_per_minute": 1000})
    if not check("create client 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}"):
        return summary()
    r = api.http.post(f"/internal/clients/{slug}/keys", headers=api.admin, json={"name": "smoke"})
    check("issue key 201", r.status_code == 201, r.text[:200])
    key = r.json().get("secret") or r.json().get("api_key") or r.json().get("key")
    if not check("key secret returned once", bool(key), json.dumps(r.json())[:300]):
        return summary()
    r = api.http.post(f"/internal/clients/{slug}/keys", headers=api.admin,
                      json={"name": "read-only", "scopes": "analyze:read"})
    ro_key = (r.json().get("secret") or r.json().get("api_key") or r.json().get("key"))
    r = api.http.post("/internal/clients", headers={"X-Admin-Token": "wrong-token-xxxxxxxx"},
                      json={"name": "x", "slug": "x"})
    check("admin with wrong token -> 401", r.status_code == 401, str(r.status_code))

    print("\n== Same statement, six formats")
    for fmt in ("csv", "tsv", "json", "xlsx", "pdf"):
        assert_statement(fmt.upper(), api.analyze(key, files[fmt]))
    assert_statement("OFX", api.analyze(key, files["ofx"]), expect_recon=("NOT_VERIFIABLE",))

    print("\n== Features")
    r = api.analyze(key, files["csv"], data={"loan_amount": "500000",
                                            "interest_rate": "12", "tenure_months": "60"})
    emi = val((r.json().get("data") or {}).get("loan"), "proposed_emi") if r.status_code == 200 else None
    check(f"loan: proposed EMI {EXPECTED_EMI}", emi is not None and abs(float(emi) - EXPECTED_EMI) < 0.01, str(emi))
    check("loan: affordability block present",
          r.status_code == 200 and bool(r.json()["data"].get("affordability")))

    r = api.analyze(key, files["csv_as_pdf"])
    check("CSV named .pdf detected by content",
          r.status_code == 200 and r.json()["metadata"]["detected_format"] == "csv",
          f"{r.status_code} {r.text[:200]}")

    r = api.analyze(key, files["csv"], data={"include_transactions": "false"})
    check("include_transactions=false omits rows",
          r.status_code == 200 and r.json()["data"]["transactions"] == [])

    idem = {"Idempotency-Key": f"{slug}-idem"}
    r1 = api.analyze(key, files["csv"], headers=idem)
    r2 = api.analyze(key, files["csv"], headers=idem)
    check("idempotent replay returns same request_id",
          r1.status_code == r2.status_code == 200
          and r1.json()["request_id"] == r2.json()["request_id"]
          and r2.headers.get("Idempotent-Replay") == "true")
    r3 = api.analyze(key, files["tsv"], headers=idem)
    check("same key, different file -> 409 IDEMPOTENCY_KEY_REUSED",
          r3.status_code == 409 and r3.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED",
          f"{r3.status_code} {r3.text[:200]}")

    r = api.analyze(key, files["csv"], data={"async_mode": "true"})
    ok = check("async -> 202", r.status_code == 202, f"{r.status_code} {r.text[:200]}")
    if ok:
        rid = r.json()["request_id"]
        body = None
        for _ in range(60):
            p = api.http.get(f"/v1/analyze/{rid}", headers={"Authorization": f"Bearer {key}"})
            body = p.json()
            if body.get("status") in ("completed", "failed"):
                break
            time.sleep(1)
        check("async poll -> completed with data",
              body and body.get("status") == "completed"
              and len(body["data"]["transactions"]) == EXPECTED_ROWS, str(body)[:200])
        p = api.http.get(f"/v1/analyze/{rid}", headers={"Authorization": f"Bearer {ro_key}"})
        check("read-only key can poll", p.status_code == 200, str(p.status_code))

    r = api.analyze(key, files["pdf_locked"])
    check("locked PDF, no password -> 422 PDF_PASSWORD_REQUIRED",
          r.status_code == 422 and r.json()["error"]["code"] == "PDF_PASSWORD_REQUIRED",
          f"{r.status_code} {r.text[:200]}")
    r = api.analyze(key, files["pdf_locked"], data={"pdf_password": "nope"})
    check("locked PDF, wrong password -> 422 PDF_PASSWORD_INVALID",
          r.status_code == 422 and r.json()["error"]["code"] == "PDF_PASSWORD_INVALID",
          f"{r.status_code} {r.text[:200]}")
    assert_statement("locked PDF, right password",
                     api.analyze(key, files["pdf_locked"], data={"pdf_password": "1234"}))

    print("\n== Errors")
    def code_is(r, status, code):
        try:
            got = r.json()["error"]["code"]
        except Exception:
            got = None
        return r.status_code == status and got == code, f"{r.status_code} {got} {r.text[:150]}"

    for label, resp, status, code in (
        ("no key", api.analyze(None, files["csv"]), 401, "MISSING_API_KEY"),
        ("bad key", api.analyze("kl_live_notarealkey000000000000000", files["csv"]), 401, "INVALID_API_KEY"),
        ("zip", api.analyze(key, files["zip"]), 415, "UNSUPPORTED_FILE_FORMAT"),
        ("partial loan", api.analyze(key, files["csv"], data={"loan_amount": "1"}), 400, "INVALID_PARAMETER"),
        ("empty file", api.analyze(key, files["empty"]), 422, "FILE_EMPTY"),
        ("read-only key cannot submit", api.analyze(ro_key, files["csv"]), 403, "INSUFFICIENT_SCOPE"),
    ):
        ok, det = code_is(resp, status, code)
        check(f"{label} -> {status} {code}", ok, det)
    r = api.http.get("/v1/analyze/req_does_not_exist", headers={"Authorization": f"Bearer {key}"})
    ok, det = code_is(r, 404, "REQUEST_NOT_FOUND"); check("unknown request id -> 404", ok, det)

    # Revocation
    keys = api.http.get(f"/internal/clients/{slug}/keys", headers=api.admin).json()
    keys = keys.get("keys", keys) if isinstance(keys, dict) else keys
    ro = [k for k in keys if k.get("name") == "read-only"]
    if ro:
        api.http.post(f"/internal/clients/{slug}/keys/{ro[0]['id']}/revoke", headers=api.admin)
        r = api.http.get("/v1/usage", headers={"Authorization": f"Bearer {ro_key}"})
        ok, det = code_is(r, 401, "REVOKED_API_KEY"); check("revoked key -> 401 REVOKED_API_KEY", ok, det)

    # Per-client rate limit, on a second throwaway client limited to 2/min.
    slug2 = slug + "-rl"
    api.http.post("/internal/clients", headers=api.admin,
                  json={"name": "Smoke RL", "slug": slug2, "rate_limit_per_minute": 2})
    k2 = api.http.post(f"/internal/clients/{slug2}/keys", headers=api.admin, json={}).json()
    k2 = k2.get("secret") or k2.get("api_key") or k2.get("key")
    codes = [api.analyze(k2, files["csv"]).status_code for _ in range(3)]
    last = api.analyze(k2, files["csv"])
    ok, det = code_is(last, 429, "RATE_LIMIT_EXCEEDED")
    check("per-client rate limit -> 429 RATE_LIMIT_EXCEEDED + Retry-After",
          codes[:2] == [200, 200] and ok and "Retry-After" in last.headers, f"{codes} {det}")

    r = api.http.get("/v1/usage", headers={"Authorization": f"Bearer {key}"})
    check("GET /v1/usage", r.status_code == 200, r.text[:200])

    if not args.keep_client:
        for s in (slug, slug2):
            api.http.post(f"/internal/clients/{s}/disable", headers=api.admin)
        r = api.http.get("/v1/usage", headers={"Authorization": f"Bearer {key}"})
        check("disabled client -> 403 CLIENT_DISABLED",
              r.status_code == 403, f"{r.status_code} {r.text[:150]}")

    return summary()


def summary() -> int:
    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)} passed, {len(failed)} failed")
    for name, _, detail in failed:
        print(f"  FAIL {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
