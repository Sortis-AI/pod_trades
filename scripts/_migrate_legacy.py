"""Force-import any legacy JSON trades not yet in the CSV ledger."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pod_the_trader.data.ledger import TradeEntry, TradeLedger

STORAGE = Path.home() / ".pod_the_trader"
LEGACY = STORAGE / "trade_history.json"


def main() -> None:
    if not LEGACY.exists():
        print("No legacy file to migrate.")
        return

    legacy_trades = json.loads(LEGACY.read_text())
    if not legacy_trades:
        print("Legacy file is empty.")
        return

    ledger = TradeLedger()
    existing = ledger.read_all()
    existing_sigs = {t.signature for t in existing if t.signature}

    new_trades = [
        t for t in legacy_trades
        if t.get("signature") and t["signature"] not in existing_sigs
    ]

    print(f"Legacy trades: {len(legacy_trades)}")
    print(f"Already in CSV: {len(legacy_trades) - len(new_trades)}")
    print(f"To migrate:    {len(new_trades)}")
    print()

    if not new_trades:
        print("Nothing to do.")
        return

    for t in new_trades:
        entry = TradeEntry(
            timestamp=t.get("timestamp", ""),
            side=t.get("side", ""),
            input_mint=t.get("input_mint", ""),
            input_amount_ui=float(t.get("input_amount", 0)),
            input_value_usd=float(t.get("value_usd", 0)),
            output_mint=t.get("output_mint", ""),
            expected_out_ui=float(t.get("output_amount", 0)),
            actual_out_ui=float(t.get("output_amount", 0)),
            output_price_usd=float(t.get("price_usd", 0)),
            output_value_usd=float(t.get("value_usd", 0)),
            signature=t.get("signature", ""),
            notes="re-migrated from legacy json (will be repaired)",
        )
        ledger.append(entry)
        print(f"  added: {entry.signature[:24]}")

    print()
    print("Now run scripts/repair_ledger.sh to fix the on-chain values.")

    # Archive the legacy file so it stops being read
    archive = LEGACY.with_suffix(
        f".json.archived.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.move(LEGACY, archive)
    print(f"Archived legacy: {archive}")


if __name__ == "__main__":
    main()
