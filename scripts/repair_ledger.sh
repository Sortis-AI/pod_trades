#!/usr/bin/env bash
# Repair the trade ledger by re-fetching each entry from on-chain.
# Reads trades.csv, queries Solana RPC for each signature, and rewrites
# rows with corrected gas, actual amounts, decimals, prices, and values.
# Usage: scripts/repair_ledger.sh
set -u

REPO=$(dirname "$(dirname "$(realpath "$0")")")
cd "$REPO"

uv run python3 "$REPO/scripts/_repair_ledger.py"
