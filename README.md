# Pod The Trader

An autonomous Solana trading agent. An LLM makes live buy/sell decisions on a single target token from a real mainnet wallet, and a btop-style Textual dashboard shows you what it's doing.

![Pod The Trader dashboard](docs/dashboard.png)

## What it does

- **Runs a cycle every N seconds.** Each cycle fetches on-chain balances, recent price history, and trade history, hands them to an LLM, and records the model's decision (BUY, SELL, or HOLD) along with the reason.
- **Executes real swaps** through Jupiter DEX aggregator. SOL, USDC, and one configured target token are the only mints it can touch — a tool-layer guard rejects anything else.
- **Tracks cost basis** in a lot-based ledger. Every position change (bot trade, external deposit, external withdrawal, gas) is a cost-basis event; realized and unrealized P&L come from FIFO lot matching instead of volume math. A reconciler runs before every cycle and at startup to absorb any on-chain drift the bot didn't cause.
- **Shows it all live** in a 3×3 Textual grid: Portfolio, Health (P&L gauge), Trade ledger, Price Action (SOL + target sparklines), Level5 Billing (inference cost), Cycle status, and a scrollable log tail.
- **Bills its own inference** through [Level5](https://level5.cloud) — a Solana-native pay-as-you-go LLM proxy. You fund a Level5 account once with USDC and the bot draws from it per request.

## Quick install

**Linux / macOS:**

```bash
curl -LsSf https://raw.githubusercontent.com/Sortis-AI/pod_trades/main/install.sh | bash
```

Installs git, [`uv`](https://docs.astral.sh/uv/), and Python 3.12 (via snap → apt on Linux or brew on macOS), clones the repo to `~/pod-the-trader`, runs `uv sync`, and drops a launcher at `~/.local/bin/pod-the-trader`.

**Windows 10 / 11** (run in PowerShell inside Windows Terminal — not legacy `cmd.exe`):

```powershell
irm https://raw.githubusercontent.com/Sortis-AI/pod_trades/main/install.ps1 | iex
```

Installs git (via winget), `uv`, and Python 3.12, clones the repo to `%USERPROFILE%\pod-the-trader`, runs `uv sync`, and drops a launcher at `%USERPROFILE%\.local\bin\pod-the-trader.cmd`.

Then open a new shell and run:

```bash
pod-the-trader
```

On first launch you'll see a disclaimer. Type `I ACCEPT` to continue, then walk through the Level5 registration and wallet-generation prompts. If you let the wizard generate a new wallet, it will print the private key exactly once and require you to type `I SAVED IT` to confirm you've backed it up — **this is your only chance to capture it**, so copy it into a password manager or write it down before continuing.

To upgrade later, run `pod-the-trader update` — it pulls the latest code and re-runs `uv sync`.

> **Heads-up:** the bot moves real funds on Solana mainnet. Start by funding the wallet with a small amount you're comfortable losing entirely, watch it run for a few cycles, and read the disclaimer carefully — it's shown at every launch for a reason.

## Requirements

- **OS:** Linux, macOS, or Windows 10/11. On Windows, use PowerShell inside Windows Terminal — legacy `cmd.exe` cannot render the Textual dashboard.
- **Python:** 3.12+
- **Wallet:** a Solana keypair. The installer can generate one for you on first run; the private key lives at `~/.pod_the_trader/keypair.json` (`%USERPROFILE%\.pod_the_trader\keypair.json` on Windows) and is never transmitted.
- **Level5 account:** the installer walks you through creating one. Fund it with USDC (or use whatever promotional credits Level5 issues) — the bot refuses to trade when the balance falls below a configured floor.
- **Some SOL** in your trading wallet to cover Jupiter gas.

## Configuration

Defaults live in `pod_the_trader/config/defaults.yaml`. Override any of them with a user YAML file passed via `--config`:

```bash
pod-the-trader --config ~/my-trader.yaml
```

The knobs you'll care about most:

| Key | Default | What it does |
|---|---|---|
| `trading.target_token_address` | `EN2nn…SQUIRE` | The token the bot is trying to trade profitably. Can also be set via `TARGET_TOKEN_ADDRESS` env var. |
| `trading.max_position_size_usdc` | `100` | Hard cap on position size, in USDC. |
| `trading.max_slippage_bps` | `50` | Max Jupiter slippage tolerance, in basis points. |
| `trading.min_trade_size_usdc` | `1.0` | Swaps below this USD value are rejected at the tool layer. Network fees would exceed the trade value. |
| `trading.max_daily_trades` | `20` | Per-day safety cap. |
| `trading.cooldown_seconds` | `300` | Seconds to wait between cycles. |
| `trading.max_price_impact_pct` | `5.0` | Refuse swaps with worse Jupiter-reported price impact. |
| `level5.min_balance_threshold_usdc` | `0.1` | Pause trading when Level5 balance drops below this floor. |
| `level5.base_domain` | `level5.cloud` | Host for the Level5 API (`api.<domain>`) and dashboard (`<domain>/dashboard/<token>`). Override on the command line with `--base-domain`. |
| `agent.model` | `minimax-m2.7` | The LLM Level5 proxies to. |
| `agent.max_iterations_per_turn` | `10` | Max tool-call iterations per cycle. |

## Usage

```bash
# Full dashboard (default if stdout is a real terminal)
pod-the-trader

# Plain CLI mode (default if stdout is piped)
pod-the-trader --cli

# Force TUI even when output is piped
pod-the-trader --tui

# Custom config
pod-the-trader --config path/to/config.yaml

# Point at an alternate Level5 deployment (default: level5.cloud)
pod-the-trader --base-domain usepod.ai

# Pull the latest code and re-sync dependencies
pod-the-trader update
```

Keybindings inside the TUI:

| Key | Action |
|---|---|
| `q` | Quit (graceful shutdown — finishes current cycle) |
| `Ctrl+C` | Same as `q` |
| Click wallet address in Portfolio panel | Copy wallet to clipboard |
| Click dashboard URL in Level5 panel | Open Level5 dashboard in default browser |

On exit the bot prints a summary of the current session: closed trades, realized P&L, unrealized P&L, open position, gas spent.

## How it works

```
┌─ Trading agent (async) ───────────────────────────────────────┐
│                                                               │
│   Level5 balance  ──┐                                         │
│   Price sample    ──┼──▶ Reconcile on-chain ──▶ Run LLM turn  │
│   Wallet snapshot ──┘                              │          │
│                                                    │          │
│                            Tool registry ◀─────────┤          │
│                              │                     │          │
│       ┌──────────────────────┼─────────────────────┐          │
│       │            │         │          │         │          │
│     Market      Portfolio  History  Trading    Solana         │
│     tools       tools      tools    tools      tools          │
│                                       │                       │
│                           Jupiter DEX aggregator              │
│                                       │                       │
└───────────────────────────────────────┼───────────────────────┘
                                        │
                              Solana mainnet
```

- **Agent** (`pod_the_trader/agent/core.py`) runs the cycle loop, builds the system prompt, enforces that BUY/SELL decisions are backed by an actual `execute_swap` call, and publishes events to a TUI publisher.
- **Tools** (`pod_the_trader/tools/`) are the only way the model touches the world. Every swap entry point is gated by a route guard (SOL/USDC/target only) and a minimum-trade-size guard ($1 default).
- **Lot ledger** (`pod_the_trader/data/lot_ledger.py`) is an event-sourced cost-basis ledger. FIFO matching produces closed segments with entry and exit prices; realized P&L falls out of those directly.
- **Reconciler** (`pod_the_trader/data/reconciler.py`) compares ledger open-lot sum against the actual on-chain balance every cycle and at startup, and emits synthetic events to absorb any drift.
- **TUI** (`pod_the_trader/tui/`) is a Textual app that implements the Publisher protocol; the agent sends events, widgets update reactively.
- **Level5 client** (`pod_the_trader/level5/`) handles account registration, balance queries, and auto-deposit from the trading wallet when inference credits run low.

## Safety

This software moves real funds on mainnet. Read [`pod_the_trader/disclaimer.py`](pod_the_trader/disclaimer.py) for the full list of things that can go wrong. Short version:

- The LLM can hallucinate, misread data, and pick bad trade sizes. It has done all of these during development.
- There is no testnet mode, no dry run, and no undo for on-chain transactions.
- Your private key lives at `~/.pod_the_trader/keypair.json` (or `%USERPROFILE%\.pod_the_trader\keypair.json` on Windows). Anyone with access to that file can drain your wallet. The bot restricts it to the owning user only — `chmod 0o600` on POSIX, `icacls /inheritance:r /grant:r <user>:F` on Windows — but filesystem permissions are the last line of defense, not the first.
- Memecoin trading is high-risk; most positions lose money. **Do not put more into the trading wallet than you can afford to lose entirely.**

The bot enforces several hard guards to reduce foot-gun potential:

- **Tradeable universe**: only SOL, USDC, and the configured target token. Anything else is rejected by the tool layer.
- **Minimum trade size**: swaps under `min_trade_size_usdc` (default $1) are rejected so dust trades don't bleed fees.
- **Decision-execution enforcement**: if the model writes `DECISION: SELL` but never calls `execute_swap`, the trade loop catches it, nudges the model to either execute or downgrade, and falls back to `HOLD` if it still doesn't comply — so the dashboard can never show a sell that didn't happen.
- **Disclaimer on every startup**: no bypass, no env var shortcut. You type `I ACCEPT` every time.

## Development

```bash
# Install dev dependencies
uv sync --extra dev

# Run the full suite
uv run pytest tests/

# Lint
uv run ruff check pod_the_trader/ tests/
uv run ruff format pod_the_trader/ tests/

# End-to-end smoke test (clears conversation state, runs for 60s, checks the log)
bash scripts/check.sh 60
```

The test suite is mostly unit tests against real component boundaries with mocked external services (Level5, Jupiter, Solana RPC). The TUI widgets are tested through `Textual App.run_test()` so reactive behavior runs for real.

## Repository layout

```
pod_the_trader/
├── agent/          # LLM trading loop + conversation memory
├── config/         # defaults.yaml + validated loader
├── data/           # trade ledger, lot ledger, price log, wallet log, reconciler
├── level5/         # Level5 account, balance, auto-funding
├── tools/          # LLM-callable tools (swap, portfolio, market, history)
├── trading/        # Jupiter DEX, Portfolio, transaction builder
├── tui/            # Textual dashboard + widgets
├── wallet/         # Keypair management + setup prompts
├── disclaimer.py   # Startup disclaimer (I ACCEPT gate)
└── main.py         # Entry point (CLI + TUI paths)

tests/              # Unit + integration + e2e tests
scripts/            # check.sh, e2e_test.sh, debug helpers
docs/               # Screenshots + user-facing docs
install.sh          # One-shot installer (curl | bash)
```

## License

Pod The Trader is free software released under the **GNU General Public License v3.0 or later**. See [`LICENSE`](LICENSE) for the full text.

    Pod The Trader is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
    GNU General Public License for more details.

## Disclaimer

This is the full disclaimer shown at every launch. You must type `I ACCEPT` before the bot will start. It is reproduced here verbatim from [`pod_the_trader/disclaimer.py`](pod_the_trader/disclaimer.py) so you can read it before you install.

> **POD THE TRADER — DISCLAIMER**
>
> This is autonomous trading software that will buy and sell real tokens on the Solana mainnet using your wallet and your money. Before you start it, read this and make sure you understand what you're agreeing to.
>
> 1. **REAL FUNDS.** Every trade moves actual SOL, USDC, and SPL tokens from your wallet. There is no testnet mode, no dry run, no simulation layer. Losses are real and on-chain transactions are irreversible.
>
> 2. **LLM-DRIVEN DECISIONS.** Trading decisions are made by a large language model (minimax-m2.7 via Level5). The model can hallucinate, misread market data, make arithmetic errors, pick bad sizes, or act on stale context. It has already done all of these during development. Do not assume it will make profitable trades.
>
> 3. **NO WARRANTY.** This software is experimental and provided as-is, with no guarantee of correctness, profitability, uptime, or data integrity. Recent history includes pricing bugs, unintended trade routes, dust trades, and decision-execution mismatches. More bugs almost certainly remain.
>
> 4. **YOU ARE THE OPERATOR.** You are responsible for monitoring the bot, setting sensible position limits, funding the wallet appropriately, and shutting it down if something looks wrong. The bot will not stop itself just because it's losing money.
>
> 5. **NOT FINANCIAL ADVICE.** Nothing this software outputs — on-screen, in logs, or in summaries — constitutes financial, legal, tax, or investment advice. Memecoin trading is high-risk and most positions lose money.
>
> 6. **KEY CUSTODY.** Your private key lives in `~/.pod_the_trader/`. Anyone with access to that file can drain your wallet. You are solely responsible for the security of that file and the machine it sits on.
>
> 7. **NO RECOURSE.** If the bot loses your money, executes an unintended trade, fails to execute an intended trade, or misreports P&L, there is no one to appeal to. Do not put more into this wallet than you can afford to lose entirely.
>
> By continuing, you confirm that you have read and understood the above, that you accept full responsibility for any losses, and that you are running this software voluntarily and at your own risk.
