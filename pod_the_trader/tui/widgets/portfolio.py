"""Portfolio panel: SOL + target token balances with USD values."""

from __future__ import annotations

from typing import Any

from textual.reactive import reactive
from textual.widgets import Static


class PortfolioWidget(Static):
    """Shows on-chain SOL + target token balances with dollar values and
    a simple % split bar between the two.

    Implemented as a single Static with rich-text content to avoid
    container/leaf rendering issues in Textual 8.x.
    """

    DEFAULT_CSS = """
    PortfolioWidget {
        height: 1fr;
    }
    """

    snapshot: reactive[dict[str, Any] | None] = reactive(None, init=False)
    target_symbol: reactive[str] = reactive("TARGET", init=False)
    wallet_address: reactive[str] = reactive("", init=False)

    def __init__(self, **kwargs) -> None:
        super().__init__(
            "[b #ffcc00]Portfolio[/]\n[dim]loading…[/]",
            markup=True,
            **kwargs,
        )

    def on_resize(self) -> None:
        self.update(self._format(self.snapshot))

    def watch_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        self.update(self._format(snapshot))

    def watch_target_symbol(self) -> None:
        self.update(self._format(self.snapshot))

    def watch_wallet_address(self) -> None:
        self.update(self._format(self.snapshot))

    def _bar_width(self) -> int:
        avail = max(0, self.size.width - 14)
        return max(8, min(60, avail))

    def _format(self, snapshot: dict[str, Any] | None) -> str:
        title = "[b #ffcc00]Portfolio[/]\n"
        if snapshot is None:
            return title + "[dim]loading…[/]"

        sol_ui = snapshot.get("sol_ui", 0.0)
        sol_value = snapshot.get("sol_value_usd", 0.0)
        token_ui = snapshot.get("token_ui", 0.0)
        token_value = snapshot.get("token_value_usd", 0.0)
        total = snapshot.get("total_usd", 0.0) or (sol_value + token_value)

        sol_pct = (sol_value / total * 100) if total > 0 else 0
        tok_pct = (token_value / total * 100) if total > 0 else 0

        bar_w = self._bar_width()
        sol_bar = _bar(sol_pct, bar_w)
        tok_bar = _bar(tok_pct, bar_w)

        symbol = (self.target_symbol or "TARGET")[:8]

        lines = [
            title,
            f"[b #00d4ff]SOL[/]     {sol_ui:>12.6f}  [b]${sol_value:>10,.2f}[/]",
            f"  {sol_bar} [dim]{sol_pct:5.1f}%[/]",
            "",
            f"[b #00d4ff]{symbol:<6}[/]  {token_ui:>12,.4f}  [b]${token_value:>10,.2f}[/]",
            f"  {tok_bar} [dim]{tok_pct:5.1f}%[/]",
            "",
            f"[#ffcc00]Total:[/]  [b #00ff88]${total:,.2f}[/]",
        ]
        if self.wallet_address:
            lines.append("")
            # Click the wallet to copy to clipboard. Textual fires
            # action_copy_wallet on this widget when the markup is clicked.
            lines.append(
                f"[dim]wallet:[/] "
                f"[@click=copy_wallet][#00d4ff]{self.wallet_address}[/][/]"
            )
        return "\n".join(lines)

    def action_copy_wallet(self) -> None:
        """Copy the wallet address to the system clipboard."""
        if not self.wallet_address:
            return
        try:
            self.app.copy_to_clipboard(self.wallet_address)
            self.app.notify(
                f"Copied {self.wallet_address[:8]}…{self.wallet_address[-4:]}",
                title="Wallet address copied",
                timeout=2,
            )
        except Exception as e:
            self.app.notify(f"Copy failed: {e}", severity="error", timeout=3)


def _bar(pct: float, width: int) -> str:
    filled = int(round(pct / 100 * width))
    filled = max(0, min(width, filled))
    return "[#00d4ff]" + ("█" * filled) + "[/]" + "[dim]" + ("░" * (width - filled)) + "[/]"
