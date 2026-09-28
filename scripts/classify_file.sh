#!/usr/bin/env bash
#
# Classify ONE statement file with YOUR rules against a running server, and
# print the result in a readable form. This is the "give me your file and rules
# and watch it work" tool — drop in your statement and your ruleset, run it.
#
#   scripts/classify_file.sh <statement-file> <rules.json> [pdf-password]
#
# Environment:
#   BASE_URL          default http://127.0.0.1:8000
#   B2B_API_KEY       a classify:write (or analyze:write) key to use directly.
#   B2B_ADMIN_TOKEN   if B2B_API_KEY is unset, one is minted with this admin
#                     token (a throwaway classify-only client).
#
# Needs: curl, python3 (stdlib only — no extra packages).
#
# Examples:
#   BASE_URL=http://127.0.0.1:8001 B2B_ADMIN_TOKEN=... \
#     scripts/classify_file.sh mystatement.csv myrules.json
#
#   B2B_API_KEY=kl_live_xxx \
#     scripts/classify_file.sh statement.pdf rules.json 'my-pdf-password'

set -uo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[ $# -ge 2 ] || die "usage: $0 <statement-file> <rules.json> [pdf-password]"
STATEMENT="$1"; RULES="$2"; PDF_PW="${3:-}"

[ -f "$STATEMENT" ] || die "statement file not found: $STATEMENT"
[ -f "$RULES" ]     || die "rules file not found: $RULES"

# Validate the rules JSON locally first — a clearer message than a 400, and it
# saves a round trip while you are iterating on the ruleset.
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$RULES" 2>/dev/null \
  || die "rules file is not valid JSON: $RULES"

command -v curl >/dev/null    || die "curl is required"
command -v python3 >/dev/null || die "python3 is required"

# 1. Server reachable?
curl -fsS --max-time 10 "$BASE_URL/v1/health" >/dev/null 2>&1 \
  || die "cannot reach $BASE_URL/v1/health — is the server running? (python main.py)"

# 2. Resolve an API key.
KEY="${B2B_API_KEY:-}"
if [ -z "$KEY" ]; then
  [ -n "${B2B_ADMIN_TOKEN:-}" ] \
    || die "set B2B_API_KEY, or set B2B_ADMIN_TOKEN so a key can be minted."
  SLUG="classify-run-$(date +%s)"
  curl -sS --max-time 15 -X POST "$BASE_URL/internal/clients" \
    -H "X-Admin-Token: $B2B_ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d "{\"name\":\"Classify Run\",\"slug\":\"$SLUG\",\"rate_limit_per_minute\":1000}" \
    -o /dev/null 2>/dev/null
  KEY=$(curl -sS --max-time 15 -X POST "$BASE_URL/internal/clients/$SLUG/keys" \
    -H "X-Admin-Token: $B2B_ADMIN_TOKEN" -H "Content-Type: application/json" \
    -d '{"name":"classify-run","scopes":"classify:write"}' \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('secret',''))" 2>/dev/null)
  [ -n "$KEY" ] || die "could not mint an API key (check B2B_ADMIN_TOKEN)"
  echo "Minted a throwaway classify:write key for client '$SLUG'."
fi

# 3. Call /v1/classify. The rules go in the form field, read from the file.
RESP="$(mktemp)"; trap 'rm -f "$RESP"' EXIT
FORM=(-F "file=@${STATEMENT}" -F "rules=<${RULES}")
[ -n "$PDF_PW" ] && FORM+=(-F "pdf_password=${PDF_PW}")

echo "Classifying '$STATEMENT' with rules from '$RULES' ..."
HTTP=$(curl -sS --max-time 300 -X POST "$BASE_URL/v1/classify" \
  -H "Authorization: Bearer $KEY" "${FORM[@]}" \
  -o "$RESP" -w '%{http_code}')

# 4. Render the result.
python3 - "$RESP" "$HTTP" <<'PY'
import json, sys
resp, http = sys.argv[1], sys.argv[2]
try:
    d = json.load(open(resp))
except Exception:
    print(f"HTTP {http}; response was not JSON:")
    print(open(resp).read()[:2000]); sys.exit(1)

if http != "200":
    err = d.get("error", d)
    print(f"\n  REQUEST FAILED — HTTP {http}")
    print(f"  code   : {err.get('code')}")
    print(f"  message: {err.get('message')}")
    if err.get("detail"):
        print(f"  detail : {json.dumps(err['detail'])}")
    sys.exit(1)

meta = d.get("metadata", {})
summ = d.get("summary", {})
rows = d.get("data", {}).get("transactions", [])
q = d.get("quality", {})

print(f"\n  format={meta.get('detected_format')}  rows={meta.get('transaction_count')}"
      f"  rules={meta.get('rule_count')}  fallback={meta.get('fallback')}"
      f"  took={meta.get('duration_ms')}ms")

if rows:
    print("\n  " + "-" * 96)
    print(f"  {'DATE':<11}{'TYPE':<7}{'AMOUNT':>13}  {'CATEGORY':<22}{'METHOD':<9}RULE")
    print("  " + "-" * 96)
    for r in rows:
        c = r.get("classification", {})
        amt = r.get("amount")
        amt_s = f"{amt:,.2f}" if isinstance(amt, (int, float)) else str(amt)
        print(f"  {str(r.get('date','')):<11}{str(r.get('type','')):<7}{amt_s:>13}  "
              f"{str(r.get('category'))[:21]:<22}{str(c.get('method',''))[:8]:<9}"
              f"{c.get('rule_id') or ''}")
        desc = (r.get("description") or "")[:92]
        print(f"      \033[2m{desc}\033[0m")

print("\n  SUMMARY")
print(f"    classified   : {summ.get('classified')}/{summ.get('transaction_count')}"
      f"  (coverage {summ.get('coverage')})")
print(f"    by method    : {summ.get('by_method')}")
print(f"    by category  : {summ.get('by_category')}")
if summ.get("ambiguous_count"):
    print(f"    AMBIGUOUS    : {summ.get('ambiguous_count')} row(s) — "
          f"rules tied at the same priority with different categories")
never = summ.get("rules_that_never_matched") or []
if never:
    print(f"    never matched: {never}   <- likely typos in those rules' terms")
missed = summ.get("unmatched_samples") or []
if missed:
    print("    unmatched narrations (write your next rule for these):")
    for m in missed[:15]:
        print(f"        · {m}")
if q.get("warnings"):
    print(f"    quality warn : {q.get('warnings')}")
print()
PY
RC=$?
[ "$RC" -eq 0 ] && echo "Done." || echo "See the error above."
exit $RC
