#!/usr/bin/env bash
# Show actual on-chain wallet holdings: SOL + all SPL tokens.
# Usage: scripts/wallet_holdings.sh
set -u

REPO=$(dirname "$(dirname "$(realpath "$0")")")
cd "$REPO"

uv run python3 "$REPO/scripts/_wallet_holdings.py"
