#!/usr/bin/env bash
# Deploy smoke (AC-020): upload the committed smoke fixture, poll to ready,
# and verify real extraction content (clause ids L.1/L.2/M.1 + "shall" in
# row bodies) — not just a state check.
#
#   ./scripts/smoke.sh <url> [<fixture>]
#
# Exit 0 when health check passes and the fixture reaches ready with
# content-verified rows; exit 1 on failure.
set -euo pipefail

BASE_URL="${1:?usage: smoke.sh <url> [<fixture>]}"
FIXTURE="${2:-tests/fixtures/corpus/smoke_fixture.pdf}"
COOKIE_JAR="$(mktemp)"
MATRIX_JSON="$(mktemp)"
trap 'rm -f "$COOKIE_JAR" "$MATRIX_JSON"' EXIT

echo "== health check =="
curl -fsS "$BASE_URL/healthz"
echo

EMAIL="smoke-$(date +%s)@example.com"
PASSWORD="smoke-password-123"

echo "== create smoke account =="
CSRF=$(curl -fsS -c "$COOKIE_JAR" -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" \
  "$BASE_URL/signup" | python3 -c 'import json,sys; print(json.load(sys.stdin)["csrf_token"])')
echo "account created: $EMAIL"

echo "== upload fixture: $FIXTURE =="
JOB_URL=$(curl -fsS -b "$COOKIE_JAR" -c "$COOKIE_JAR" -o /dev/null -w '%{redirect_url}' \
  -H "X-CSRF-Token: $CSRF" -F "file=@$FIXTURE" "$BASE_URL/app/new")
RFP_ID="${JOB_URL##*/}"
echo "rfp id: $RFP_ID"

echo "== poll to ready =="
for i in $(seq 1 120); do
  STAGE=$(curl -fsS -b "$COOKIE_JAR" "$BASE_URL/api/jobs/$RFP_ID" | python3 -c 'import json,sys; print(json.load(sys.stdin)["stage"])')
  echo "  stage: $STAGE"
  if [ "$STAGE" = "ready" ]; then break; fi
  if [ "$STAGE" = "failed" ]; then echo "FAIL: extraction failed" >&2; exit 1; fi
  sleep 5
done
if [ "$STAGE" != "ready" ]; then echo "FAIL: timed out waiting for ready" >&2; exit 1; fi

echo "== verify extracted content =="
curl -fsS -b "$COOKIE_JAR" "$BASE_URL/api/matrices/$RFP_ID" > "$MATRIX_JSON"
python3 - "$MATRIX_JSON" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as fh:
    payload = json.load(fh)
rows = payload.get("rows", [])
clauses = {r.get("clause_id") for r in rows}
missing = {"l.1", "l.2", "m.1"} - clauses
if missing:
    print(f"FAIL: expected clause ids missing: {sorted(missing)} (got {sorted(clauses)})", file=sys.stderr)
    sys.exit(1)
if not any("shall" in (r.get("body") or "").lower() for r in rows):
    print("FAIL: no row body contains 'shall' — extraction looks like a no-op", file=sys.stderr)
    sys.exit(1)
print(f"content verified: {len(rows)} rows, clauses {sorted(clauses)} present, 'shall' found")
PY

echo "SMOKE OK"
