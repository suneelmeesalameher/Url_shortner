#!/usr/bin/env bash
# QA validation script for the containerized URL Shortener stack.
#
# Runs six sequential checks against a live `docker compose` deployment:
#   1. Startup + health checks (app, postgres, redis)
#   2. Greenfield flow: shorten -> redirect (302 + Location)
#   3. Custom alias creation + duplicate-alias conflict (409)
#   4. SSRF guard: cloud metadata IP + loopback IP rejected
#   5. Analytics: click counts and time-series recorded asynchronously
#   6. Resiliency: Redis stopped -> redirect still resolves via Postgres fallback
#
# Usage:
#   ./test_stack.sh                          # uses http://localhost:8000
#   BASE_URL=http://localhost:8080 ./test_stack.sh
#
# Requires: docker, docker compose (v2 plugin), curl, python3 (used for JSON
# parsing instead of jq, since python3 is already a hard dependency of this project).
#
# Exit code: 0 if every check passed, 1 if any check failed.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BASE_URL="${BASE_URL:-http://localhost:8000}"
STARTUP_TIMEOUT_SECONDS="${STARTUP_TIMEOUT_SECONDS:-90}"
RUN_ID="$(date +%s)-$RANDOM"           # keeps each run's test data unique, so re-runs never collide
TMP_DIR="$(mktemp -d)"

PASS_COUNT=0
FAIL_COUNT=0

trap 'rm -rf "$TMP_DIR"' EXIT

# ---------------------------------------------------------------------------
# Formatting / assertion helpers
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_YELLOW='\033[1;33m'; C_BOLD='\033[1m'; C_RESET='\033[0m'
else
    C_GREEN=''; C_RED=''; C_YELLOW=''; C_BOLD=''; C_RESET=''
fi

section() {
    echo
    echo -e "${C_BOLD}=== $1 ===${C_RESET}"
}

pass() {
    PASS_COUNT=$((PASS_COUNT + 1))
    echo -e "  ${C_GREEN}PASS${C_RESET} - $1"
}

fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    echo -e "  ${C_RED}FAIL${C_RESET} - $1"
}

info() {
    echo -e "  ${C_YELLOW}i${C_RESET} $1"
}

# assert_status_in <actual> <description> <expected1> [expected2 ...]
assert_status_in() {
    local actual="$1" description="$2"
    shift 2
    for expected in "$@"; do
        if [ "$actual" = "$expected" ]; then
            pass "$description (got HTTP $actual)"
            return 0
        fi
    done
    fail "$description (expected one of [$*], got HTTP $actual)"
    return 1
}

# json_get <file> <field> -> prints the field's value, or empty string if absent/unparsable
json_get() {
    python3 -c "
import json, sys
try:
    d = json.load(open('$1'))
    print(d.get('$2', ''))
except Exception:
    print('')
"
}

# json_len <file> <field> -> prints the length of a list field, or 0
json_len() {
    python3 -c "
import json, sys
try:
    d = json.load(open('$1'))
    print(len(d.get('$2', [])))
except Exception:
    print(0)
"
}

# wait_for_service_healthy <compose service name> <timeout seconds>
wait_for_service_healthy() {
    local service="$1" timeout="$2" waited=0 cid status
    cid=$(docker compose ps -q "$service" 2>/dev/null)
    if [ -z "$cid" ]; then
        fail "container for service '$service' not found"
        return 1
    fi
    while [ "$waited" -lt "$timeout" ]; do
        status=$(docker inspect --format='{{.State.Health.Status}}' "$cid" 2>/dev/null || echo "unknown")
        if [ "$status" = "healthy" ]; then
            pass "service '$service' is healthy (${waited}s)"
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    fail "service '$service' did not become healthy within ${timeout}s (last status: $status)"
    return 1
}

# ---------------------------------------------------------------------------
# 1. Docker Compose Startup
# ---------------------------------------------------------------------------
section "1. Docker Compose Startup"

if docker compose up --build -d; then
    pass "docker compose up --build -d exited successfully"
else
    fail "docker compose up --build -d failed"
fi

wait_for_service_healthy app "$STARTUP_TIMEOUT_SECONDS"
wait_for_service_healthy postgres "$STARTUP_TIMEOUT_SECONDS"
wait_for_service_healthy redis "$STARTUP_TIMEOUT_SECONDS"

# ---------------------------------------------------------------------------
# 2. Greenfield Flow: POST /api/v1/shorten -> GET /{short_code} -> 302 + Location
# ---------------------------------------------------------------------------
section "2. Greenfield Flow (shorten -> redirect)"

TARGET_URL="https://example.com/qa/greenfield/${RUN_ID}"
status=$(curl -s -o "$TMP_DIR/greenfield.json" -w "%{http_code}" \
    -X POST "$BASE_URL/api/v1/shorten" \
    -H "Content-Type: application/json" \
    -d "{\"original_url\": \"$TARGET_URL\"}")

assert_status_in "$status" "POST /api/v1/shorten returns 201" 201

SHORT_CODE=$(json_get "$TMP_DIR/greenfield.json" short_code)
if [ -n "$SHORT_CODE" ]; then
    pass "extracted short_code from response: '$SHORT_CODE'"
else
    fail "could not extract short_code from response body: $(cat "$TMP_DIR/greenfield.json")"
fi

status=$(curl -s -o /dev/null -D "$TMP_DIR/redirect_headers.txt" -w "%{http_code}" "$BASE_URL/$SHORT_CODE")
assert_status_in "$status" "GET /$SHORT_CODE returns 302" 302

LOCATION=$(grep -i '^location:' "$TMP_DIR/redirect_headers.txt" | sed 's/^[Ll]ocation: *//' | tr -d '\r\n')
if [ "$LOCATION" = "$TARGET_URL" ]; then
    pass "Location header matches original_url ($LOCATION)"
else
    fail "Location header mismatch (expected '$TARGET_URL', got '$LOCATION')"
fi

# ---------------------------------------------------------------------------
# 3. Custom Alias & Conflict Check
# ---------------------------------------------------------------------------
section "3. Custom Alias & Conflict Check"

# Short on purpose: the app enforces a 20-character max on custom_alias, and
# "qa-alias-${RUN_ID}" (RUN_ID includes a full unix timestamp) blew past that,
# getting rejected by request-schema validation (422) before ever reaching the
# alias-conflict logic this section is meant to test. Numeric-only suffix keeps
# this comfortably under the limit while still being unique per run.
ALIAS="qa-$(( $(date +%s) % 100000 ))${RANDOM}"

status=$(curl -s -o "$TMP_DIR/alias_first.json" -w "%{http_code}" \
    -X POST "$BASE_URL/api/v1/shorten" \
    -H "Content-Type: application/json" \
    -d "{\"original_url\": \"https://example.com/qa/alias/${RUN_ID}\", \"custom_alias\": \"$ALIAS\"}")
assert_status_in "$status" "first request for alias '$ALIAS' returns 201" 201

status=$(curl -s -o "$TMP_DIR/alias_conflict.json" -w "%{http_code}" \
    -X POST "$BASE_URL/api/v1/shorten" \
    -H "Content-Type: application/json" \
    -d "{\"original_url\": \"https://example.com/qa/alias-again/${RUN_ID}\", \"custom_alias\": \"$ALIAS\"}")
assert_status_in "$status" "duplicate request for the same alias returns 409" 409

ERROR_CODE=$(json_get "$TMP_DIR/alias_conflict.json" error)
if [ "$ERROR_CODE" = "ALIAS_TAKEN" ]; then
    pass "409 response body reports error=ALIAS_TAKEN"
else
    fail "409 response body did not report ALIAS_TAKEN (got: $(cat "$TMP_DIR/alias_conflict.json"))"
fi

# ---------------------------------------------------------------------------
# 4. SSRF Guard Validation
# ---------------------------------------------------------------------------
section "4. SSRF Guard Validation"

for ssrf_target in "http://169.254.169.254/latest/meta-data" "http://127.0.0.1/"; do
    status=$(curl -s -o "$TMP_DIR/ssrf.json" -w "%{http_code}" \
        -X POST "$BASE_URL/api/v1/shorten" \
        -H "Content-Type: application/json" \
        -d "{\"original_url\": \"$ssrf_target\"}")
    # The implementation returns 400 (INVALID_URL) specifically; 422 is accepted
    # too since that's what a pure request-schema validator would return for the
    # same rejection in a different implementation.
    assert_status_in "$status" "POST with original_url=$ssrf_target is rejected" 400 422
done

# ---------------------------------------------------------------------------
# 5. Analytics Reporting
# ---------------------------------------------------------------------------
section "5. Analytics Reporting"

info "generating 2 additional clicks on short_code '$SHORT_CODE' (1 already happened in step 2)"
curl -s -o /dev/null "$BASE_URL/$SHORT_CODE"
curl -s -o /dev/null "$BASE_URL/$SHORT_CODE"
EXPECTED_CLICKS=3

info "polling the analytics endpoint - the click recorder flushes asynchronously, not instantly"
ANALYTICS_OK=0
for attempt in $(seq 1 20); do
    curl -s -o "$TMP_DIR/analytics.json" -w "%{http_code}" "$BASE_URL/api/v1/urls/$SHORT_CODE/analytics" > "$TMP_DIR/analytics_status.txt"
    total_clicks=$(json_get "$TMP_DIR/analytics.json" total_clicks)
    if [ "$total_clicks" = "$EXPECTED_CLICKS" ]; then
        ANALYTICS_OK=1
        break
    fi
    sleep 0.5
done

analytics_status=$(cat "$TMP_DIR/analytics_status.txt")
assert_status_in "$analytics_status" "GET /api/v1/urls/$SHORT_CODE/analytics returns 200" 200

if [ "$ANALYTICS_OK" = "1" ]; then
    pass "total_clicks reached expected value ($EXPECTED_CLICKS) within 10s"
else
    fail "total_clicks did not reach $EXPECTED_CLICKS within 10s (last seen: $total_clicks)"
fi

time_series_len=$(json_len "$TMP_DIR/analytics.json" time_series)
if [ "$time_series_len" -gt 0 ]; then
    pass "time_series contains at least one bucket ($time_series_len)"
else
    fail "time_series is empty - click events were not recorded"
fi

# ---------------------------------------------------------------------------
# 6. Fallback & Resiliency Test (Redis down -> Postgres fallback)
# ---------------------------------------------------------------------------
section "6. Fallback & Resiliency Test (Redis outage)"

info "stopping the redis container to simulate a cache outage"
docker compose stop redis > /dev/null

# Give the in-app circuit breaker's health signal a moment to reflect reality;
# not strictly required (the very first call already fails over), but avoids
# racing the container's own shutdown.
sleep 1

start_time=$(date +%s%N)
status=$(curl -s -o /dev/null -D "$TMP_DIR/fallback_headers.txt" -w "%{http_code}" --max-time 5 "$BASE_URL/$SHORT_CODE")
end_time=$(date +%s%N)
elapsed_ms=$(( (end_time - start_time) / 1000000 ))

assert_status_in "$status" "GET /$SHORT_CODE with Redis down still returns 302" 302

FALLBACK_LOCATION=$(grep -i '^location:' "$TMP_DIR/fallback_headers.txt" | sed 's/^[Ll]ocation: *//' | tr -d '\r\n')
if [ "$FALLBACK_LOCATION" = "$TARGET_URL" ]; then
    pass "Location header still correct via Postgres fallback ($FALLBACK_LOCATION)"
else
    fail "Location header wrong during fallback (expected '$TARGET_URL', got '$FALLBACK_LOCATION')"
fi

info "request completed in ${elapsed_ms}ms with Redis down"
if [ "$elapsed_ms" -lt 3000 ]; then
    pass "fallback did not hang waiting on the dead Redis connection (${elapsed_ms}ms < 3000ms)"
else
    fail "fallback took ${elapsed_ms}ms - Redis timeouts may not be configured correctly"
fi

info "restarting redis and waiting for it to report healthy again"
docker compose start redis > /dev/null
wait_for_service_healthy redis "$STARTUP_TIMEOUT_SECONDS"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
section "Summary"
echo -e "  ${C_GREEN}${PASS_COUNT} passed${C_RESET}, ${C_RED}${FAIL_COUNT} failed${C_RESET}"

if [ "$FAIL_COUNT" -eq 0 ]; then
    echo -e "${C_GREEN}${C_BOLD}ALL CHECKS PASSED${C_RESET}"
    exit 0
else
    echo -e "${C_RED}${C_BOLD}ONE OR MORE CHECKS FAILED${C_RESET}"
    exit 1
fi
