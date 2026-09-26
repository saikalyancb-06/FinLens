#!/usr/bin/env bash
#
# End-to-end smoke test for POST /v1/classify against a RUNNING server.
#
# Run this before a deploy. It starts from nothing and proves the whole path:
# admin key issuance -> scope enforcement -> multipart upload -> parse ->
# rule evaluation -> response shape. Every step asserts a value, so a green run
# means the service classified real rows correctly, not merely that it answered.
#
#   BASE_URL=http://localhost:8000 \
#   B2B_ADMIN_TOKEN=... \
#   scripts/smoke_classify_api.sh
#
# Needs: curl, python3 (for JSON assertions — no jq dependency).

set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
ADMIN_TOKEN="${B2B_ADMIN_TOKEN:-}"
SLUG="${SLUG:-smoke-classify-$(date +%s)}"

PASS=0
FAIL=0
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

c_green() { printf '\033[32m%s\033[0m\n' "$1"; }
c_red()   { printf '\033[31m%s\033[0m\n' "$1"; }
c_dim()   { printf '\033[2m%s\033[0m\n' "$1"; }

ok()   { PASS=$((PASS+1)); c_green "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); c_red   "  FAIL  $1"; [ -n "${2:-}" ] && c_dim "        $2"; }

# assert_json <file> <python-expr-on-`d`> <description>
#   The expression is evaluated with the parsed body bound to `d`. Kept in
#   python rather than jq so the script has no dependency beyond the stdlib.
assert_json() {
  local file="$1" expr="$2" desc="$3"
  local out
  out="$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
try:
    r=bool(eval(sys.argv[2]))
except Exception as e:
    print('EXPR-ERROR: %s: %s' % (type(e).__name__, e)); sys.exit(2)
print('TRUE' if r else 'FALSE')
" "$file" "$expr" 2>&1)"
  if [ "$out" = "TRUE" ]; then
    ok "$desc"
  else
    bad "$desc" "$out $(head -c 300 "$file")"
  fi
}

echo
echo "================================================================"
echo " /v1/classify smoke test"
echo " target: $BASE_URL"
echo "================================================================"
echo

# ---------------------------------------------------------------- 0. liveness
echo "[0] server reachable"
if ! curl -fsS --max-time 10 "$BASE_URL/v1/health" -o "$WORKDIR/health.json" 2>/dev/null; then
  c_red "  Cannot reach $BASE_URL/v1/health — is the server running?"
  c_dim "  Start it with:  python main.py"
  exit 1
fi
assert_json "$WORKDIR/health.json" "d['status']=='healthy'" "GET /v1/health is healthy"

# ------------------------------------------------- 1. the contract is public
echo
echo "[1] the rules contract is readable without a key"
curl -fsS --max-time 10 "$BASE_URL/v1/classify/schema" -o "$WORKDIR/schema.json" 2>/dev/null
assert_json "$WORKDIR/schema.json" "d['limits']['max_rules']>0" \
  "GET /v1/classify/schema needs no credentials"
assert_json "$WORKDIR/schema.json" \
  "'amount' not in d['rule']['set']['settable_fields']" \
  "schema states a rule cannot write 'amount'"

# ----------------------------------------------------------- 2. issue a key
echo
echo "[2] issue a classify-only API key"
if [ -z "$ADMIN_TOKEN" ]; then
  c_red "  B2B_ADMIN_TOKEN is not set — cannot create a client."
  c_dim "  export B2B_ADMIN_TOKEN=\$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  c_dim "  ...and set the same value in the server's environment, then restart it."
  exit 1
fi

curl -sS --max-time 15 -X POST "$BASE_URL/internal/clients" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d "{\"name\":\"Smoke Test\",\"slug\":\"$SLUG\",\"contact_email\":\"smoke@example.com\",\"rate_limit_per_minute\":1000}" \
  -o "$WORKDIR/client.json" >/dev/null 2>&1
assert_json "$WORKDIR/client.json" "d.get('slug')=='$SLUG' or d.get('client',{}).get('slug')=='$SLUG'" \
  "created client '$SLUG'"

# Scoped to classify:write ONLY — this is what the sister product should hold.
curl -sS --max-time 15 -X POST "$BASE_URL/internal/clients/$SLUG/keys" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"smoke-classify-only","scopes":"classify:write"}' \
  -o "$WORKDIR/key.json" >/dev/null 2>&1
assert_json "$WORKDIR/key.json" "d['secret'].startswith('kl_')" \
  "issued a classify:write-only key"

KEY="$(python3 -c "import json;print(json.load(open('$WORKDIR/key.json')).get('secret',''))" 2>/dev/null)"
if [ -z "$KEY" ]; then
  c_red "  Could not read the key secret; aborting."
  cat "$WORKDIR/key.json"
  exit 1
fi

# --------------------------------------------------------- 3. the statement
cat > "$WORKDIR/statement.csv" <<'CSV'
Date,Narration,Debit,Credit,Balance
2026-03-01,SALARY CREDIT MARCH PAYROLL,,78000,178000.00
2026-03-02,UPI-SWIGGY-ORDER-8821,450,,177550.00
2026-03-03,UPI TRANSFER TO SWIGGY WALLET,900,,176650.00
2026-03-04,UPI-NAMMAYATRI-99112233,120,,176530.00
2026-03-05,NEFT-HDFC0001234-MEYER ORGANICS,250000,,-73470.00
2026-03-06,DOMINOS PIZZA REFUND CREDIT,,300,-73170.00
2026-03-07,SERVICE CHARGE AMC ANNUAL,590,,-73760.00
2026-03-08,SOMETHING COMPLETELY UNKNOWN,42,,-73802.00
CSV

cat > "$WORKDIR/rules.json" <<'JSON'
{
  "version": "smoke-1",
  "default_category": "Unclassified",
  "fallback": "none",
  "rules": [
    {"id": "payroll", "category": "Payroll", "priority": 100,
     "match": {"any_of": ["SALARY", "PAYROLL"], "direction": "credit",
               "min_amount": 10000},
     "set": {"category_path": "Expenses > Payroll"}},
    {"id": "food", "category": "Food", "priority": 100,
     "match": {"any_of": ["SWIGGY", "DOMINOS"], "none_of": ["REFUND"]}},
    {"id": "wallet-transfer", "category": "Transfer", "priority": 120,
     "match": {"all_of": ["TRANSFER"], "any_of": ["SWIGGY", "PAYTM"]}},
    {"id": "rides", "category": "Transport", "priority": 100,
     "match": {"any_of": ["NAMMAYATRI", "UBER"]}},
    {"id": "vendor-neft", "category": "Vendor Payment", "priority": 80,
     "match": {"regex": "^NEFT-[A-Z]{4}[0-9]{7}", "min_amount": 100000}},
    {"id": "bank-fees", "category": "Bank Charges", "priority": 90,
     "match": {"any_of": ["SERVICE CHARGE", "AMC"], "none_of": ["REVERSAL"],
               "max_amount": 2000}},
    {"id": "never-fires", "category": "Nothing", "priority": 10,
     "match": {"any_of": ["ZZZ_NO_SUCH_MERCHANT_ZZZ"]}}
  ]
}
JSON

# ------------------------------------------------------------- 4. classify
echo
echo "[3] classify the statement"
HTTP=$(curl -sS --max-time 120 -X POST "$BASE_URL/v1/classify" \
  -H "Authorization: Bearer $KEY" \
  -F "file=@$WORKDIR/statement.csv;type=text/csv" \
  -F "rules=<$WORKDIR/rules.json" \
  -o "$WORKDIR/result.json" -w '%{http_code}' 2>/dev/null)

if [ "$HTTP" != "200" ]; then
  bad "POST /v1/classify returned HTTP $HTTP" "$(head -c 500 "$WORKDIR/result.json")"
else
  ok "POST /v1/classify returned 200"

  R="$WORKDIR/result.json"
  # A tiny helper so the assertions below read like the statement.
  cat > "$WORKDIR/cat.py" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
rows = {r["description"]: r for r in d["data"]["transactions"]}
print(rows.get(sys.argv[2], {}).get("category"))
PY
  check_cat() {
    local desc="$1" want="$2"
    local got
    got="$(python3 "$WORKDIR/cat.py" "$R" "$desc" 2>&1)"
    if [ "$got" = "$want" ]; then ok "'$desc' -> $want"
    else bad "'$desc' -> expected $want, got $got"; fi
  }

  echo
  echo "    per-row classification"
  check_cat "SALARY CREDIT MARCH PAYROLL"     "Payroll"
  check_cat "UPI-SWIGGY-ORDER-8821"           "Food"
  # priority 120 beats the merchant rule at 100
  check_cat "UPI TRANSFER TO SWIGGY WALLET"   "Transfer"
  # term matched inside a concatenated token
  check_cat "UPI-NAMMAYATRI-99112233"         "Transport"
  check_cat "NEFT-HDFC0001234-MEYER ORGANICS" "Vendor Payment"
  # none_of:[REFUND] vetoes the food rule -> falls to the default
  check_cat "DOMINOS PIZZA REFUND CREDIT"     "Unclassified"
  check_cat "SERVICE CHARGE AMC ANNUAL"       "Bank Charges"
  check_cat "SOMETHING COMPLETELY UNKNOWN"    "Unclassified"

  echo
  echo "    summary and provenance"
  assert_json "$R" "d['summary']['transaction_count']==8" "all 8 rows returned"
  assert_json "$R" "d['summary']['by_method']=={'rule':6,'default':2}" \
    "6 rows by rule, 2 by default"
  assert_json "$R" "d['summary']['rules_that_never_matched']==['never-fires']" \
    "the dead rule is reported as never matching"
  assert_json "$R" "'SOMETHING COMPLETELY UNKNOWN' in d['summary']['unmatched_samples']" \
    "unmatched narrations are sampled for writing the next rule"
  assert_json "$R" "d['metadata']['ruleset_version']=='smoke-1'" \
    "ruleset version echoed back"
  assert_json "$R" "d['summary']['ambiguous_count']==0" "no priority collisions"

  echo
  echo "    the invariant: a rule cannot change the money"
  assert_json "$R" \
    "[r for r in d['data']['transactions'] if r['description']=='NEFT-HDFC0001234-MEYER ORGANICS'][0]['amount']==250000.0" \
    "amount is still 250000.00 after classification"
  assert_json "$R" \
    "[r for r in d['data']['transactions'] if r['description']=='SALARY CREDIT MARCH PAYROLL'][0]['type']=='CREDIT'" \
    "direction is unchanged"
  assert_json "$R" \
    "sum(r['amount'] for r in d['data']['transactions'])==78000+450+900+120+250000+300+590+42" \
    "total of all amounts matches the statement exactly"
fi

# ------------------------------------------------- 5. scope is enforced
echo
echo "[4] the classify-only key cannot reach full financial analysis"
HTTP=$(curl -sS --max-time 60 -X POST "$BASE_URL/v1/analyze" \
  -H "Authorization: Bearer $KEY" \
  -F "file=@$WORKDIR/statement.csv;type=text/csv" \
  -o "$WORKDIR/denied.json" -w '%{http_code}' 2>/dev/null)
if [ "$HTTP" = "403" ]; then
  ok "POST /v1/analyze refused with 403 for a classify-only key"
else
  bad "POST /v1/analyze returned HTTP $HTTP, expected 403" \
      "$(head -c 300 "$WORKDIR/denied.json")"
fi

# ------------------------------------------------- 6. a bad ruleset is caught
echo
echo "[5] a malformed ruleset is rejected clearly, before the file is read"
HTTP=$(curl -sS --max-time 30 -X POST "$BASE_URL/v1/classify" \
  -H "Authorization: Bearer $KEY" \
  -F "file=@$WORKDIR/statement.csv;type=text/csv" \
  -F 'rules={"rules":[{"id":"typo","category":"C","match":{"any_off":["X"]}}]}' \
  -o "$WORKDIR/badrules.json" -w '%{http_code}' 2>/dev/null)
if [ "$HTTP" = "400" ]; then
  ok "a typo'd condition key is a 400, not a rule that matches everything"
  assert_json "$WORKDIR/badrules.json" "d['error']['code']=='INVALID_RULES'" \
    "error code is INVALID_RULES"
  assert_json "$WORKDIR/badrules.json" "d['error']['detail']['rule']=='rules[0]'" \
    "the error points at the offending rule"
else
  bad "malformed ruleset returned HTTP $HTTP, expected 400" \
      "$(head -c 300 "$WORKDIR/badrules.json")"
fi

HTTP=$(curl -sS --max-time 30 -X POST "$BASE_URL/v1/classify" \
  -H "Authorization: Bearer $KEY" \
  -F "file=@$WORKDIR/statement.csv;type=text/csv" \
  -F 'rules={"rules":[{"id":"oops","category":"Everything"}]}' \
  -o "$WORKDIR/catchall.json" -w '%{http_code}' 2>/dev/null)
if [ "$HTTP" = "400" ]; then
  ok "a rule with no conditions is refused unless catch_all is explicit"
else
  bad "conditionless rule returned HTTP $HTTP, expected 400"
fi

# ----------------------------------------------------------------- verdict
echo
echo "================================================================"
if [ "$FAIL" -eq 0 ]; then
  c_green " ALL $PASS CHECKS PASSED"
  echo " The classification service is ready. Key used was scoped"
  echo " classify:write only; revoke it with:"
  echo "   curl -X POST $BASE_URL/internal/clients/$SLUG/disable \\"
  echo "     -H \"X-Admin-Token: \$B2B_ADMIN_TOKEN\""
  echo "================================================================"
  exit 0
else
  c_red " $FAIL CHECK(S) FAILED, $PASS passed"
  echo " Full last response body: $WORKDIR/result.json (removed on exit —"
  echo " re-run with 'trap - EXIT' edited out to keep it)"
  echo "================================================================"
  exit 1
fi
