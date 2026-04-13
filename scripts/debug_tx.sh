#!/usr/bin/env bash
set -u
REPO=$(dirname "$(dirname "$(realpath "$0")")")
cd "$REPO"
uv run python3 "$REPO/scripts/_debug_tx.py"
