"""Health / P&L gauge panel."""

from __future__ import annotations

from typing import Any

from textual.reactive import reactive
from textual.widgets import Static


class HealthWidget(Static):
    """Big P&L gauge: realized return % + win rate."""

    DEFAULT_CSS = """
    HealthWidget {
        height: 1fr;
        content-align: center middle;
    }
    """

    summary: reactive[dict[str, Any] | None] = reactive(None, init=False)

    def __init__(self, **kwargs) -> None:
        super().__init__(
            "[b #ffcc00]Health[/]\n\n[dim]no trades[/]",
            markup=True,
            **kwargs,
        )

    def watch_summary(self, summary: dict[str, Any] | None) -> None:
        self.update(self._format(summary))

    def _format(self, summary: dict[str, Any] | None) -> str:
        title = "[b #ffcc00]Health[/]\n"
        if summary is None or summary.get("trade_count", 0) == 0:
            return title + "\n[dim]no trades[/]"

        pnl = summary.get("realized_pnl_usd", 0.0)
        pnl_pct = summary.get("realized_pnl_pct", 0.0)
        win_rate = summary.get("win_rate_pct", 0.0)
        trades = summary.get("trade_count", 0)

        color = "#00ff88" if pnl >= 0 else "#ff3366"
        sign = "+" if pnl >= 0 else ""
        arrow = "▲" if pnl >= 0 else "▼"

        return "\n".join(
            [
                title,
                "",
                f"[b {color}]{arrow} {sign}{pnl_pct:.2f}%[/]",
                f"[{color}]{sign}${pnl:,.4f}[/]",
                "",
                f"[dim]win rate[/] [b]{win_rate:.0f}%[/]",
                f"[dim]{trades} trades[/]",
            ]
        )
