"""Repair trade ledger entries by re-fetching transactions from chain.

For each row, queries Solana RPC for the transaction details and recomputes:
- gas (from meta.fee)
- actual output amount (from token balance diff)
- block slot + time
- token decimals (from token balance entries)
- USD values (using sol_price_usd if present, otherwise queries Jupiter)

Backs up the original ledger before writing.
"""

import asyncio
import csv
import json
import shutil
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey
from solders.signature import Signature

from pod_the_trader.data.ledger import LEDGER_COLUMNS, TradeEntry, TradeLedger

RPC_URL = "https://api.mainnet-beta.solana.com"
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000


def fetch_token_decimals(mint: str) -> int:
    """Fetch token decimals from Jupiter token list. Default 6 on failure."""
    try:
        url = f"https://lite-api.jup.ag/tokens/v2/search?query={mint}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "pod-the-trader/0.1"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        for token in data:
            if token.get("id") == mint:
                return int(token.get("decimals", 6))
    except Exception:
        pass
    return 6


def fetch_token_price(mint: str) -> float:
    """Fetch current USD price from Jupiter."""
    try:
        url = f"https://lite-api.jup.ag/price/v3?ids={mint}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "pod-the-trader/0.1"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        return float(data.get(mint, {}).get("usdPrice", 0))
    except Exception:
        return 0.0


async def fetch_tx(client: AsyncClient, sig: str) -> dict | None:
    try:
        signature = Signature.from_string(sig)
        resp = await client.get_transaction(
            signature, encoding="json", max_supported_transaction_version=0
        )
        return resp.value
    except Exception as e:
        print(f"  failed: {e}")
        return None


def extract_token_changes(
    tx_data, owner: str, mint: str
) -> tuple[int, int]:
    """Return (pre_amount, post_amount) for the owner+mint pair."""
    meta = getattr(tx_data.transaction, "meta", None)
    if meta is None:
        return 0, 0

    pre = getattr(meta, "pre_token_balances", None) or []
    post = getattr(meta, "post_token_balances", None) or []

    def _amount(entries) -> int:
        for e in entries:
            # mint/owner come back as Pubkey objects — wrap in str()
            if (
                str(getattr(e, "mint", "")) == mint
                and str(getattr(e, "owner", "")) == owner
            ):
                ui = getattr(e, "ui_token_amount", None)
                if ui is not None:
                    try:
                        return int(getattr(ui, "amount", 0))
                    except (TypeError, ValueError):
                        return 0
        return 0

    return _amount(pre), _amount(post)


def extract_decimals(tx_data, mint: str) -> int:
    """Find the decimals reported in the transaction's token balances."""
    meta = getattr(tx_data.transaction, "meta", None)
    if meta is None:
        return 6
    for entries in (
        getattr(meta, "pre_token_balances", None) or [],
        getattr(meta, "post_token_balances", None) or [],
    ):
        for e in entries:
            if str(getattr(e, "mint", "")) == mint:
                ui = getattr(e, "ui_token_amount", None)
                if ui is not None:
                    return int(getattr(ui, "decimals", 6) or 6)
    return 6


def get_wallet_address() -> str:
    """Read the wallet address from the keypair file."""
    keypair_file = Path.home() / ".pod_the_trader" / "keypair.json"
    if not keypair_file.exists():
        return ""
    try:
        from solders.keypair import Keypair

        kp = Keypair.from_bytes(bytes(json.loads(keypair_file.read_text())))
        return str(kp.pubkey())
    except Exception:
        return ""


async def repair_entry(
    client: AsyncClient, entry: TradeEntry, default_wallet: str
) -> TradeEntry:
    if not entry.signature:
        print(f"  skip: no signature")
        return entry

    print(f"  fetching {entry.signature[:20]}...")
    tx_data = await fetch_tx(client, entry.signature)
    if tx_data is None:
        print("  could not fetch transaction")
        return entry

    slot = getattr(tx_data, "slot", 0) or 0
    block_time = getattr(tx_data, "block_time", 0) or 0
    meta = getattr(tx_data.transaction, "meta", None)
    gas_lamports = int(getattr(meta, "fee", 0) or 0) if meta else 0

    # Determine decimals for input/output
    input_decimals = (
        9 if entry.input_mint == SOL_MINT
        else extract_decimals(tx_data, entry.input_mint)
    )
    output_decimals = (
        9 if entry.output_mint == SOL_MINT
        else extract_decimals(tx_data, entry.output_mint)
    )
    if input_decimals == 6 and entry.input_mint != SOL_MINT:
        input_decimals = fetch_token_decimals(entry.input_mint)
    if output_decimals == 6 and entry.output_mint != SOL_MINT:
        output_decimals = fetch_token_decimals(entry.output_mint)

    # Actual output: positive token balance change for the wallet
    wallet = entry.wallet or default_wallet
    pre_out, post_out = extract_token_changes(
        tx_data, wallet, entry.output_mint
    )
    actual_out_raw = max(0, post_out - pre_out)

    # If still zero, try matching by account_index across pre/post
    if actual_out_raw == 0 and meta is not None:
        post = getattr(meta, "post_token_balances", None) or []
        pre = getattr(meta, "pre_token_balances", None) or []
        for p in post:
            if str(getattr(p, "mint", "")) != entry.output_mint:
                continue
            if str(getattr(p, "owner", "")) != wallet:
                continue
            ui = getattr(p, "ui_token_amount", None)
            if not ui:
                continue
            post_amt = int(getattr(ui, "amount", 0))
            pre_amt = 0
            p_idx = getattr(p, "account_index", -2)
            for q in pre:
                if getattr(q, "account_index", -1) == p_idx:
                    pre_ui = getattr(q, "ui_token_amount", None)
                    if pre_ui:
                        pre_amt = int(getattr(pre_ui, "amount", 0))
                    break
            diff = post_amt - pre_amt
            if diff > 0:
                actual_out_raw = diff
                break

    # Compute UI amounts
    input_amount_ui = entry.input_amount_raw / (10**input_decimals) if entry.input_amount_raw else entry.input_amount_ui
    if entry.input_mint == SOL_MINT and entry.input_amount_ui and not entry.input_amount_raw:
        # Legacy entry: input_amount_ui was correct (0.1 SOL), input_amount_raw was 0
        input_amount_raw = int(entry.input_amount_ui * (10**input_decimals))
    else:
        input_amount_raw = entry.input_amount_raw or int(input_amount_ui * (10**input_decimals))

    actual_out_ui = actual_out_raw / (10**output_decimals)
    expected_out_raw = entry.expected_out_raw or actual_out_raw
    expected_out_ui = expected_out_raw / (10**output_decimals)

    # Prices: keep existing if set, else fetch current (best we can do for legacy)
    sol_price = entry.sol_price_usd or fetch_token_price(SOL_MINT)
    input_price = (
        sol_price if entry.input_mint == SOL_MINT
        else (entry.input_price_usd or fetch_token_price(entry.input_mint))
    )
    output_price = (
        sol_price if entry.output_mint == SOL_MINT
        else (entry.output_price_usd or fetch_token_price(entry.output_mint))
    )

    input_value = input_amount_ui * input_price
    output_value = actual_out_ui * output_price

    gas_sol = gas_lamports / LAMPORTS_PER_SOL
    gas_usd = gas_sol * sol_price

    # Build the repaired entry
    repaired = TradeEntry(
        timestamp=entry.timestamp,
        session_id=entry.session_id,
        side=entry.side,
        input_mint=entry.input_mint,
        input_symbol=entry.input_symbol or ("SOL" if entry.input_mint == SOL_MINT else ""),
        input_decimals=input_decimals,
        input_amount_raw=input_amount_raw,
        input_amount_ui=input_amount_ui,
        input_price_usd=input_price,
        input_value_usd=input_value,
        output_mint=entry.output_mint,
        output_symbol=entry.output_symbol or ("SOL" if entry.output_mint == SOL_MINT else ""),
        output_decimals=output_decimals,
        expected_out_raw=expected_out_raw,
        expected_out_ui=expected_out_ui,
        actual_out_raw=actual_out_raw,
        actual_out_ui=actual_out_ui,
        output_price_usd=output_price,
        output_value_usd=output_value,
        slippage_bps_requested=entry.slippage_bps_requested,
        slippage_bps_realized=entry.slippage_bps_realized,
        price_impact_pct=entry.price_impact_pct,
        sol_price_usd=sol_price,
        gas_lamports=gas_lamports,
        gas_sol=gas_sol,
        gas_usd=gas_usd,
        signature=entry.signature,
        block_slot=slot,
        block_time=block_time,
        wallet=entry.wallet,
        model=entry.model,
        notes=(entry.notes + "; repaired from chain").strip("; "),
    )

    print(f"    {entry.side} {input_amount_ui:.6f} -> {actual_out_ui:.4f}")
    print(f"    decimals: in={input_decimals} out={output_decimals}")
    print(f"    gas: {gas_lamports} lamports (${gas_usd:.6f})")
    print(f"    values: ${input_value:.4f} -> ${output_value:.4f}")
    return repaired


async def main() -> None:
    ledger = TradeLedger()
    entries = ledger.read_all()
    if not entries:
        print("Ledger is empty, nothing to repair.")
        return

    default_wallet = get_wallet_address()
    print(f"Default wallet: {default_wallet}")
    print(f"Repairing {len(entries)} ledger entries from chain...")
    print()

    # Backup
    backup = ledger.path.with_suffix(
        f".csv.bak.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    )
    shutil.copy(ledger.path, backup)
    print(f"Backup: {backup}")
    print()

    repaired = []
    async with AsyncClient(RPC_URL) as client:
        for i, entry in enumerate(entries):
            print(f"[{i + 1}/{len(entries)}] {entry.side} {entry.timestamp}")
            try:
                fixed = await repair_entry(client, entry, default_wallet)
                repaired.append(fixed)
            except Exception as e:
                print(f"  ERROR: {e}")
                repaired.append(entry)
            print()

    # Rewrite
    with ledger.path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS)
        writer.writeheader()
        for entry in repaired:
            writer.writerow(entry.to_row())

    print(f"Wrote {len(repaired)} repaired entries to {ledger.path}")


if __name__ == "__main__":
    asyncio.run(main())
