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
from pod_the_trader.data.lot_ledger import LotLedger, migrate_from_trade_ledger
from pod_the_trader.data.price_log import PriceLog
from pod_the_trader.data.wallet_log import WalletLog, WalletSnapshot
from pod_the_trader.level5.auth import Level5Auth
from pod_the_trader.level5.client import Level5Client, Level5Error
from pod_the_trader.level5.poller import BalancePoller, FundingOrchestrator
from pod_the_trader.tools import create_registry
from pod_the_trader.trading.dex import SOL_MINT, USDC_MINT, JupiterDex
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
            try:
                account = await level5_client.register()
            except Level5Error as e:
                logger.error("Level5 registration failed: %s", e)
                print(
                    f"\nLevel5 registration failed: {e}\n\n"
                    "This usually means the Level5 API returned an "
                    "incomplete response. Try again in a moment, or "
                    "contact Level5 support if it persists.",
                    file=sys.stderr,
                )
                sys.exit(1)
            creds.api_token = account.api_token
            creds.deposit_address = account.deposit_address
            creds.deposit_code = account.deposit_code
            creds.dashboard_url = account.dashboard_url or level5_client.get_dashboard_url()
            creds.is_new = False
            level5_auth.save(creds)
            deposit_address = account.deposit_address
            print()
            print("Level5 account registered.")
            print(f"  Dashboard:    {creds.dashboard_url}")
            print(f"  Contract:     {account.deposit_address}")
            print(f"  Deposit code: {account.deposit_code}")
            print(f"  Status:       {account.status or 'pending_deposit'}")
            print()
            print("Fund the account via the dashboard above — the deposit")
            print("code is how Level5 routes your deposit to this account.")

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

            # 11. Persistent data: trade ledger + price log + wallet log +
            #     lot ledger. The lot ledger is the authoritative model for
            #     "what do I own and at what cost basis" — the trade ledger
            #     stays as a human-readable trade history.
            session_id = uuid.uuid4().hex[:12]
            session_start = datetime.now(UTC)
            ledger = TradeLedger(storage_dir)
            price_log = PriceLog(storage_dir)
            wallet_log = WalletLog(storage_dir)
            lot_ledger = LotLedger(storage_dir)
            if not lot_ledger.exists():
                migrate_from_trade_ledger(lot_ledger, ledger.read_all(), sol_mint=SOL_MINT)

            # 12. Tool registry
            registry = create_registry(
                config=config,
                portfolio=portfolio,
                jupiter_dex=jupiter_dex,
                transaction_builder=tx_builder,
                rpc_url=rpc_url,
                wallet_address=wallet_address,
                ledger=ledger,
                lot_ledger=lot_ledger,
                price_log=price_log,
                session_id=session_id,
            )

            # Set the trading keypair on the registry
            if hasattr(registry, "_set_trading_keypair"):
                registry._set_trading_keypair(keypair)

            # 13. Memory — start each process with a clean conversation. The
            # prior session's assistant messages could be mid-action (e.g.
            # "checking price…") and would prime the model to continue that
            # intent on cycle 1 before any new reasoning runs. Bootstrap
            # context (ledger + price log) provides continuity instead.
            memory = ConversationMemory(storage_dir)

            # 14. Agent
            agent = TradingAgent(
                config,
                level5_client,
                registry,
                memory,
                ledger=ledger,
                lot_ledger=lot_ledger,
                price_log=price_log,
                jupiter_dex=jupiter_dex,
                wallet_log=wallet_log,
                portfolio=portfolio,
                wallet_address=wallet_address,
            )
            await agent.bootstrap_context()
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
                    lot_ledger=lot_ledger,
                    live_snapshot=live_snap,
                    target_symbol=agent._target_symbol,
                    target_mint=config.get("trading.target_token_address", ""),
                )
                logger.info(
                    "Shutdown complete. Trades this session: %d",
                    agent.trade_count,
                )


def _build_snap(live_snapshot: dict | None, wallet_log: WalletLog) -> WalletSnapshot | None:
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
    lot_ledger: LotLedger | None = None,
    live_snapshot: dict | None = None,
    target_symbol: str = "",
    target_mint: str = "",
) -> None:
    """Print a P&L summary on shutdown using the lot ledger as the source of truth.

    The lot ledger tracks every position change — bot trades, deposits,
    withdrawals, external swaps — so realized and unrealized P&L come
    directly from cost-basis math. Reports SOL, USDC, and the configured
    target token side-by-side. The legacy ``TradeLedger`` is still passed
    in for a trade-count reference in the footer.

    ``live_snapshot`` is a freshly-fetched portfolio dict and is preferred
    over the last entry in ``wallet_snapshots.csv``; the CSV can contain
    stale zeros from failed RPC reads, so a live fetch is the only
    trustworthy view at shutdown.
    """
    snap = _build_snap(live_snapshot, wallet_log)
    label = target_symbol or "target token"

    # Resolve current spot prices. Prefer the live snapshot, fall back to
    # the last wallet log row, then to defaults.
    target_price = 0.0
    sol_price = 0.0
    usdc_price = 1.0
    if live_snapshot is not None:
        target_price = float(live_snapshot.get("token_price_usd", 0.0) or 0.0)
        sol_price = float(live_snapshot.get("sol_price_usd", 0.0) or 0.0)
        usdc_price = float(live_snapshot.get("usdc_price_usd", 0.0) or 0.0) or 1.0
    if target_price <= 0 and snap is not None:
        target_price = float(snap.token_price_usd or 0.0)

    def _fmt_lot_subblock(title: str, mint: str, price: float) -> list[str]:
        if lot_ledger is None or not mint:
            return []
        s = lot_ledger.summary(mint, price)
        if s["trade_close_count"] == 0 and s["open_qty"] == 0:
            return []
        rsign = "+" if s["realized_pnl_usd"] >= 0 else ""
        usign = "+" if s["unrealized_pnl_usd"] >= 0 else ""
        tsign = "+" if s["total_pnl_usd"] >= 0 else ""
        return [
            f"    {title}:",
            f"      closed trades:   {s['trade_close_count']}",
            f"      open qty:        {s['open_qty']:,.6f}",
            f"      cost basis:      ${s['cost_basis_usd']:.4f} (avg ${s['avg_cost_basis']:.8f})",
            f"      position value:  ${s['position_value_usd']:.4f} @ ${price:.8f}",
            f"      realized PnL:    {rsign}${s['realized_pnl_usd']:.4f}",
            f"      unrealized PnL:  {usign}${s['unrealized_pnl_usd']:.4f}",
            f"      total PnL:       {tsign}${s['total_pnl_usd']:.4f}",
            f"      gas spent:       ${s['gas_usd']:.4f}",
        ]

    def _fmt_lot_block() -> list[str]:
        if lot_ledger is None:
            return ["  Cost-basis ledger: (no lot ledger configured)"]
        sections: list[list[str]] = []
        sol_section = _fmt_lot_subblock("SOL", SOL_MINT, sol_price)
        if sol_section:
            sections.append(sol_section)
        usdc_section = _fmt_lot_subblock("USDC", USDC_MINT, usdc_price)
        if usdc_section:
            sections.append(usdc_section)
        if target_mint and target_mint != USDC_MINT:
            tgt_section = _fmt_lot_subblock(label, target_mint, target_price)
            if tgt_section:
                sections.append(tgt_section)
        if not sections:
            return ["  Cost-basis ledger: no positions tracked"]
        out = ["  Cost-basis ledger:"]
        for i, sec in enumerate(sections):
            if i > 0:
                out.append("")
            out.extend(sec)
        return out

    def _fmt_wallet_block() -> list[str]:
        if snap is None:
            return ["  on-chain wallet:  (no snapshot yet)"]
        block = [
            "  On-chain wallet (real position):",
            f"    SOL:             {snap.sol_balance:.6f} (${snap.sol_value_usd:.4f})",
        ]
        # USDC line — pulled from the live snapshot since wallet_log doesn't
        # carry it as a structured field.
        if live_snapshot is not None:
            usdc_ui = float(live_snapshot.get("usdc_ui", 0.0) or 0.0)
            usdc_value = float(live_snapshot.get("usdc_value_usd", 0.0) or 0.0)
            if usdc_ui > 0:
                block.append(f"    USDC:            {usdc_ui:,.4f} (${usdc_value:.4f})")
        if snap.token_mint:
            block.append(
                f"    {label}:    {snap.token_balance:,.4f} "
                f"@ ${snap.token_price_usd:.8f} "
                f"= ${snap.token_value_usd:.4f}"
            )
        block.append(f"    total value:     ${snap.total_value_usd:.4f}")
        return block

    trade_count = len(ledger.read_all())
    lines = [
        "",
        "================================================",
        " Pod The Trader — Shutdown Summary",
        "================================================",
        *_fmt_lot_block(),
        "",
        *_fmt_wallet_block(),
        "",
        f"  Legacy trade ledger: {trade_count} bot trades recorded",
        "================================================",
        "",
    ]
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
    # Require the user to accept the disclaimer on every startup. This runs
    # BEFORE any heavy setup (no wallet load, no network calls, no Textual
    # app) so a decline exits cleanly and leaves no side effects behind.
    from pod_the_trader.disclaimer import require_acceptance

    require_acceptance()

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
            try:
                account = await level5_client.register()
            except Level5Error as e:
                logger.error("Level5 registration failed: %s", e)
                print(
                    f"\nLevel5 registration failed: {e}\n\n"
                    "This usually means the Level5 API returned an "
                    "incomplete response. Try again in a moment, or "
                    "contact Level5 support if it persists.",
                    file=sys.stderr,
                )
                sys.exit(1)
            creds.api_token = account.api_token
            creds.deposit_address = account.deposit_address
            creds.deposit_code = account.deposit_code
            creds.dashboard_url = account.dashboard_url or level5_client.get_dashboard_url()
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
            lot_ledger = LotLedger(storage_dir)
            if not lot_ledger.exists():
                migrate_from_trade_ledger(lot_ledger, ledger.read_all(), sol_mint=SOL_MINT)
            target_mint = config.get("trading.target_token_address", "")

            # Build the dashboard app (it IS the Publisher).
            app = PodDashboardApp(
                ledger=ledger,
                lot_ledger=lot_ledger,
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
                lot_ledger=lot_ledger,
                price_log=price_log,
                session_id=session_id,
                publisher=app,
            )
            if hasattr(registry, "_set_trading_keypair"):
                registry._set_trading_keypair(keypair)

            memory = ConversationMemory(storage_dir)

            agent = TradingAgent(
                config,
                level5_client,
                registry,
                memory,
                ledger=ledger,
                lot_ledger=lot_ledger,
                price_log=price_log,
                jupiter_dex=jupiter_dex,
                wallet_log=wallet_log,
                portfolio=portfolio,
                wallet_address=wallet_address,
                publisher=app,
            )
            await agent.bootstrap_context()

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
                    lot_ledger=lot_ledger,
                    live_snapshot=live_snap,
                    target_symbol=agent._target_symbol,
                    target_mint=target_mint,
                )


if __name__ == "__main__":
    main()
