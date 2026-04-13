"""Entry point: wire everything together and run the startup flow."""

import asyncio
import contextlib
import logging
import signal
import sys
import uuid
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from pod_the_trader.agent.core import TradingAgent
from pod_the_trader.agent.memory import ConversationMemory
from pod_the_trader.config import Config, ConfigError
from pod_the_trader.data.ledger import TradeLedger
from pod_the_trader.data.price_log import PriceLog
from pod_the_trader.data.wallet_log import WalletLog, WalletSnapshot
from pod_the_trader.level5.auth import Level5Auth
from pod_the_trader.level5.client import Level5Client
from pod_the_trader.level5.poller import BalancePoller, FundingOrchestrator
from pod_the_trader.tools import create_registry
from pod_the_trader.trading.dex import JupiterDex
from pod_the_trader.trading.portfolio import Portfolio
from pod_the_trader.trading.transaction import TransactionBuilder
from pod_the_trader.wallet.manager import WalletManager
from pod_the_trader.wallet.setup import WalletSetup

logger = logging.getLogger("pod_the_trader")


def _configure_logging(config: Config) -> None:
    """Set up logging with split file (verbose) + console (minimal) handlers.

    File gets everything at DEBUG with full format — that's the forensic
    log for digging into what happened.

    Console (stderr) gets only WARNING+ with a minimal format — errors are
    visible, everything else stays out of the user's face. Noisy libraries
    (httpx, httpcore, openai) are pinned to WARNING so their INFO-level
    request logs don't spam the console.

    User-facing cycle summaries and trade events are printed directly to
    stdout via `print()`, not through the logger.
    """
    log_format = config.get("logging.format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    log_file = config.get("logging.file", "pod_the_trader.log")
    max_bytes = config.get("logging.max_bytes", 52428800)
    backup_count = config.get("logging.backup_count", 5)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # let the file handler get everything

    # File handler — verbose, DEBUG, full format
    file_handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(file_handler)

    # Console handler (stderr) — minimal, WARNING+, simple format
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(console)

    # Silence noisy third-party libs at the console level (they still log
    # to the file at DEBUG via their own loggers → root → file_handler).
    for noisy in ("httpx", "httpcore", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Make stdout line-buffered so print() output is visible in real time
    # even when redirected to a file.
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]


async def async_main(config_path: str | None = None) -> None:
    """Main async entry point."""
    load_dotenv()

    # 1. Load and validate config
    try:
        config = Config(config_path)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    # 2. Configure logging
    _configure_logging(config)
    logger.info("Pod The Trader starting up...")

    storage_dir = config.get("storage.base_dir", "~/.pod_the_trader")
    rpc_url = config.get("solana.rpc_url", "https://api.mainnet-beta.solana.com")

    # 3. Level5 auth
    level5_auth = Level5Auth(storage_dir)
    creds = level5_auth.setup_interactive()

    if creds is None or not creds.api_token:
        if creds and creds.is_new:
            pass  # Will register below
        else:
            logger.critical(
                "Level5 credentials required — it is the only LLM provider. "
                "Set LEVEL5_API_TOKEN or run interactive setup."
            )
            sys.exit(1)

    # 4. Wallet setup
    wallet_mgr = WalletManager(storage_dir)
    wallet_setup = WalletSetup(wallet_mgr)
    keypair = wallet_setup.run()

    if keypair is None:
        logger.critical("No wallet configured. Exiting.")
        sys.exit(1)

    wallet_address = str(keypair.pubkey())
    logger.info("Using wallet: %s", wallet_address)

    # 5. Transaction builder
    tx_builder = TransactionBuilder(rpc_url)

    # 6. Level5 client
    api_token = creds.api_token if creds else None
    deposit_address = creds.deposit_address if creds else None

    async with Level5Client(
        api_token=api_token,
        deposit_address=deposit_address,
        base_url=config.get("level5.base_url", "https://api.level5.cloud"),
    ) as level5_client:
        # 7. Register with Level5 if new
        if creds and creds.is_new:
            logger.info("Registering with Level5...")
            account = await level5_client.register()
            creds.api_token = account.api_token
            creds.deposit_address = account.deposit_address
            creds.is_new = False
            level5_auth.save(creds)
            deposit_address = account.deposit_address
            print(f"\nDashboard: {level5_client.get_dashboard_url()}")

            # 8. Wait for funding and auto-deposit
            poller = BalancePoller(
                rpc_url=rpc_url,
                wallet_address=wallet_address,
                interval=config.get("polling.funding_interval_seconds", 10),
                timeout=config.get("polling.funding_timeout_seconds", 3600),
            )
            orchestrator = FundingOrchestrator(poller, level5_client, tx_builder)

            print(f"\nDeposit SOL to your wallet: {wallet_address}")
            print("Waiting for funding...")

            try:
                await orchestrator.wait_and_deposit(
                    keypair=keypair,
                    deposit_address=deposit_address,
                    deposit_amount_sol=0.1,
                    funding_threshold_sol=0.15,
                )
            except TimeoutError as e:
                logger.error("Funding timed out: %s", e)
                sys.exit(1)

        logger.info("Dashboard: %s", level5_client.get_dashboard_url())

        # 9. Jupiter DEX
        async with JupiterDex(
            quote_url=config.get("jupiter.quote_url"),
            swap_url=config.get("jupiter.swap_url"),
            price_url=config.get("jupiter.price_url"),
            rpc_url=rpc_url,
        ) as jupiter_dex:
            # 10. Portfolio
            portfolio = Portfolio(
                rpc_url=rpc_url,
                jupiter_dex=jupiter_dex,
                storage_dir=storage_dir,
            )

            # 11. Persistent data: trade ledger + price log + wallet log
            session_id = uuid.uuid4().hex[:12]
            session_start = datetime.now(UTC)
            ledger = TradeLedger(storage_dir)
            price_log = PriceLog(storage_dir)
            wallet_log = WalletLog(storage_dir)

            # 12. Tool registry
            registry = create_registry(
                config=config,
                portfolio=portfolio,
                jupiter_dex=jupiter_dex,
                transaction_builder=tx_builder,
                rpc_url=rpc_url,
                wallet_address=wallet_address,
                ledger=ledger,
                price_log=price_log,
                session_id=session_id,
            )

            # Set the trading keypair on the registry
            if hasattr(registry, "_set_trading_keypair"):
                registry._set_trading_keypair(keypair)

            # 13. Memory
            memory = ConversationMemory(storage_dir)
            memory.load()

            # 14. Agent
            agent = TradingAgent(
                config,
                level5_client,
                registry,
                memory,
                ledger=ledger,
                price_log=price_log,
                jupiter_dex=jupiter_dex,
                wallet_log=wallet_log,
                portfolio=portfolio,
                wallet_address=wallet_address,
            )
            agent.bootstrap_context()
            await agent.print_startup_banner()

            # 15. Signal handling
            shutdown_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            shutdown_count = 0

            def _signal_handler() -> None:
                nonlocal shutdown_count
                shutdown_count += 1
                if shutdown_count == 1:
                    print("\n  (shutdown signal received — finishing cycle)", flush=True)
                    logger.info("Shutdown signal received. Finishing current cycle...")
                    shutdown_event.set()
                else:
                    print("  (second signal — forcing exit)", flush=True)
                    logger.warning("Second signal received. Forcing exit.")
                    # Hard-exit without raising through asyncio (avoids the
                    # nasty traceback from SystemExit in a signal handler).
                    import os

                    os._exit(130)

            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _signal_handler)

            # 16. Run
            logger.info(
                "Pod The Trader is live. Session: %s. Trading loop starting.",
                session_id,
            )
            try:
                await agent.trade_loop(shutdown_event)
            finally:
                memory.save()
                live_snap = None
                try:
                    live_snap = await agent._fetch_portfolio_snapshot()
                except Exception as e:
                    logger.debug("Shutdown live snapshot fetch failed: %s", e)
                _print_shutdown_summary(
                    ledger,
                    wallet_log,
                    session_start,
                    live_snapshot=live_snap,
                    target_symbol=agent._target_symbol,
                )
                logger.info(
                    "Shutdown complete. Trades this session: %d",
                    agent.trade_count,
                )


def _build_snap(
    live_snapshot: dict | None, wallet_log: WalletLog
) -> WalletSnapshot | None:
    """Prefer a live (just-fetched) snapshot over the last CSV row.

    The CSV log can contain stale zeros from earlier failed RPC reads; a
    live fetch is the only trustworthy view at shutdown.
    """
    from pod_the_trader.data.wallet_log import now_iso

    if live_snapshot is not None:
        # Pull token_mint from the latest stored snapshot if present so the
        # reconciliation block knows whether to include the token line.
        prev = wallet_log.latest()
        return WalletSnapshot(
            timestamp=now_iso(),
            wallet=prev.wallet if prev else "",
            sol_balance=float(live_snapshot.get("sol_ui", 0.0)),
            sol_value_usd=float(live_snapshot.get("sol_value_usd", 0.0)),
            token_mint=prev.token_mint if prev else "",
            token_balance=float(live_snapshot.get("token_ui", 0.0)),
            token_decimals=prev.token_decimals if prev else 0,
            token_price_usd=float(live_snapshot.get("token_price_usd", 0.0)),
            token_value_usd=float(live_snapshot.get("token_value_usd", 0.0)),
            total_value_usd=float(live_snapshot.get("total_usd", 0.0)),
        )
    return wallet_log.latest()


def _print_shutdown_summary(
    ledger: TradeLedger,
    wallet_log: WalletLog,
    session_start: datetime,
    *,
    live_snapshot: dict | None = None,
    target_symbol: str = "",
) -> None:
    """Print a P&L summary on shutdown: session + all-time + on-chain reality.

    ``live_snapshot`` is a freshly-fetched portfolio dict (from
    ``TradingAgent._fetch_portfolio_snapshot``) and is preferred over the
    last entry in ``wallet_snapshots.csv``. The CSV may contain stale
    zeros from earlier failed RPC reads, so a live fetch is the only
    trustworthy view at shutdown.

    ``target_symbol`` is the ticker for the configured target token (e.g.
    ``"SQUIRE"``); used as a label in the on-chain wallet block.
    """
    all_time = ledger.summary()
    session = ledger.summary(since=session_start)
    snap = _build_snap(live_snapshot, wallet_log)
    label = target_symbol or "target token"

    def _fmt_ledger_block(label: str, s: dict) -> list[str]:
        if s["trade_count"] == 0:
            return [f"  {label}: no bot trades"]
        sign = "+" if s["realized_pnl_usd"] >= 0 else ""

        # Mark-to-market: what would the position be worth if liquidated
        # right now? Uses the on-chain token balance (real position) and
        # the latest token price from the wallet snapshot.
        m2m_line: str | None = None
        if snap is not None and snap.token_mint:
            position_value = snap.token_balance * snap.token_price_usd
            net_invested = (
                s["buy_volume_usd"] - s["sell_volume_usd"] + s["gas_spent_usd"]
            )
            m2m_pnl = position_value - net_invested
            m2m_pct = (m2m_pnl / s["buy_volume_usd"] * 100) if s["buy_volume_usd"] else 0.0
            msign = "+" if m2m_pnl >= 0 else ""
            m2m_line = (
                f"    mark-to-mkt:   {msign}${m2m_pnl:.4f} ({m2m_pct:+.2f}%)  "
                f"[on-chain {snap.token_balance:,.2f} @ ${snap.token_price_usd:.8f} "
                f"= ${position_value:.4f}]"
            )

        block = [
            f"  {label}:",
            f"    bot trades:    {s['trade_count']} "
            f"({s['buy_count']} buys, {s['sell_count']} sells)",
            f"    buy volume:    ${s['buy_volume_usd']:.4f}",
            f"    sell volume:   ${s['sell_volume_usd']:.4f}",
            f"    realized PnL:  {sign}${s['realized_pnl_usd']:.4f} "
            f"({s['realized_pnl_pct']:+.2f}%)",
        ]
        if m2m_line is not None:
            block.append(m2m_line)
        block.extend(
            [
                f"    win rate:      {s['win_rate_pct']:.0f}%",
                f"    avg buy:       ${s['avg_buy_price']:.8f}",
                f"    avg sell:      ${s['avg_sell_price']:.8f}",
                f"    bot pos:       {s['tokens_held']:,.4f} tokens (from bot trades only)",
                f"    gas spent:     ${s['gas_spent_usd']:.4f} ({s['gas_spent_sol']:.6f} SOL)",
            ]
        )
        return block

    def _fmt_wallet_block() -> list[str]:
        if snap is None:
            return ["  on-chain wallet:  (no snapshot yet)"]
        block = [
            "  On-chain wallet (real position):",
            f"    SOL:             {snap.sol_balance:.6f} (${snap.sol_value_usd:.4f})",
        ]
        if snap.token_mint:
            block.append(
                f"    {label}:    {snap.token_balance:,.4f} "
                f"@ ${snap.token_price_usd:.8f} "
                f"= ${snap.token_value_usd:.4f}"
            )
        block.append(f"    total value:     ${snap.total_value_usd:.4f}")
        return block

    # Reconciliation: bot ledger position vs on-chain reality
    reconcile_lines: list[str] = []
    if snap is not None and snap.token_mint and all_time["trade_count"] > 0:
        bot_pos = all_time["tokens_held"]
        actual_pos = snap.token_balance
        diff = actual_pos - bot_pos
        if abs(diff) > 0.01:
            reconcile_lines = [
                "  Reconciliation:",
                f"    bot ledger says:  {bot_pos:,.4f} tokens",
                f"    on-chain shows:   {actual_pos:,.4f} tokens",
                f"    difference:       {diff:+,.4f} tokens "
                "(external transfers — not tracked by bot)",
            ]

    lines = [
        "",
        "================================================",
        " Pod The Trader — Shutdown Summary",
        "================================================",
        *_fmt_ledger_block("This session", session),
        "",
        *_fmt_ledger_block("All time", all_time),
        "",
        *_fmt_wallet_block(),
    ]
    if reconcile_lines:
        lines.append("")
        lines.extend(reconcile_lines)
    lines.extend(["================================================", ""])
    print("\n".join(lines))


def _parse_cli_args(argv: list[str]) -> tuple[str | None, str]:
    """Parse command-line args into (config_path, ui_mode).

    ui_mode ∈ {"auto", "tui", "cli"}. "auto" picks tui iff stdout is a TTY.
    """
    config_path: str | None = None
    ui_mode = "auto"
    for arg in argv:
        if arg == "--tui":
            ui_mode = "tui"
        elif arg == "--cli":
            ui_mode = "cli"
        elif not arg.startswith("--"):
            config_path = arg
    return config_path, ui_mode


def _resolve_ui_mode(requested: str) -> str:
    if requested in ("tui", "cli"):
        return requested
    # auto: TUI only if stdout is a real terminal
    return "tui" if sys.stdout.isatty() else "cli"


def main() -> None:
    """Sync entry point."""
    config_path, ui_mode = _parse_cli_args(sys.argv[1:])
    resolved = _resolve_ui_mode(ui_mode)
    try:
        if resolved == "tui":
            asyncio.run(async_main_tui(config_path))
        else:
            asyncio.run(async_main(config_path))
    except KeyboardInterrupt:
        print("\nShutdown.")


async def async_main_tui(config_path: str | None = None) -> None:
    """TUI entry point: launch the Textual dashboard.

    Mirrors async_main but launches a PodDashboardApp instead of running
    the trade loop directly in the terminal. The app runs the trade loop
    as a Textual worker.
    """
    # Lazy-import so the CLI-only path doesn't pay the Textual import cost.
    from pod_the_trader.tui.app import PodDashboardApp

    load_dotenv()

    try:
        config = Config(config_path)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    _configure_logging(config)
    logger.info("Pod The Trader (TUI) starting up...")

    storage_dir = config.get("storage.base_dir", "~/.pod_the_trader")
    rpc_url = config.get("solana.rpc_url", "https://api.mainnet-beta.solana.com")

    level5_auth = Level5Auth(storage_dir)
    creds = level5_auth.setup_interactive()
    if (creds is None or not creds.api_token) and not (creds and creds.is_new):
        logger.critical("Level5 credentials required.")
        sys.exit(1)

    wallet_mgr = WalletManager(storage_dir)
    wallet_setup = WalletSetup(wallet_mgr)
    keypair = wallet_setup.run()
    if keypair is None:
        logger.critical("No wallet configured. Exiting.")
        sys.exit(1)

    wallet_address = str(keypair.pubkey())
    tx_builder = TransactionBuilder(rpc_url)

    async with Level5Client(
        api_token=creds.api_token if creds else None,
        deposit_address=creds.deposit_address if creds else None,
        base_url=config.get("level5.base_url", "https://api.level5.cloud"),
    ) as level5_client:
        if creds and creds.is_new:
            account = await level5_client.register()
            creds.api_token = account.api_token
            creds.deposit_address = account.deposit_address
            creds.is_new = False
            level5_auth.save(creds)

        async with JupiterDex(
            quote_url=config.get("jupiter.quote_url"),
            swap_url=config.get("jupiter.swap_url"),
            price_url=config.get("jupiter.price_url"),
            rpc_url=rpc_url,
        ) as jupiter_dex:
            portfolio = Portfolio(
                rpc_url=rpc_url,
                jupiter_dex=jupiter_dex,
                storage_dir=storage_dir,
            )

            session_id = uuid.uuid4().hex[:12]
            session_start = datetime.now(UTC)
            ledger = TradeLedger(storage_dir)
            price_log = PriceLog(storage_dir)
            wallet_log = WalletLog(storage_dir)
            target_mint = config.get("trading.target_token_address", "")

            # Build the dashboard app (it IS the Publisher).
            app = PodDashboardApp(
                ledger=ledger,
                price_log=price_log,
                target_mint=target_mint,
            )

            registry = create_registry(
                config=config,
                portfolio=portfolio,
                jupiter_dex=jupiter_dex,
                transaction_builder=tx_builder,
                rpc_url=rpc_url,
                wallet_address=wallet_address,
                ledger=ledger,
                price_log=price_log,
                session_id=session_id,
                publisher=app,
            )
            if hasattr(registry, "_set_trading_keypair"):
                registry._set_trading_keypair(keypair)

            memory = ConversationMemory(storage_dir)
            memory.load()

            agent = TradingAgent(
                config,
                level5_client,
                registry,
                memory,
                ledger=ledger,
                price_log=price_log,
                jupiter_dex=jupiter_dex,
                wallet_log=wallet_log,
                portfolio=portfolio,
                wallet_address=wallet_address,
                publisher=app,
            )
            agent.bootstrap_context()

            # Hand the trade_loop coroutine to the app as a worker factory.
            app._run_agent = agent.trade_loop

            try:
                await app.run_async()
            finally:
                memory.save()
                live_snap = None
                try:
                    live_snap = await agent._fetch_portfolio_snapshot()
                except Exception as e:
                    logger.debug("Shutdown live snapshot fetch failed: %s", e)
                _print_shutdown_summary(
                    ledger,
                    wallet_log,
                    session_start,
                    live_snapshot=live_snap,
                    target_symbol=agent._target_symbol,
                )


if __name__ == "__main__":
    main()
