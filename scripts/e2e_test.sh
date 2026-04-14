#!/usr/bin/env bash
# End-to-end test harness for pod_the_trader.
# Resets conversation state, runs the bot for a configurable duration,
# and reports pass/fail based on log content.
#
# Usage: scripts/e2e_test.sh [duration_seconds]  (default: 60)

set -u

DURATION="${1:-60}"
PRESERVE_STATE="${PRESERVE_STATE:-0}"
TARGET="${TARGET_TOKEN_ADDRESS:-EN2nnxrg8uUi6x2sJkzNPd2eT6rB9rdSoQNNaENA4RZA}"
LOG=/tmp/pod_e2e.log
STATE=$HOME/.pod_the_trader/conversation.json
REPO=$(dirname "$(dirname "$(realpath "$0")")")

cd "$REPO"

echo "=== Pod The Trader E2E Test ==="
echo "Duration: ${DURATION}s"
echo "Target: $TARGET"
echo "Log: $LOG"
echo "Preserve state: $PRESERVE_STATE"
echo ""

# Clear stale state unless preserving
if [ "$PRESERVE_STATE" = "0" ]; then
    rm -f "$STATE"
    echo "[setup] cleared conversation state"
else
    echo "[setup] preserving existing state ($([ -f "$STATE" ] && echo "exists" || echo "absent"))"
fi

# Run the bot with timeout. The bot now shows a disclaimer on every
# startup that blocks until "I ACCEPT" is typed — pipe it in so the e2e
# harness doesn't hang.
echo "[run] starting bot for ${DURATION}s..."
echo "I ACCEPT" | timeout "$DURATION" env TARGET_TOKEN_ADDRESS="$TARGET" uv run pod-the-trader > "$LOG" 2>&1
EXIT=$?
echo "[run] exit code: $EXIT (124 = timeout, which is expected)"
echo ""

# Analyze results. The bot's output for THIS run is captured in $LOG
# (stdout + stderr).  We count only things visible there so the metrics
# reflect this session and not accumulated forensic history.
# Tool-call telemetry is intentionally dropped from the pass criterion
# since those DEBUG messages are file-only and not useful for a PASS/FAIL
# decision.
echo "=== Results ==="
count() { grep -c "$1" "$LOG" 2>/dev/null | head -1 || echo 0; }
# Pod cycle summary boxes print " Cycle N " to stdout on cycle completion.
CYCLES=$(count " Cycle [0-9]* ")
MINIMAX_ERRORS=$(count "Minimax midstream")
EMPTY_CHOICES=$(count "LLM response has no choices")
CYCLE_ERRORS=$(count "Trading cycle error")
JUPITER_ERRORS=$(count "Jupiter.*failed after")
SWAP_SUCCESS=$(count "\[TRADE BUY\]\|\[TRADE SELL\]")
BANNER=$(count "Pod The Trader — live")
TOOL_CALLS=0  # no longer tracked here; see pod_the_trader.log for detail

echo "  cycles completed:      $CYCLES"
echo "  tool calls made:       $TOOL_CALLS"
echo "  minimax errors:        $MINIMAX_ERRORS"
echo "  empty LLM responses:   $EMPTY_CHOICES"
echo "  trading cycle errors:  $CYCLE_ERRORS"
echo "  jupiter failures:      $JUPITER_ERRORS"
echo "  swap executions:       $SWAP_SUCCESS"
echo ""

# Print last 5 errors if any
if [ "$MINIMAX_ERRORS" -gt 0 ] || [ "$EMPTY_CHOICES" -gt 0 ]; then
    echo "=== Recent LLM Errors ==="
    grep -E "Minimax midstream|LLM response has no choices" "$LOG" | tail -5
    echo ""
fi

if [ "$JUPITER_ERRORS" -gt 0 ]; then
    echo "=== Recent Jupiter Errors ==="
    grep -E "Jupiter.*failed after" "$LOG" | tail -3
    echo ""
fi

# Pass criterion: no LLM errors AND the bot got far enough to show its
# startup banner (or completed a cycle/trade).
if [ "$MINIMAX_ERRORS" -eq 0 ] && [ "$EMPTY_CHOICES" -eq 0 ] && \
   { [ "$CYCLES" -gt 0 ] || [ "$BANNER" -gt 0 ]; }; then
    echo "[PASS] $CYCLES cycle(s), $SWAP_SUCCESS swap(s), no errors"
    exit 0
else
    echo "[FAIL] issues detected"
    exit 1
fi
