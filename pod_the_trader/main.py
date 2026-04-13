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
from pod_the_trader.data.wallet_log import WalletLog
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
    log_format = config.get(
        "logging.format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    log_file = config.get("logging.file", "pod_the_trader.log")
    max_bytes = config.get("logging.max_bytes", 52428800)
    backup_count = config.get("logging.backup_count", 5)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)  # let the file handler get everything

    # File handler — verbose, DEBUG, full format
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count
    )
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
                _print_shutdown_summary(ledger, wallet_log, session_start)
                logger.info(
                    "Shutdown complete. Trades this session: %d",
                    agent.trade_count,
                )


def _print_shutdown_summary(
    ledger: TradeLedger,
    wallet_log: WalletLog,
    session_start: datetime,
) -> None:
    """Print a P&L summary on shutdown: session + all-time + on-chain reality."""
    all_time = ledger.summary()
    session = ledger.summary(since=session_start)
    snap = wallet_log.latest()

    def _fmt_ledger_block(label: str, s: dict) -> list[str]:
        if s["trade_count"] == 0:
            return [f"  {label}: no bot trades"]
        sign = "+" if s["realized_pnl_usd"] >= 0 else ""
        return [
            f"  {label}:",
            f"    bot trades:    {s['trade_count']} "
            f"({s['buy_count']} buys, {s['sell_count']} sells)",
            f"    buy volume:    ${s['buy_volume_usd']:.4f}",
            f"    sell volume:   ${s['sell_volume_usd']:.4f}",
            f"    realized PnL:  {sign}${s['realized_pnl_usd']:.4f} "
            f"({s['realized_pnl_pct']:+.2f}%)",
            f"    win rate:      {s['win_rate_pct']:.0f}%",
            f"    avg buy:       ${s['avg_buy_price']:.8f}",
            f"    avg sell:      ${s['avg_sell_price']:.8f}",
            f"    bot pos:       {s['tokens_held']:,.4f} tokens (from bot trades only)",
            f"    gas spent:     ${s['gas_spent_usd']:.4f} ({s['gas_spent_sol']:.6f} SOL)",
        ]

    def _fmt_wallet_block() -> list[str]:
        if snap is None:
            return ["  on-chain wallet:  (no snapshot yet)"]
        block = [
            "  On-chain wallet (real position):",
            f"    SOL:             {snap.sol_balance:.6f} (${snap.sol_value_usd:.4f})",
        ]
        if snap.token_mint:
            block.append(
                f"    target token:    {snap.token_balance:,.4f} "
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


def main() -> None:
    """Sync entry point."""
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        asyncio.run(async_main(config_path))
    except KeyboardInterrupt:
        print("\nShutdown.")


if __name__ == "__main__":
    main()
