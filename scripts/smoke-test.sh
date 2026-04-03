#!/usr/bin/env bash
# smoke-test.sh — post-deploy verification for Logos gateway
#
# Usage:
#   ./scripts/smoke-test.sh [BASE_URL]
#
# Defaults to http://localhost:8080.  Exits 0 on success, 1 on any failure.
# Suitable for CI pipelines and canary promotion gates.

set -euo pipefail

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
    ((PASS++))
  else
    echo "  FAIL  $label (got $status, expected $expected_status)"
    ((FAIL++))
  fi
}

echo "Smoke testing $BASE_URL"
echo "---"

# 1. Health check (no auth required)
_check "GET /health" "$BASE_URL/health"

# 2. K8s liveness probe alias
_check "GET /healthz" "$BASE_URL/healthz"

# 3. Deep readiness probe
_check "GET /health/ready" "$BASE_URL/health/ready"

# 4. Login page renders (no auth required)
_check "GET /login" "$BASE_URL/login"

# 5. Status endpoint (no auth required)
_check "GET /status" "$BASE_URL/status"

# 6. Souls endpoint (no auth required, returns soul registry)
_check "GET /souls" "$BASE_URL/souls"

# 7. Auth endpoint rejects bad credentials (should return 401)
_check "POST /auth/login (bad creds)" \
  "$BASE_URL/auth/login" \
  "401" \
  "POST" \
  '{"username":"__smoke_test__","password":"__invalid__"}'

# 8. Unauthenticated instance list returns 401/403
_check "GET /instances (no auth)" "$BASE_URL/instances" "401"

echo "---"
echo "Results: $PASS passed, $FAIL failed"

if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
