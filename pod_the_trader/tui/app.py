"""Pod The Trader — Textual dashboard application."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Footer, Static

from pod_the_trader.data.ledger import TradeLedger
from pod_the_trader.data.price_log import PriceLog
from pod_the_trader.trading.dex import SOL_MINT

from .widgets.cycle_status import CycleStatusWidget
from .widgets.health import HealthWidget
from .widgets.ledger import LedgerWidget
from .widgets.level5 import Level5Widget
from .widgets.log_tail import LogTailHandler, LogTailWidget
from .widgets.portfolio import PortfolioWidget
from .widgets.prices import PriceActionWidget

if TYPE_CHECKING:
    from pod_the_trader.data.lot_ledger import LotLedger

    from .publisher import Publisher

logger = logging.getLogger(__name__)


class PodDashboardApp(App):
    """A btop-style live dashboard for Pod The Trader.

    The app implements the :class:`Publisher` protocol: the trading agent
    calls ``on_*`` methods on this instance, and each handler updates the
    reactive state of a specific widget.  All state is stored on widgets
    (not the App) so widget refresh logic lives next to the data it owns.
    """

    CSS_PATH = "styles.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(
        self,
        *,
        ledger: TradeLedger,
        price_log: PriceLog,
        target_mint: str,
        lot_ledger: LotLedger | None = None,
        run_agent: callable | None = None,
    ) -> None:
        super().__init__()
        self._ledger = ledger
        self._lot_ledger = lot_ledger
        self._price_log = price_log
        self._target_mint = target_mint
        self._run_agent = run_agent
        self._shutdown_event = asyncio.Event()
        self._cycle_started_at: float | None = None
        self._header_text = "🤖 pod-the-trader · starting up…"
        self._log_handler: LogTailHandler | None = None
        # Cache the latest token price seen via on_portfolio_snapshot so
        # health/cycle handlers can reprice the lot ledger without an RPC.
        self._latest_token_price: float = 0.0

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Container(id="root"):
            yield Static(self._header_text, id="header-bar")
            with Container(id="row-top", classes="row"):
                yield PortfolioWidget(classes="panel", id="portfolio")
                yield HealthWidget(classes="panel", id="health")
                yield LedgerWidget(self._ledger, classes="panel", id="ledger")
            with Container(id="row-mid", classes="row"):
                yield PriceActionWidget(
                    "Price Action",
                    [("SOL", SOL_MINT), ("TARGET", self._target_mint)],
                    self._price_log,
                    classes="panel",
                    id="price-action",
                )
                yield Level5Widget(classes="panel", id="level5")
            with Container(id="row-bot", classes="row"):
                yield CycleStatusWidget(classes="panel", id="cycle")
                yield LogTailWidget(classes="panel", id="log")
        yield Footer()

    async def on_mount(self) -> None:
        # Attach a logging handler that forwards records into the log panel.
        log_widget = self.query_one("#log", LogTailWidget)
        self._log_handler = LogTailHandler(log_widget, self)
        self._log_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(self._log_handler)

        # Seed an initial portfolio snapshot from whatever's on disk.
        self.query_one("#portfolio", PortfolioWidget).snapshot = None
        self._refresh_health()

        # Kick off the trading agent as a background worker.
        if self._run_agent is not None:
            self.run_worker(
                self._run_agent(self._shutdown_event),
                name="trade_loop",
                exclusive=True,
            )

    async def action_quit(self) -> None:  # type: ignore[override]
        self._shutdown_event.set()
        await asyncio.sleep(0.1)
        self.exit()

    async def on_unmount(self) -> None:
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)

    # ------------------------------------------------------------- Publisher

    def _set_header(self, text: str) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#header-bar", Static).update(text)

    def _refresh_health(self, summary: dict[str, Any] | None = None) -> None:
        """Push the latest P&L summary into the Health widget.

        Prefers an explicitly provided ``summary`` (e.g. from an
        ``on_cycle_complete`` event), then falls back to a fresh lot-ledger
        replay at the latest cached token price, then to the legacy
        TradeLedger summary as a last resort.
        """
        try:
            health = self.query_one("#health", HealthWidget)
        except Exception:
            return
        if summary is not None:
            health.summary = summary
            return
        if self._lot_ledger is not None and self._target_mint:
            health.summary = self._lot_ledger.summary(self._target_mint, self._latest_token_price)
            return
        health.summary = self._ledger.summary()

    def on_startup(
        self,
        *,
        wallet: str,
        target: str,
        target_symbol: str = "",
        target_name: str = "",
        model: str,
        cooldown: int,
        dashboard_url: str = "",
        ledger_summary: dict[str, Any] | None = None,
    ) -> None:
        short_wallet = wallet[:6] + "…" + wallet[-4:] if len(wallet) > 12 else wallet
        symbol = target_symbol or "TARGET"
        self._header_text = (
            f"🤖 pod-the-trader · {short_wallet} · {symbol} · {model} · every {cooldown}s"
        )
        self._set_header(self._header_text)
        self._refresh_health(ledger_summary)

        # Propagate the symbol to the panels that label the target token.
        with contextlib.suppress(Exception):
            portfolio = self.query_one("#portfolio", PortfolioWidget)
            portfolio.target_symbol = symbol
            portfolio.wallet_address = wallet
        with contextlib.suppress(Exception):
            self.query_one("#price-action", PriceActionWidget).set_label(self._target_mint, symbol)
        with contextlib.suppress(Exception):
            level5 = self.query_one("#level5", Level5Widget)
            level5.model = model
            if dashboard_url:
                level5.dashboard_url = dashboard_url

    def on_cycle_start(self, cycle_num: int, timestamp: str) -> None:
        cycle_widget = self.query_one("#cycle", CycleStatusWidget)
        cycle_widget.cycle_num = cycle_num
        cycle_widget.status = "analyzing"
        self._cycle_started_at = time.time()

    def on_cycle_complete(self, summary: dict[str, Any]) -> None:
        cycle_widget = self.query_one("#cycle", CycleStatusWidget)
        cycle_widget.status = "sleeping"
        cycle_widget.decision = summary.get("decision", "")
        cycle_widget.reason = summary.get("reason", "")
        cycle_widget.next_cycle_at = time.time() + summary.get("cooldown_seconds", 300)

        if snap := summary.get("portfolio"):
            self.query_one("#portfolio", PortfolioWidget).snapshot = snap
            with contextlib.suppress(Exception):
                self._latest_token_price = float(snap.get("token_price_usd", 0.0) or 0.0)

        # Refresh health from the lot ledger (or whatever the agent passed).
        self._refresh_health(summary.get("ledger_summary"))

        # Refresh sparklines from the price log.
        with contextlib.suppress(Exception):
            self.query_one("#price-action", PriceActionWidget).refresh_data()

        # Update header with latest decision.
        decision = summary.get("decision", "")
        icon = {"BUY": "📈", "SELL": "📉", "HOLD": "⏸"}.get(decision, "❓")
        self._set_header(f"{self._header_text}   ·   {icon} {decision}")

    def on_trade(self, entry: dict[str, Any], pnl: dict[str, Any]) -> None:
        self.query_one("#ledger", LedgerWidget).refresh_rows()
        self._refresh_health()
        log_widget = self.query_one("#log", LogTailWidget)
        log_widget.append(
            "TRADE",
            f"[b]{entry.get('side', '').upper()}[/]  "
            f"{entry.get('actual_out_ui', 0):,.2f} tokens  "
            f"${entry.get('input_value_usd', 0):.2f}",
        )

    def on_price_tick(self, mint: str, price_usd: float, timestamp: str) -> None:
        if mint in (SOL_MINT, self._target_mint):
            with contextlib.suppress(Exception):
                self.query_one("#price-action", PriceActionWidget).refresh_data()

    def on_portfolio_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.query_one("#portfolio", PortfolioWidget).snapshot = snapshot
        with contextlib.suppress(Exception):
            self._latest_token_price = float(snapshot.get("token_price_usd", 0.0) or 0.0)

    def on_level5_balance(self, usdc: float, credit: float) -> None:
        widget = self.query_one("#level5", Level5Widget)
        widget.usdc = usdc
        widget.credit = credit

    def on_log(self, level: str, message: str) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#log", LogTailWidget).append(level, message)

    def on_shutdown(self, session_summary: dict[str, Any]) -> None:
        self._set_header("🤖 pod-the-trader · shutting down…")


def _check_publisher_protocol() -> None:
    """Structural sanity check: App implements the Publisher protocol."""
    _: Publisher = PodDashboardApp(
        ledger=TradeLedger("/tmp"),
        price_log=PriceLog("/tmp"),
        target_mint="",
    )
    # Delete so pytest doesn't try to instantiate the real app.
    del _
