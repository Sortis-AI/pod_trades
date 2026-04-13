#!/usr/bin/env bash
# Search the bot's main log file for a pattern.
# Usage: scripts/grep_log.sh <pattern> [context_lines]
set -u

PATTERN="${1:?usage: grep_log.sh <pattern> [context]}"
CONTEXT="${2:-0}"

LOG_CANDIDATES=(
    "$PWD/pod_the_trader.log"
    "$HOME/pod_the_trader.log"
    "$HOME/Code/pod_the_trader/pod_the_trader.log"
)

LOG=""
for c in "${LOG_CANDIDATES[@]}"; do
    if [ -f "$c" ]; then
        LOG="$c"
        break
    fi
done

if [ -z "$LOG" ]; then
    echo "  (no log file found in: ${LOG_CANDIDATES[*]})"
    exit 1
fi

echo "log: $LOG ($(wc -l < "$LOG") lines)"
echo "pattern: $PATTERN"
echo ""

if [ "$CONTEXT" -gt 0 ]; then
    grep -n -C "$CONTEXT" -E "$PATTERN" "$LOG"
else
    grep -n -E "$PATTERN" "$LOG"
fi
