#!/usr/bin/env bash
# Dump conversation state + last log in a readable form.
# Usage: scripts/inspect.sh
set -u

REPO=$(dirname "$(dirname "$(realpath "$0")")")
cd "$REPO"

LOG=/tmp/pod_e2e.log
STATE=$HOME/.pod_the_trader/conversation.json

echo "=== Conversation State ==="
if [ -f "$STATE" ]; then
    uv run python3 "$REPO/scripts/_dump_state.py"
else
    echo "  (no state file)"
fi
echo ""

echo "=== Last E2E Log Summary ==="
if [ -f "$LOG" ]; then
    bash "$REPO/scripts/_log_summary.sh" "$LOG"
else
    echo "  (no log file at $LOG)"
fi
