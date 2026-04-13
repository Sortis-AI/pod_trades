"""Show pod_the_trader status: trades, balances, recent activity."""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

STORAGE = Path.home() / ".pod_the_trader"
LOG_FILE = Path("pod_the_trader.log")  # default log location, in cwd
TRADE_HISTORY = STORAGE / "trade_history.json"
KEYPAIR_FILE = STORAGE / "keypair.json"
LEVEL5_CREDS = STORAGE / "level5_credentials.json"


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def fmt_ts(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except Exception:
        return iso


def show_wallet() -> str | None:
    section("Wallet")
    if not KEYPAIR_FILE.exists():
        print("  (no wallet)")
        return None
    try:
        from solders.keypair import Keypair

        kp = Keypair.from_bytes(bytes(json.loads(KEYPAIR_FILE.read_text())))
        addr = str(kp.pubkey())
        print(f"  address: {addr}")
        return addr
    except Exception as e:
        print(f"  (failed to load: {e})")
        return None


def show_level5_balance() -> None:
    section("Level5 Balance")
    if not LEVEL5_CREDS.exists():
        print("  (no Level5 credentials)")
        return
    creds = json.loads(LEVEL5_CREDS.read_text())
    token = creds.get("api_token")
    if not token:
        print("  (no api token)")
        return

    import urllib.request

    try:
        url = f"https://api.level5.cloud/proxy/{token}/balance"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "pod-the-trader/0.1 (status check)"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        usdc = data.get("usdc_balance", 0) / 1_000_000
        credit = data.get("credit_balance", 0) / 1_000_000
        total = usdc + credit
        print(f"  USDC:    ${usdc:.4f}")
        print(f"  Credits: ${credit:.4f}")
        print(f"  Total:   ${total:.4f}")
        print(f"  Active:  {data.get('is_active')}")
        print(f"  Dashboard: https://level5.cloud/dashboard/{token}")
    except Exception as e:
        print(f"  (failed to fetch: {e})")


def show_sol_balance(address: str) -> None:
    section("Solana Wallet Balance")
    if not address:
        return
    import urllib.request

    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [address],
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.mainnet-beta.solana.com",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        lamports = data.get("result", {}).get("value", 0)
        sol = lamports / 1_000_000_000
        print(f"  SOL: {sol:.6f}")
    except Exception as e:
        print(f"  (failed to fetch: {e})")


def show_trade_history() -> None:
    section("Trade History (CSV ledger)")
    from pod_the_trader.data.ledger import TradeLedger

    ledger = TradeLedger()
    trades = ledger.read_all()
    if not trades:
        print("  No trades recorded.")
        return

    print(f"  Total trades: {len(trades)}")
    print()
    print(
        f"  {'#':<3} {'time':<22} {'side':<5} "
        f"{'in (SOL)':>10} {'out (tokens)':>16} {'value':>10} "
        f"{'gas':>9}  signature"
    )
    for i, t in enumerate(trades[-20:], start=max(1, len(trades) - 19)):
        ts = fmt_ts(t.timestamp)[:19]
        ia = t.input_amount_ui
        oa = t.actual_out_ui
        val = t.input_value_usd if t.side == "buy" else t.output_value_usd
        gas = t.gas_usd
        sig = (t.signature or "")[:20]
        print(
            f"  {i:<3} {ts:<22} {t.side:<5} {ia:>10.6f} {oa:>16,.4f} "
            f"${val:>8.2f} ${gas:>7.4f}  {sig}..."
        )

    print()
    summary = ledger.summary()
    sign = "+" if summary["realized_pnl_usd"] >= 0 else ""
    print(f"  Buys: {summary['buy_count']}  Sells: {summary['sell_count']}")
    print(
        f"  Realized PnL: {sign}${summary['realized_pnl_usd']:.4f} "
        f"({summary['realized_pnl_pct']:+.2f}%)"
    )
    print(f"  Win rate: {summary['win_rate_pct']:.0f}%")
    print(f"  Avg buy:  ${summary['avg_buy_price']:.8f}")
    print(f"  Avg sell: ${summary['avg_sell_price']:.8f}")
    print(f"  Bot pos:  {summary['tokens_held']:,.4f} tokens (bot trades only)")
    print(
        f"  Gas spent: ${summary['gas_spent_usd']:.4f} "
        f"({summary['gas_spent_sol']:.6f} SOL)"
    )


def show_recent_log() -> None:
    section("Recent Log Activity")
    # Try cwd first, then home
    candidates = [LOG_FILE, Path.home() / "pod_the_trader.log"]
    log = next((c for c in candidates if c.exists()), None)
    if not log:
        print("  (no log file found in cwd or home)")
        return

    print(f"  log: {log}")
    size = log.stat().st_size
    print(f"  size: {size:,} bytes")

    try:
        # Tail last 200 lines via subprocess to avoid loading huge files
        result = subprocess.run(
            ["tail", "-n", "200", str(log)],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.splitlines()
    except Exception as e:
        print(f"  (failed to read: {e})")
        return

    # Count interesting events
    cycles = sum(1 for line in lines if "Agent response:" in line)
    swaps_called = sum(1 for line in lines if "execute_swap" in line and "Tool call:" in line)
    swap_success = sum(1 for line in lines if '"success": true' in line and "signature" in line)
    swap_fail = sum(1 for line in lines if '"success": false' in line)
    minimax_errors = sum(1 for line in lines if "Minimax midstream" in line)
    cycle_errors = sum(1 for line in lines if "Trading cycle error" in line)
    low_balance = sum(1 for line in lines if "balance low" in line)

    print(f"  (last 200 log lines)")
    print(f"  cycles completed:    {cycles}")
    print(f"  execute_swap calls:  {swaps_called}")
    print(f"  swap successes:      {swap_success}")
    print(f"  swap failures:       {swap_fail}")
    print(f"  minimax errors:      {minimax_errors}")
    print(f"  cycle errors:        {cycle_errors}")
    print(f"  low balance pauses:  {low_balance}")

    print()
    print("  --- last 5 agent responses ---")
    responses = [line for line in lines if "Agent response:" in line]
    for r in responses[-5:]:
        print(f"  {r[:300]}")


def main() -> None:
    print("================================")
    print(" Pod The Trader — Status")
    print(f" {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("================================")

    show_trade_history()
    addr = show_wallet()
    show_sol_balance(addr or "")
    show_level5_balance()
    show_recent_log()


if __name__ == "__main__":
    main()
