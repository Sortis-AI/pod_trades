#!/usr/bin/env bash
# Print a summary of a pod_the_trader log file.
# Usage: _log_summary.sh <logfile>
set -u

LOG="${1:-/tmp/pod_e2e.log}"

if [ ! -f "$LOG" ]; then
    echo "  (log file not found: $LOG)"
    exit 0
fi

count() { grep -c "$1" "$LOG" 2>/dev/null | head -1 || echo 0; }

echo "  file: $LOG ($(wc -l < "$LOG") lines)"
echo "  cycles:           $(count 'Agent response:')"
echo "  tool calls:       $(count 'Tool call:')"
echo "  minimax errors:   $(count 'Minimax midstream')"
echo "  empty responses:  $(count 'LLM response has no choices')"
echo "  cycle errors:     $(count 'Trading cycle error')"
echo "  jupiter failures: $(count 'Jupiter.*failed after')"
echo "  swap executions:  $(count '\"success\": true, \"signature\":')"
echo ""
echo "  --- recent agent responses ---"
grep "Agent response:" "$LOG" | tail -3 || true
echo ""
echo "  --- recent errors (if any) ---"
grep -E "Minimax midstream|LLM response has no choices|Trading cycle error" "$LOG" | tail -5 || true
echo ""
echo "  --- recent tool calls ---"
grep "Tool call:" "$LOG" | tail -5 || true
