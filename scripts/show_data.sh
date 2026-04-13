#!/usr/bin/env bash
# Show the ledger and price log files.
# Usage: scripts/show_data.sh
set -u

LEDGER=$HOME/.pod_the_trader/trades.csv
PRICES=$HOME/.pod_the_trader/prices.csv

echo "=== Trade Ledger ==="
if [ -f "$LEDGER" ]; then
    echo "file: $LEDGER ($(wc -l < "$LEDGER") lines)"
    echo ""
    column -t -s, "$LEDGER" 2>/dev/null | cut -c1-200 || head "$LEDGER"
else
    echo "  (no ledger file yet)"
fi

echo ""
echo "=== Price Log ==="
if [ -f "$PRICES" ]; then
    echo "file: $PRICES ($(wc -l < "$PRICES") lines)"
    echo ""
    echo "  --- latest 10 ticks ---"
    head -1 "$PRICES"
    tail -10 "$PRICES"
else
    echo "  (no price log file yet)"
fi
