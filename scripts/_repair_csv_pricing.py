"""Repair trades.csv rows that were corrupted by the target↔USDC pricing bug.

Background: ``trading_tools.execute_swap`` originally assumed every swap was
SOL↔TARGET, so for any non-SOL input it picked a single "target_price" from
the wrong leg and applied it to both sides. The result:

* SQUIRE→USDC sells: ``output_value_usd`` was ``USDC_qty × SQUIRE_price`` —
  off by ~7000x in the wrong direction (~$0.00 instead of ~$1.00).
* USDC→SQUIRE buys (e.g. trade #21 in the user's history): ``side`` was
  recorded as "sell" instead of "buy", and ``output_value_usd`` was
  ``SQUIRE_qty × USDC_price`` — off by ~7000x the *other* way (~$74,000
  instead of ~$10).

Pure SOL↔TARGET trades and SOL↔USDC trades are unaffected and left alone.

This script:
  1. Backs up trades.csv → trades.csv.bak.<timestamp>
  2. For each row, identifies the broken pricing pattern and repairs it
     by treating the USDC leg as $1.00 (it's a stablecoin) and deriving
     the target leg's value from the swap rate.
  3. Re-derives ``side`` from the configured target token (BUY if the
     target is the output, SELL if it's the input).
  4. Writes the corrected file in place.

Usage::

    uv run python scripts/_repair_csv_pricing.py [--dry-run]
"""

from __future__ import annotations

import csv
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from pod_the_trader.config import Config
from pod_the_trader.data.ledger import LEDGER_COLUMNS

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_PRICE_USD = 1.0  # stablecoin


def _to_float(s: str) -> float:
    try:
        return float(s) if s != "" else 0.0
    except ValueError:
        return 0.0


def _row_needs_repair(row: dict, target_mint: str) -> tuple[bool, str]:
    """Return (needs_repair, reason) for one trade row."""
    in_mint = row.get("input_mint", "")
    out_mint = row.get("output_mint", "")

    # Only touch swaps where USDC is one leg AND the target is the other.
    legs = {in_mint, out_mint}
    if USDC_MINT not in legs or target_mint not in legs:
        return False, "not a USDC↔target swap"

    out_price = _to_float(row.get("output_price_usd", ""))
    in_amount = _to_float(row.get("input_amount_ui", ""))
    out_amount = _to_float(row.get("actual_out_ui", "")) or _to_float(
        row.get("expected_out_ui", "")
    )

    # If output is USDC, output_price should be ~1.0. Anything wildly off
    # means the bug applied the target token's price to USDC.
    if out_mint == USDC_MINT:
        if 0.5 <= out_price <= 1.5:
            return False, "USDC output already priced at ~$1"
        return True, f"USDC output priced at ${out_price:.8g}, should be ~$1"

    # Output is the target. Output price should be derived from the swap
    # rate against USDC input (input_value / output_qty). If it's pinned
    # near $1 and the trade is large, that's the bug signature (USDC's
    # price was applied to the target token output).
    if out_mint == target_mint:
        if in_amount > 0 and out_amount > 0:
            implied_price = (in_amount * USDC_PRICE_USD) / out_amount
            # Bug signature: output_price ~$1 and implied price is many
            # orders of magnitude smaller (typical for cheap memecoins).
            if out_price > implied_price * 100:
                return True, (
                    f"target output priced at ${out_price:.8g} but implied "
                    f"swap price is ${implied_price:.8g}"
                )
    return False, "looks correct"


def _repair_row(row: dict, target_mint: str) -> dict:
    """Return a new row dict with corrected pricing fields.

    Trusts the USDC leg as $1.00 and derives the target side from the
    swap rate. Also re-derives ``side`` relative to the target token.
    """
    new_row = dict(row)
    in_mint = row.get("input_mint", "")
    out_mint = row.get("output_mint", "")
    in_amount = _to_float(row.get("input_amount_ui", ""))
    out_amount = _to_float(row.get("actual_out_ui", "")) or _to_float(
        row.get("expected_out_ui", "")
    )

    # 1. Re-derive side from the target token's position.
    if out_mint == target_mint:
        new_row["side"] = "buy"
    elif in_mint == target_mint:
        new_row["side"] = "sell"

    # 2. Fix prices per leg.
    if out_mint == USDC_MINT:
        # SELL of target → USDC. USDC is the proceeds.
        usdc_value = out_amount * USDC_PRICE_USD
        new_row["output_price_usd"] = f"{USDC_PRICE_USD}"
        new_row["output_value_usd"] = f"{usdc_value}"
        # input_price/value were already approximately correct (target's
        # spot price applied to target qty), but re-anchor to the swap
        # rate so input_value matches output_value (modulo slippage).
        if in_amount > 0:
            implied_target_price = usdc_value / in_amount
            new_row["input_price_usd"] = f"{implied_target_price}"
            new_row["input_value_usd"] = f"{usdc_value}"
    elif in_mint == USDC_MINT:
        # BUY of target with USDC. USDC is the cost.
        usdc_value = in_amount * USDC_PRICE_USD
        new_row["input_price_usd"] = f"{USDC_PRICE_USD}"
        new_row["input_value_usd"] = f"{usdc_value}"
        if out_amount > 0:
            implied_target_price = usdc_value / out_amount
            new_row["output_price_usd"] = f"{implied_target_price}"
            new_row["output_value_usd"] = f"{usdc_value}"

    return new_row


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv

    config = Config()
    target_mint = config.get("trading.target_token_address", "")
    if not target_mint:
        print("ERROR: trading.target_token_address not configured", file=sys.stderr)
        return 1

    storage_dir = Path(config.get("storage.base_dir", "~/.pod_the_trader")).expanduser()
    csv_path = storage_dir / "trades.csv"
    if not csv_path.is_file():
        print(f"No trades.csv at {csv_path}", file=sys.stderr)
        return 1

    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        if reader.fieldnames is None:
            print("trades.csv has no header", file=sys.stderr)
            return 1
        # Use the file's actual fieldnames so we round-trip cleanly even
        # if the schema has new columns we don't know about.
        fieldnames = list(reader.fieldnames)

    repaired_rows: list[dict] = []
    repair_count = 0
    for i, row in enumerate(rows, start=1):
        needs, reason = _row_needs_repair(row, target_mint)
        if needs:
            repair_count += 1
            new_row = _repair_row(row, target_mint)
            print(f"  #{i:>2}: REPAIR ({reason})")
            print(
                f"        side {row.get('side', ''):<5} → {new_row.get('side', ''):<5}  "
                f"out_val ${_to_float(row.get('output_value_usd', '')):>14.8f} → "
                f"${_to_float(new_row.get('output_value_usd', '')):>14.8f}"
            )
            repaired_rows.append(new_row)
        else:
            repaired_rows.append(row)

    print()
    print(f"Repaired {repair_count} of {len(rows)} rows")

    if dry_run:
        print("[dry-run] not writing")
        return 0

    if repair_count == 0:
        print("No changes needed.")
        return 0

    backup = csv_path.with_suffix(
        f".csv.bak.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    )
    shutil.copy2(csv_path, backup)
    print(f"Backup → {backup}")

    # Pad missing fields with empty string so DictWriter doesn't error on
    # rows that pre-date later schema additions.
    for r in repaired_rows:
        for col in fieldnames:
            r.setdefault(col, "")

    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(repaired_rows)

    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
