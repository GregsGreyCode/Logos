#!/usr/bin/env bash
# smoke-test.sh — post-deploy verification for Logos gateway
#
# Usage:
#   ./scripts/smoke-test.sh [BASE_URL]
#
# Defaults to http://localhost:8080.  Exits 0 on success, 1 on any failure.
# Suitable for CI pipelines and canary promotion gates.

set -uo pipefail

BASE_URL="${1:-http://localhost:8080}"
PASS=0
FAIL=0

_check() {
  local label="$1"
  local url="$2"
  local expected_status="${3:-200}"
  local method="${4:-GET}"
  local body="${5:-}"

  local curl_args=(-s -o /dev/null -w "%{http_code}" --max-time 10)

  if [[ "$method" == "POST" ]]; then
    curl_args+=(-X POST -H "Content-Type: application/json")
    if [[ -n "$body" ]]; then
      curl_args+=(-d "$body")
    fi
  fi

  local status
  status=$(curl "${curl_args[@]}" "$url" 2>/dev/null || echo "000")

  if [[ "$status" == "$expected_status" ]]; then
    echo "  PASS  $label ($status)"
    PASS=$((PASS + 1))
  else
    echo "  FAIL  $label (got $status, expected $expected_status)"
    FAIL=$((FAIL + 1))
  fi
}

echo "Smoke testing $BASE_URL"
echo "---"

# 1. Health check (no auth required — always public)
_check "GET /health" "$BASE_URL/health"

# 2. Deep readiness probe (no auth)
_check "GET /health/ready" "$BASE_URL/health/ready"

# 3. Login page renders (no auth)
_check "GET /login" "$BASE_URL/login"

# 4. Model catalog API (no auth)
_check "GET /api/model-catalog" "$BASE_URL/api/model-catalog"

# 5. Auth-protected endpoints return 401 without credentials
_check "GET /instances (no auth → 401)" "$BASE_URL/instances" "401"
_check "GET /status (no auth → 401)" "$BASE_URL/status" "401"
_check "GET /souls (no auth → 401)" "$BASE_URL/souls" "401"

# 6. Bad login credentials rejected
_check "POST /auth/login (bad creds → 400/401)" \
  "$BASE_URL/auth/login" \
  "400" \
  "POST" \
  '{"username":"__smoke_test__","password":"__invalid__"}'

echo "---"
echo "Results: $PASS passed, $FAIL failed"

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
