#!/usr/bin/env bash
# Run the bot for a set duration, capturing stdout and stderr separately.
# Stdout (/tmp/pod_stdout.log) = what a user sees on their terminal.
# Stderr (/tmp/pod_stderr.log) = errors and logging via StreamHandler.
# Usage: scripts/run_capture.sh [duration_seconds]
set -u

DURATION="${1:-600}"
TARGET="${TARGET_TOKEN_ADDRESS:-EN2nnxrg8uUi6x2sJkzNPd2eT6rB9rdSoQNNaENA4RZA}"
STDOUT=/tmp/pod_stdout.log
STDERR=/tmp/pod_stderr.log
REPO=$(dirname "$(dirname "$(realpath "$0")")")

cd "$REPO"

echo "=== Running Pod The Trader for ${DURATION}s ==="
echo "stdout: $STDOUT"
echo "stderr: $STDERR"
echo "target: $TARGET"
echo ""
echo "Started at $(date -Iseconds)"

# Run with stdout and stderr split
timeout "$DURATION" env TARGET_TOKEN_ADDRESS="$TARGET" \
    uv run pod-the-trader > "$STDOUT" 2> "$STDERR"
EXIT=$?

echo ""
echo "Finished at $(date -Iseconds) (exit code: $EXIT)"
echo ""
echo "stdout: $(wc -l < "$STDOUT") lines"
echo "stderr: $(wc -l < "$STDERR") lines"
