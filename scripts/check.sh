#!/usr/bin/env bash
# One-command full check: lint + tests + e2e run.
# Usage: scripts/check.sh [duration_seconds]
set -u

REPO=$(dirname "$(dirname "$(realpath "$0")")")
cd "$REPO"

DURATION="${1:-90}"

echo "================================================"
echo " Pod The Trader — Full Check"
echo "================================================"
echo ""
echo "[1/3] Lint"
echo "------------------------------------------------"
uv run ruff check pod_the_trader/ tests/
RUFF=$?
echo ""

echo "[2/3] Unit + integration tests"
echo "------------------------------------------------"
uv run pytest tests/ -q
PYTEST=$?
echo ""

echo "[3/3] E2E run (${DURATION}s)"
echo "------------------------------------------------"
bash "$REPO/scripts/e2e_test.sh" "$DURATION"
E2E=$?
echo ""

echo "================================================"
echo " Summary"
echo "================================================"
[ $RUFF -eq 0 ]   && echo "  lint:   PASS" || echo "  lint:   FAIL"
[ $PYTEST -eq 0 ] && echo "  tests:  PASS" || echo "  tests:  FAIL"
[ $E2E -eq 0 ]    && echo "  e2e:    PASS" || echo "  e2e:    FAIL"

if [ $RUFF -eq 0 ] && [ $PYTEST -eq 0 ] && [ $E2E -eq 0 ]; then
    echo ""
    echo "  ALL GREEN"
    exit 0
fi
exit 1
