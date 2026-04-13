#!/usr/bin/env bash
# Force-import any trades from trade_history.json into trades.csv that
# aren't already there (matched by signature), then re-fetch on-chain
# data for them. Optionally archives the legacy JSON afterwards.
# Usage: scripts/migrate_legacy.sh
set -u
REPO=$(dirname "$(dirname "$(realpath "$0")")")
cd "$REPO"
uv run python3 "$REPO/scripts/_migrate_legacy.py"
