"""Entry point: wire everything together and run the startup flow."""

import asyncio
import contextlib
import logging
import signal
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from pod_the_trader.agent.core import TradingAgent
from pod_the_trader.agent.memory import ConversationMemory
from pod_the_trader.config import Config, ConfigError
from pod_the_trader.data.ledger import TradeLedger
from pod_the_trader.data.lot_ledger import LotLedger, migrate_from_trade_ledger
from pod_the_trader.data.price_log import PriceLog
from pod_the_trader.data.wallet_log import WalletLog, WalletSnapshot
from pod_the_trader.level5.auth import Level5Auth, Level5Credentials
from pod_the_trader.level5.client import Level5Client, Level5Error
from pod_the_trader.level5.poller import BalancePoller, FundingOrchestrator
from pod_the_trader.level5.provider import ProviderConfig, resolve_provider
from pod_the_trader.tools import create_registry
from pod_the_trader.trading.dex import SOL_MINT, USDC_MINT, JupiterDex
from pod_the_trader.trading.portfolio import Portfolio
from pod_the_trader.trading.transaction import TransactionBuilder
from pod_the_trader.wallet.manager import WalletManager
from pod_the_trader.wallet.setup import WalletSetup

logger = logging.getLogger("pod_the_trader")


def _resolve_rpc_urls(config: Config) -> list[str]:
    """Build the prioritized RPC endpoint list used for read failover.

    The primary ``solana.rpc_url`` is always first so transactions and
    reads use the same endpoint by default; additional entries from
    ``solana.rpc_urls`` follow, deduplicated in order.
    """
    primary = config.get("solana.rpc_url", "https://api.mainnet-beta.solana.com")
    extras = config.get("solana.rpc_urls", []) or []
    if not isinstance(extras, list):
        extras = [extras]
    ordered: list[str] = []
    for url in [primary, *extras]:
        if url and url not in ordered:
            ordered.append(url)
    return ordered


def _configure_logging(config: Config, *, console: bool = True) -> None:
    """Set up logging with split file (verbose) + console (minimal) handlers.

    File gets everything at DEBUG with full format — that's the forensic
    log for digging into what happened.

    Console (stderr) gets only WARNING+ with a minimal format — errors are
    visible, everything else stays out of the user's face. Noisy libraries
    (httpx, httpcore, openai) are pinned to WARNING so their INFO-level
    request logs don't spam the console.

    Pass ``console=False`` when launching the TUI: Textual owns the screen
    and any stray writes to stderr paint over the dashboard. The TUI wires
    its own LogTailHandler for in-app log display.

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

    if console:
        # Console handler (stderr) — minimal, WARNING+, simple format
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root_logger.addHandler(console_handler)

    # Silence noisy third-party libs at the console level (they still log
    # to the file at DEBUG via their own loggers → root → file_handler).
    for noisy in ("httpx", "httpcore", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Make stdout line-buffered so print() output is visible in real time
    # even when redirected to a file.
    with contextlib.suppress(AttributeError, OSError):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]


def _print_post_registration_instructions(
    *,
    provider_display: str,
    dashboard_url: str,
    contract: str,
    deposit_code: str,
    status: str,
    wallet_address: str,
) -> None:
    """Print the operator instructions immediately after a successful
    provider registration. Must be called in both CLI and TUI paths so
    the operator knows what to do next regardless of mode.

    pod-the-trader has no programmatic deposit path — the operator
    funds USDC through the provider's dashboard, and the bot
    separately needs SOL in the trading wallet for Jupiter gas. Both
    actions are called out explicitly.
    """
    bar = "─" * 72
    print()
    print(bar)
    print(f"  {provider_display} account registered.")
    print()
    print(f"    Dashboard:     {dashboard_url}")
    print(f"    Contract:      {contract}")
    print(f"    Deposit code:  {deposit_code}")
    print(f"    Status:        {status or 'pending_deposit'}")
    print()
    print("  Next steps:")
    print()
    print(f"    1. Open the dashboard and deposit USDC. {provider_display} routes")
    print("       the deposit to this account via the deposit code.")
    print()
    print("    2. Send SOL to your trading wallet (for Jupiter gas):")
    print()
    print(f"         {wallet_address}")
    print()
    print(f"  pod-the-trader will begin trading once both the {provider_display}")
    print("  account and the trading wallet have funds.")
    print(bar)
    print(flush=True)


async def async_main(
    config_path: str | None = None,
    base_domain_override: str | None = None,
    provider_override: str | None = None,
) -> None:
    """Main async entry point."""
    load_dotenv()

    # 1. Load and validate config
    try:
        config = Config(config_path)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    provider = _resolve_provider(config, provider_override)
    # base_domain is computed AFTER the wizard so the operator's
    # in-wizard provider choice (or saved credentials' provider) wins
    # over the CLI/config default. See _reconcile_provider_with_creds.

    # 2. Configure logging
    _configure_logging(config)
    logger.info("Pod The Trader starting up...")

    storage_dir = config.get("storage.base_dir", "~/.pod_the_trader")
    rpc_urls = _resolve_rpc_urls(config)
    rpc_url = rpc_urls[0]  # primary — used for writes and provider polling

    # 3. Provider auth (wizard may pick a different provider than the
    # resolved default; reconciler below switches to whatever the
    # returned credentials actually name).
    level5_auth = Level5Auth(storage_dir)
    creds = level5_auth.setup_interactive(provider)
    provider, base_domain = _reconcile_provider_with_creds(
        provider, creds, config, base_domain_override
    )
    logger.info("Using %s domain: %s", provider.display_name, base_domain)

    if creds is None or not creds.api_token:
        if provider.accountless:
            pass  # No token/registration — the wallet pays per request.
        elif creds and creds.is_new:
            pass  # Will register below
        else:
            env_var = f"{provider.key.upper()}_API_TOKEN"
            logger.critical(
                "%s credentials required — it is the active LLM provider. "
                "Set %s or run interactive setup.",
                provider.display_name,
                env_var,
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
        base_domain=base_domain,
        provider=provider,
    ) as level5_client:
        # 7. Register with the chosen provider if new
        if creds and creds.is_new:
            logger.info("Registering with %s...", provider.display_name)
            try:
                account = await level5_client.register()
            except Level5Error as e:
                logger.error("%s registration failed: %s", provider.display_name, e)
                print(
                    f"\n{provider.display_name} registration failed: {e}\n\n"
                    "This usually means the provider's API returned an "
                    "incomplete response. Try again in a moment, or "
                    f"contact {provider.display_name} support if it persists.",
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
            _print_post_registration_instructions(
                provider_display=provider.display_name,
                dashboard_url=creds.dashboard_url,
                contract=account.deposit_address,
                deposit_code=account.deposit_code,
                status=account.status,
                wallet_address=wallet_address,
            )

            poller = BalancePoller(
                rpc_url=rpc_url,
                wallet_address=wallet_address,
                interval=config.get("polling.funding_interval_seconds", 10),
                timeout=config.get("polling.funding_timeout_seconds", 3600),
            )
            orchestrator = FundingOrchestrator(poller, level5_client)

            min_level5_usdc = float(
                config.get(f"{provider.key}.min_balance_threshold_usdc", 0.1) or 0.0
            )
            min_wallet_sol = float(config.get("polling.min_wallet_sol", 0.05) or 0.05)

            try:
                print(
                    f"\n  Waiting for {provider.display_name} funding via dashboard "
                    f"(min ${min_level5_usdc:.2f} USDC)..."
                )
                await orchestrator.wait_for_level5_funding(min_level5_usdc)
                print(f"  {provider.display_name} account is funded.")

                print(
                    f"\n  Waiting for trading wallet to hold at least "
                    f"{min_wallet_sol:.4f} SOL for Jupiter gas..."
                )
                await orchestrator.wait_for_trading_wallet(min_wallet_sol)
                print("  Trading wallet ready.")
            except TimeoutError as e:
                logger.error("Funding timed out: %s", e)
                print(f"\nFunding timed out: {e}", file=sys.stderr)
                sys.exit(1)

        logger.info("Dashboard: %s", level5_client.get_dashboard_url())

        # 9. Jupiter DEX
        async with JupiterDex(
            quote_url=config.get("jupiter.quote_url"),
            swap_url=config.get("jupiter.swap_url"),
            price_url=config.get("jupiter.price_url"),
            search_url=config.get("jupiter.search_url", "https://lite-api.jup.ag/tokens/v2/search"),
            rpc_url=rpc_url,
        ) as jupiter_dex:
            # 10. Portfolio
            portfolio = Portfolio(
                rpc_url=rpc_urls,
                jupiter_dex=jupiter_dex,
                storage_dir=storage_dir,
            )

            # Accountless x402: the wallet's on-chain USDC is the
            # inference budget. Wire a reader so the client's balance
            # gate (and the TUI) reflect the live wallet rather than a
            # non-existent server-side ledger.
            if provider.accountless:
                level5_client.set_balance_reader(
                    lambda: portfolio.get_token_balance(wallet_address, USDC_MINT)
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
                keypair=keypair,
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

            # Windows asyncio (ProactorEventLoop) doesn't implement
            # add_signal_handler, and SIGTERM isn't a real deliverable
            # signal on Windows either. Fall back to signal.signal for
            # Ctrl+C there — it's the only thing the Windows console
            # can actually raise into a Python process.
            if sys.platform == "win32":
                signal.signal(signal.SIGINT, lambda *_: _signal_handler())
            else:
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


def _parse_cli_args(
    argv: list[str],
) -> tuple[str | None, str, str | None, str | None]:
    """Parse command-line args into
    ``(config_path, ui_mode, base_domain, provider)``.

    ui_mode ∈ {"auto", "tui", "cli"}. "auto" picks tui iff stdout is a TTY.

    ``base_domain`` is the proxy deployment host (e.g. ``level5.cloud``
    or ``usepod.ai``). ``provider`` is the LLM-proxy provider key
    (``"level5"`` or ``"usepod"``). For both, ``None`` means "fall
    back to the config value, then the default". Accepts both
    ``--flag foo`` and ``--flag=foo`` forms.
    """
    config_path: str | None = None
    ui_mode = "auto"
    base_domain: str | None = None
    provider: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--tui":
            ui_mode = "tui"
        elif arg == "--cli":
            ui_mode = "cli"
        elif arg == "--base-domain":
            if i + 1 >= len(argv):
                raise SystemExit("--base-domain requires a value (e.g. level5.cloud)")
            base_domain = argv[i + 1]
            i += 1
        elif arg.startswith("--base-domain="):
            base_domain = arg.split("=", 1)[1]
        elif arg == "--provider":
            if i + 1 >= len(argv):
                raise SystemExit("--provider requires a value (level5, usepod, or usepod-x402)")
            provider = argv[i + 1]
            i += 1
        elif arg.startswith("--provider="):
            provider = arg.split("=", 1)[1]
        elif not arg.startswith("--"):
            config_path = arg
        i += 1
    return config_path, ui_mode, base_domain, provider


def _resolve_provider(config: Config, cli_override: str | None) -> ProviderConfig:
    """CLI flag wins over config; config wins over default (Level5).

    Raises ``SystemExit`` with a clear message if the value names an
    unknown provider, so the operator hears about typos before the bot
    tries to talk to a non-existent API.
    """
    raw = cli_override if cli_override else config.get("provider")
    try:
        return resolve_provider(raw)
    except ValueError as e:
        raise SystemExit(str(e)) from e


def _resolve_base_domain(
    config: Config,
    cli_override: str | None,
    provider: ProviderConfig,
) -> str:
    """CLI flag wins over config; config wins over the provider default.

    Looks up ``<provider.key>.base_domain`` so each provider's section
    contributes its own host. The legacy ``level5.base_domain`` key is
    naturally still consulted when provider == Level5.
    """
    if cli_override:
        return cli_override.strip().strip("/")
    cfg_key = f"{provider.key}.base_domain"
    return str(config.get(cfg_key, provider.default_domain)).strip().strip("/")


def _reconcile_provider_with_creds(
    provider: ProviderConfig,
    creds: Level5Credentials | None,
    config: Config,
    base_domain_override: str | None,
) -> tuple[ProviderConfig, str]:
    """Reconcile the active provider with what setup_interactive returned.

    The outer ``provider`` is resolved from CLI flag / config / default
    BEFORE the wizard runs because we need it to seed the wizard's
    default chooser and to know which env var (``LEVEL5_API_TOKEN`` vs
    ``USEPOD_API_TOKEN``) to consult. Inside the wizard the operator
    can pick a different provider, and a saved credentials file may
    name a different provider than the CLI default. In either case,
    the returned credentials' ``provider`` field is authoritative for
    everything downstream — client construction, registration
    endpoint, dashboard URL, panel title — so we switch to it before
    constructing ``Level5Client``. Without this, picking UsePod in the
    wizard while the config default is Level5 registers a Level5
    account by mistake (the exact bug fixed in 0.3.1).

    Returns the active ``(provider, base_domain)`` tuple. Logs a
    switch line when the credentials override the resolved default.
    """
    if creds is None or creds.provider == provider.key:
        return provider, _resolve_base_domain(config, base_domain_override, provider)
    chosen = resolve_provider(creds.provider)
    logger.info(
        "Provider from credentials: %s (overrides resolved default %s)",
        chosen.display_name,
        provider.display_name,
    )
    return chosen, _resolve_base_domain(config, base_domain_override, chosen)


def _resolve_ui_mode(requested: str) -> str:
    if requested in ("tui", "cli"):
        return requested
    # auto: TUI only if stdout is a real terminal
    return "tui" if sys.stdout.isatty() else "cli"


def _run_update() -> int:
    """`pod-the-trader update` — git fetch + reset + uv sync.

    Previously lived inside the bash launcher generated by install.sh.
    Moved here so the Windows installer's .cmd launcher doesn't have to
    reimplement the same logic; both shims now do nothing more than
    ``exec uv run pod-the-trader "$@"``.

    Resolves the install dir as the parent of this package, assumes the
    current git branch has an upstream, and hard-resets onto it. Detached
    HEAD installs (unusual) are refused rather than silently rewritten
    to ``main``.
    """
    install_dir = Path(__file__).resolve().parent.parent
    if not (install_dir / ".git").is_dir():
        print(
            f"pod-the-trader: no git checkout at {install_dir} — re-run the installer.",
            file=sys.stderr,
        )
        return 1

    print(f"Updating pod-the-trader from {install_dir}...")

    def git(*args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(install_dir), *args],
            check=True,
            capture_output=capture,
            text=True,
        )

    try:
        branch = git("symbolic-ref", "--short", "HEAD", capture=True).stdout.strip()
    except subprocess.CalledProcessError:
        print(
            "pod-the-trader: HEAD is detached; cannot auto-update. "
            "Re-run the installer or `git checkout main` first.",
            file=sys.stderr,
        )
        return 1
    except FileNotFoundError:
        print("pod-the-trader: `git` not found on PATH.", file=sys.stderr)
        return 1

    try:
        before = git("rev-parse", "--short", "HEAD", capture=True).stdout.strip()
        git("fetch", "--quiet", "origin", branch)
        git("reset", "--hard", "--quiet", f"origin/{branch}")
        after = git("rev-parse", "--short", "HEAD", capture=True).stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"pod-the-trader: git update failed: {e}", file=sys.stderr)
        return 1

    if before == after:
        print(f"Already up to date at {after}.")
    else:
        print(f"Updated {before} → {after}.")

    print("Syncing dependencies...")
    try:
        subprocess.run(
            ["uv", "sync", "--quiet"],
            cwd=str(install_dir),
            check=True,
        )
    except FileNotFoundError:
        print(
            "pod-the-trader: `uv` not found on PATH — re-run the installer.",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as e:
        print(f"pod-the-trader: uv sync failed: {e}", file=sys.stderr)
        return 1

    print("Done. Run `pod-the-trader` to launch.")
    return 0


def main() -> None:
    """Sync entry point."""
    # `update` is a maintenance op — no disclaimer gate, no config load,
    # no network calls. Intercept before anything else so it stays fast
    # and works even when the config is broken.
    if len(sys.argv) >= 2 and sys.argv[1] == "update":
        sys.exit(_run_update())

    # Require the user to accept the disclaimer on every startup. This runs
    # BEFORE any heavy setup (no wallet load, no network calls, no Textual
    # app) so a decline exits cleanly and leaves no side effects behind.
    from pod_the_trader.disclaimer import require_acceptance

    require_acceptance()

    config_path, ui_mode, base_domain, provider = _parse_cli_args(sys.argv[1:])
    resolved = _resolve_ui_mode(ui_mode)
    try:
        if resolved == "tui":
            asyncio.run(async_main_tui(config_path, base_domain, provider))
        else:
            asyncio.run(async_main(config_path, base_domain, provider))
    except KeyboardInterrupt:
        print("\nShutdown.")


async def async_main_tui(
    config_path: str | None = None,
    base_domain_override: str | None = None,
    provider_override: str | None = None,
) -> None:
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

    provider = _resolve_provider(config, provider_override)
    # base_domain computed after wizard so the operator's in-wizard
    # choice wins over the resolved default. See
    # _reconcile_provider_with_creds for the rationale.

    _configure_logging(config, console=False)
    logger.info("Pod The Trader (TUI) starting up...")

    storage_dir = config.get("storage.base_dir", "~/.pod_the_trader")
    rpc_urls = _resolve_rpc_urls(config)
    rpc_url = rpc_urls[0]  # primary — used for writes and provider polling

    level5_auth = Level5Auth(storage_dir)
    creds = level5_auth.setup_interactive(provider)
    provider, base_domain = _reconcile_provider_with_creds(
        provider, creds, config, base_domain_override
    )
    logger.info("Using %s domain: %s", provider.display_name, base_domain)

    if (creds is None or not creds.api_token) and not (creds and creds.is_new):
        logger.critical("%s credentials required.", provider.display_name)
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
        base_domain=base_domain,
        provider=provider,
    ) as level5_client:
        if creds and creds.is_new:
            try:
                account = await level5_client.register()
            except Level5Error as e:
                logger.error("%s registration failed: %s", provider.display_name, e)
                print(
                    f"\n{provider.display_name} registration failed: {e}\n\n"
                    "This usually means the provider's API returned an "
                    "incomplete response. Try again in a moment, or "
                    f"contact {provider.display_name} support if it persists.",
                    file=sys.stderr,
                )
                sys.exit(1)
            creds.api_token = account.api_token
            creds.deposit_address = account.deposit_address
            creds.deposit_code = account.deposit_code
            creds.dashboard_url = account.dashboard_url or level5_client.get_dashboard_url()
            creds.is_new = False
            level5_auth.save(creds)

            _print_post_registration_instructions(
                provider_display=provider.display_name,
                dashboard_url=creds.dashboard_url,
                contract=account.deposit_address,
                deposit_code=account.deposit_code,
                status=account.status,
                wallet_address=wallet_address,
            )

            poller = BalancePoller(
                rpc_url=rpc_url,
                wallet_address=wallet_address,
                interval=config.get("polling.funding_interval_seconds", 10),
                timeout=config.get("polling.funding_timeout_seconds", 3600),
            )
            orchestrator = FundingOrchestrator(poller, level5_client)
            min_level5_usdc = float(
                config.get(f"{provider.key}.min_balance_threshold_usdc", 0.1) or 0.0
            )
            min_wallet_sol = float(config.get("polling.min_wallet_sol", 0.05) or 0.05)
            try:
                print(
                    f"\n  Waiting for {provider.display_name} funding via dashboard "
                    f"(min ${min_level5_usdc:.2f} USDC)..."
                )
                await orchestrator.wait_for_level5_funding(min_level5_usdc)
                print(f"  {provider.display_name} account is funded.")

                print(
                    f"\n  Waiting for trading wallet to hold at least "
                    f"{min_wallet_sol:.4f} SOL for Jupiter gas..."
                )
                await orchestrator.wait_for_trading_wallet(min_wallet_sol)
                print("  Trading wallet ready.")
            except TimeoutError as e:
                logger.error("Funding timed out: %s", e)
                print(f"\nFunding timed out: {e}", file=sys.stderr)
                sys.exit(1)

        async with JupiterDex(
            quote_url=config.get("jupiter.quote_url"),
            swap_url=config.get("jupiter.swap_url"),
            price_url=config.get("jupiter.price_url"),
            search_url=config.get("jupiter.search_url", "https://lite-api.jup.ag/tokens/v2/search"),
            rpc_url=rpc_url,
        ) as jupiter_dex:
            portfolio = Portfolio(
                rpc_url=rpc_urls,
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
