#!/usr/bin/env bash
# test.sh — run the logos/hermes-agent test suite locally
#
# Usage:
#   ./scripts/test.sh                   # unit tests only (default, mirrors CI)
#   ./scripts/test.sh --integration     # include integration tests (needs API keys)
#   ./scripts/test.sh --all             # unit + integration
#   ./scripts/test.sh --mini            # mini-swe-agent tests only
#   ./scripts/test.sh --everything      # all of the above
#   ./scripts/test.sh -k "test_foo"     # pass extra pytest args through
#   ./scripts/test.sh --no-parallel     # disable -n auto (easier to read tracebacks)
#   ./scripts/test.sh --coverage        # emit an html coverage report

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ── Defaults ──────────────────────────────────────────────────────────────────
RUN_UNIT=true
RUN_INTEGRATION=false
RUN_MINI=false
PARALLEL=true
COVERAGE=false
EXTRA_ARGS=()

# ── Arg parsing ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --integration)   RUN_INTEGRATION=true;  RUN_UNIT=true;  shift ;;
    --all)           RUN_INTEGRATION=true;  RUN_UNIT=true;  shift ;;
    --mini)          RUN_MINI=true;         RUN_UNIT=false; shift ;;
    --everything)    RUN_UNIT=true; RUN_INTEGRATION=true; RUN_MINI=true; shift ;;
    --no-parallel)   PARALLEL=false;  shift ;;
    --coverage)      COVERAGE=true;   shift ;;
    *)               EXTRA_ARGS+=("$1"); shift ;;
  esac
done

# ── Detect / activate venv ────────────────────────────────────────────────────
if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
elif ! python -c "import pytest" &>/dev/null; then
  echo "ERROR: pytest not found and no .venv present."
  echo "Run:  uv venv .venv --python 3.11 && source .venv/bin/activate && uv pip install -e '.[all,dev]'"
  exit 1
fi

# ── Null out API keys so tests never hit real backends ────────────────────────
export OPENROUTER_API_KEY=""
export OPENAI_API_KEY=""
export NOUS_API_KEY=""
export ANTHROPIC_API_KEY=""

# ── Build common pytest flags ─────────────────────────────────────────────────
PYTEST_FLAGS=("--tb=short" "-q")
$PARALLEL && PYTEST_FLAGS+=("-n" "auto")
$COVERAGE && PYTEST_FLAGS+=("--cov=." "--cov-report=html:htmlcov" "--cov-report=term-missing")

FAILED=0

# ── Unit tests ────────────────────────────────────────────────────────────────
if $RUN_UNIT; then
  echo ""
  echo "━━━ Unit tests (tests/, no integration) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  UNIT_FLAGS=("${PYTEST_FLAGS[@]}" "-m" "not integration" "--ignore=tests/integration")
  $RUN_INTEGRATION && UNIT_FLAGS=("${PYTEST_FLAGS[@]}")  # include integration marker if --all
  python -m pytest tests/ "${UNIT_FLAGS[@]}" "${EXTRA_ARGS[@]}" || FAILED=$?
fi

# ── Integration tests (explicit, needs real API keys) ─────────────────────────
if $RUN_INTEGRATION && ! $RUN_UNIT; then
  # --integration alone: run only the integration dir with marker
  echo ""
  echo "━━━ Integration tests ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  python -m pytest tests/integration/ "${PYTEST_FLAGS[@]}" "${EXTRA_ARGS[@]}" || FAILED=$?
fi

# ── mini-swe-agent tests ──────────────────────────────────────────────────────
if $RUN_MINI; then
  echo ""
  echo "━━━ mini-swe-agent tests ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  python -m pytest mini-swe-agent/tests/ "${PYTEST_FLAGS[@]}" "${EXTRA_ARGS[@]}" || FAILED=$?
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
if [[ $FAILED -ne 0 ]]; then
  echo "FAILED (exit $FAILED)"
  exit $FAILED
else
  echo "All tests passed."
fi
