#!/usr/bin/env bash
# Show trading activity: trade history, wallet balance, recent log activity.
# Usage: scripts/status.sh
set -u

REPO=$(dirname "$(dirname "$(realpath "$0")")")
cd "$REPO"

uv run python3 "$REPO/scripts/_status.py"
