#!/usr/bin/env bash
# Fast pre/post-deploy confidence check against a running server. Unlike
# tests/test_all_cards.py (which walks every card and is too slow for a quick
# check), this hits a handful of representative endpoints and exits non-zero
# with a clear message on the first failure.
#
# Usage: scripts/smoke_test.sh [-u BASE_URL]
#   BASE_URL can also be set via the $BASE_URL env var. Defaults to
#   http://localhost:8000.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"

while getopts "u:" opt; do
  case "${opt}" in
    u) BASE_URL="${OPTARG}" ;;
    *) echo "Usage: $0 [-u BASE_URL]" >&2; exit 1 ;;
  esac
done

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

echo "Smoke testing ${BASE_URL}"

# --- /v1/health ---
health_body="$(curl -sf "${BASE_URL}/v1/health")" || fail "/v1/health did not return 200"
echo "${health_body}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
if data.get('status') not in ('ok', 'degraded'):
    sys.exit('unexpected status: ' + repr(data.get('status')))
datasets = data.get('datasets') or {}
if not datasets:
    sys.exit('no datasets reported')
if not any((d or {}).get('entries', 0) > 0 for d in datasets.values()):
    sys.exit('all datasets have zero entries')
" || fail "/v1/health response failed validation: ${health_body}"
echo "OK: /v1/health"

# --- known-good single lookup ---
lookup_code="$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/v1/cards/lightningbolt")"
[ "${lookup_code}" = "200" ] || fail "GET /v1/cards/lightningbolt returned ${lookup_code}, expected 200"
echo "OK: GET /v1/cards/lightningbolt"

# --- known-bad single lookup ---
missing_code="$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/v1/cards/this-card-does-not-exist-xyz")"
[ "${missing_code}" = "404" ] || fail "GET /v1/cards/this-card-does-not-exist-xyz returned ${missing_code}, expected 404"
echo "OK: GET /v1/cards/{unknown} -> 404"

# --- bulk lookup, mixed hit/miss ---
bulk_body="$(curl -sf -X POST "${BASE_URL}/v1/cards/bulk/lookup" \
  -H "Content-Type: application/json" \
  -d '{"card_ids": ["lightningbolt", "this-card-does-not-exist-xyz"]}')" || fail "POST /v1/cards/bulk/lookup did not return 200"
echo "${bulk_body}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
results = data.get('results') or []
not_found = data.get('not_found') or []
if not any(r.get('name', '').lower().replace(' ', '') == 'lightningbolt' for r in results):
    sys.exit('lightningbolt missing from results')
if 'this-card-does-not-exist-xyz' not in not_found:
    sys.exit('bogus id missing from not_found')
" || fail "bulk lookup response failed validation: ${bulk_body}"
echo "OK: POST /v1/cards/bulk/lookup"

echo
echo "All smoke tests passed."
