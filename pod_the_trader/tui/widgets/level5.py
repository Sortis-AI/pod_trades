"""Level5 billing panel: USDC + credits balance and session inference cost."""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static


class Level5Widget(Static):
    """USDC + credits balance bars, session-cost split, model + dashboard.

    Tracks the *first* balance reading as the session start so subsequent
    updates can compute how much USDC and credit have been spent on
    inference this session.
    """

    DEFAULT_CSS = """
    Level5Widget {
        height: 1fr;
        min-width: 77;
        overflow-y: auto;
        overflow-x: hidden;
    }
    """

    usdc: reactive[float] = reactive(0.0, init=False)
    credit: reactive[float] = reactive(0.0, init=False)
    model: reactive[str] = reactive("", init=False)
    dashboard_url: reactive[str] = reactive("", init=False)
    # Provider-aware display: title swaps to the active provider's name
    # and the Credits row + session credits fragment disappear for
    # providers without a promotional-credits ledger (e.g. UsePod).
    provider_display: reactive[str] = reactive("Level5", init=False)
    show_credits: reactive[bool] = reactive(True, init=False)

    def __init__(self, **kwargs) -> None:
        super().__init__("[b #ffcc00]Level5 Billing[/]\n[dim]no balance[/]", markup=True, **kwargs)
        # Session start balances are captured the first time on_balance fires.
        self._session_start_usdc: float | None = None
        self._session_start_credit: float | None = None

    def on_mount(self) -> None:
        self.update(self._format())

    def on_resize(self) -> None:
        self.update(self._format())

    def watch_usdc(self) -> None:
        # Seed on the first real reading. The agent only publishes balance
        # events after a successful Level5 /balance fetch (see
        # print_startup_banner), so the first watch fire is authoritative.
        # We deliberately do NOT require self.usdc > 0 — a legitimate
        # zero USDC balance (user with only credits) must still seed.
        if self._session_start_usdc is None:
            self._session_start_usdc = self.usdc
        self.update(self._format())

    def watch_credit(self) -> None:
        # Same logic as watch_usdc. A user with no promotional credits
        # has credit == 0.0 always, and we still need to seed so the
        # USDC-side spend calculation isn't gated on credit being
        # non-None.
        if self._session_start_credit is None:
            self._session_start_credit = self.credit
        self.update(self._format())

    def watch_model(self) -> None:
        self.update(self._format())

    def watch_dashboard_url(self) -> None:
        self.update(self._format())

    def watch_provider_display(self) -> None:
        self.update(self._format())

    def watch_show_credits(self) -> None:
        self.update(self._format())

    def _bar_width(self) -> int:
        # Total content width minus border (2), padding (2), label+value (~22),
        # and trailing percent column (~6).
        avail = max(0, self.size.width - 32)
        return max(6, min(40, avail))

    def _format(self) -> str:
        title = f"[b #ffcc00]{self.provider_display} Billing[/]"
        total = self.usdc + self.credit
        if total <= 0 and self._session_start_usdc is None:
            return title + "\n[dim]no balance[/]"

        bar_w = self._bar_width()

        # Session inference cost: delta from the very first balance we saw.
        # Each side is computed independently — a user with no promotional
        # credits has session_start_credit pinned at 0 which should NOT
        # gate out the (real) USDC spend on the other line.
        spent_usdc = 0.0
        spent_credit = 0.0
        if self._session_start_usdc is not None:
            spent_usdc = max(0.0, self._session_start_usdc - self.usdc)
        if self._session_start_credit is not None:
            spent_credit = max(0.0, self._session_start_credit - self.credit)
        spent_total = spent_usdc + spent_credit

        # When the provider has no credit ledger (UsePod), the USDC bar
        # represents the entire balance — there's no credit slice
        # competing for it — so the percentage shows 100 % when any
        # USDC is present. When there are credits, both percentages
        # reflect their share of the combined balance.
        if self.show_credits:
            usdc_pct = self.usdc / total * 100 if total > 0 else 0.0
            credit_pct = self.credit / total * 100 if total > 0 else 0.0
        else:
            usdc_pct = 100.0 if self.usdc > 0 else 0.0
            credit_pct = 0.0

        lines: list[str] = [
            title,
            "",
            f"[b #00d4ff]USDC[/]     [b]${self.usdc:>10.4f}[/]  "
            f"{_bar(usdc_pct, bar_w)} [dim]{usdc_pct:4.0f}%[/]",
        ]
        if self.show_credits:
            lines.append(
                f"[b #00d4ff]Credits[/]  [b]${self.credit:>10.4f}[/]  "
                f"{_bar(credit_pct, bar_w)} [dim]{credit_pct:4.0f}%[/]"
            )

        # "Total" only differs from USDC when there's a real credit
        # ledger. Hiding it for credit-less providers reduces noise
        # without losing information.
        if self.show_credits:
            lines.extend(
                [
                    "",
                    f"[#ffcc00]Total:[/]    [b #00ff88]${total:,.4f}[/]",
                ]
            )

        session_line = (
            f"[#ffcc00]Session:[/]  [b #ff3366]${spent_total:.6f}[/]  "
            f"[dim]usdc[/] ${spent_usdc:.6f}"
        )
        if self.show_credits:
            session_line += f"  [dim]credits[/] ${spent_credit:.6f}"
        lines.extend(["", session_line])

        if self.model:
            lines.append("")
            lines.append(f"[dim]model:[/]     [b]{self.model}[/]")
        if self.dashboard_url:
            # Click the URL to open in the default browser. Textual fires
            # action_open_dashboard on this widget when the markup is clicked.
            lines.append(
                f"[dim]dashboard:[/] [@click=open_dashboard][#00d4ff]{self.dashboard_url}[/][/]"
            )
        else:
            lines.append("[dim]dashboard:[/] [dim](unavailable)[/]")
        return "\n".join(lines)

    def action_open_dashboard(self) -> None:
        """Open the active provider's dashboard URL in the default web browser."""
        if not self.dashboard_url:
            return
        import webbrowser

        try:
            webbrowser.open(self.dashboard_url)
            self.app.notify(
                f"Opening {self.provider_display} dashboard…",
                title="Dashboard",
                timeout=2,
            )
        except Exception as e:
            self.app.notify(f"Open failed: {e}", severity="error", timeout=3)


def _bar(pct: float, width: int) -> str:
    filled = int(round(pct / 100 * width))
    filled = max(0, min(width, filled))
    return "[#00d4ff]" + ("█" * filled) + "[/]" + "[dim]" + ("░" * (width - filled)) + "[/]"
