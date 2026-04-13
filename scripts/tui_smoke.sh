#!/usr/bin/env bash
# Smoke test the TUI: boot it via Textual's headless-screenshot mode and
# verify it doesn't crash. Uses the TEXTUAL_SCREENSHOT env var.
# Usage: scripts/tui_smoke.sh [duration_seconds]
set -u
DURATION="${1:-5}"
TARGET="${TARGET_TOKEN_ADDRESS:-EN2nnxrg8uUi6x2sJkzNPd2eT6rB9rdSoQNNaENA4RZA}"
REPO=$(dirname "$(dirname "$(realpath "$0")")")
cd "$REPO"

echo "=== TUI smoke test (${DURATION}s) ==="
timeout "$DURATION" env TARGET_TOKEN_ADDRESS="$TARGET" \
    TEXTUAL_SCREENSHOT="${DURATION}" \
    TEXTUAL_SCREENSHOT_LOCATION="/tmp" \
    uv run pod-the-trader --tui > /tmp/pod_tui_stdout.log 2> /tmp/pod_tui_stderr.log
EXIT=$?
echo "exit: $EXIT (124 = timeout, 0 = screenshot taken)"
ls -la /tmp/pod_tui_stdout.log /tmp/pod_tui_stderr.log 2>/dev/null
echo ""
echo "--- stderr (errors only) ---"
grep -v "DEBUG\|INFO" /tmp/pod_tui_stderr.log 2>/dev/null | tail -20
echo ""
SCREENSHOT=$(ls -t /tmp/*.svg 2>/dev/null | head -1)
if [ -n "$SCREENSHOT" ]; then
    echo "screenshot: $SCREENSHOT"
    ls -la "$SCREENSHOT"
fi
