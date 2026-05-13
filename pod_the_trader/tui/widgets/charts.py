"""Market charts panel — price + derived-indicator sparklines.

Stacks one or more labeled sparklines in a single panel. The original
implementation only rendered price-per-mint; the panel now also
renders RSI, IPP, and rolling volatility series so the operator can
see the same inputs the trading model uses to decide.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Static

if TYPE_CHECKING:
    from collections.abc import Callable

    from pod_the_trader.data.price_log import PriceLog

SPARK_CHARS = " ▁▂▃▄▅▆▇█"

# IPP band thresholds — must stay in sync with Section A of the strategy
# prompt in pod_the_trader/agent/core.py. There's no shared constants
# module yet; if the prompt's thresholds move, update both.
IPP_BUY_MAX = 180
IPP_NEUTRAL_MAX = 365
IPP_HOLD_MAX = 500

# Volatility override threshold — Section B of the strategy prompt.
# Lowered from 0.05 to 0.02 in v0.2.2.
VOL_OVERRIDE_THRESHOLD = 0.02


class MarketChartsWidget(Static):
    """Stacks price + derived-metric sparklines in a single panel.

    ``price_series`` is a list of ``(label, mint)`` tuples — one row
    per mint, value extracted from the price log.

    ``derived_series`` is a list of ``(label, kind, mint)`` tuples
    where ``kind`` is one of ``"rsi"``, ``"ipp"``, or ``"vol"``. Each
    kind has a built-in value provider that reads from the price log.

    Each row renders as: ``LABEL  value  ▲ delta  [annotation]``
    followed by a sparkline on the next line. The panel title is
    rendered on the first line.
    """

    DEFAULT_CSS = """
    MarketChartsWidget {
        height: 1fr;
    }
    """

    def __init__(
        self,
        title: str,
        price_series: list[tuple[str, str]],
        derived_series: list[tuple[str, str, str]] | None = None,
        price_log: PriceLog | None = None,
        **kwargs,
    ) -> None:
        self._title = title
        # Mutable so labels can update (e.g. TARGET → SQUIRE after metadata).
        self._price_series: list[list[str]] = [[label, mint] for label, mint in price_series]
        self._derived_series: list[list[str]] = [
            [label, kind, mint] for label, kind, mint in (derived_series or [])
        ]
        self._price_log = price_log
        super().__init__(
            f"[b #ffcc00]{title}[/]\n[dim]no data[/]",
            markup=True,
            **kwargs,
        )

    def on_mount(self) -> None:
        self.refresh_data()

    def on_resize(self) -> None:
        self.refresh_data()

    def set_label(self, mint: str, label: str) -> None:
        """Update the display label for every row tied to ``mint``."""
        for row in self._price_series:
            if row[1] == mint:
                row[0] = label
        for row in self._derived_series:
            if row[2] == mint:
                # Keep the metric tag in the label so RSI/IPP/VOL stay
                # distinguishable when the underlying mint label changes.
                pass
        self.refresh_data()

    def refresh_data(self) -> None:
        self.update(self._format())

    def _spark_width(self) -> int:
        return max(20, self.size.width - 4)

    def _format(self) -> str:
        lines = [f"[b #ffcc00]{self._title}[/]"]
        if self._price_log is None:
            lines.append("[dim]no data[/]")
            return "\n".join(lines)

        spark_w = self._spark_width()

        # Price rows (top).
        for label, mint in self._price_series:
            ticks = self._price_log.read_for_mint(mint)[-240:]
            values = [t.price_usd for t in ticks if t.price_usd > 0]
            self._render_row(
                lines,
                label=label,
                values=values,
                width=spark_w,
                formatter=lambda v: f"${_fmt_price(v)}",
            )

        # Derived rows (bottom).
        for label, kind, mint in self._derived_series:
            values, formatter, annotator = self._derived_values(kind, mint)
            self._render_row(
                lines,
                label=label,
                values=values[-240:],
                width=spark_w,
                formatter=formatter,
                annotator=annotator,
            )

        return "\n".join(lines)

    def _derived_values(
        self,
        kind: str,
        mint: str,
    ) -> tuple[list[float], Callable[[float], str], Callable[[float], str] | None]:
        """Return (values, value-formatter, annotator) for a derived kind."""
        assert self._price_log is not None  # _format guards this

        if kind == "rsi":
            return (
                self._price_log.rsi_series(mint, period=12),
                lambda v: f"{v:.1f}",
                _rsi_annotation,
            )
        if kind == "ipp":
            ticks = self._price_log.read_for_mint(mint)
            values = [500_000 * t.price_usd for t in ticks if t.price_usd > 0]
            return (
                values,
                lambda v: f"{v:,.0f}",
                _ipp_annotation,
            )
        if kind == "vol":
            return (
                self._price_log.rolling_volatility_series(mint, window=12),
                lambda v: f"{v:.4f}",
                _vol_annotation,
            )
        return ([], str, None)

    def _render_row(
        self,
        lines: list[str],
        *,
        label: str,
        values: list[float],
        width: int,
        formatter: Callable[[float], str],
        annotator: Callable[[float], str] | None = None,
    ) -> None:
        # Column width matches the longest label ("BREAKEVEN" = 9).
        label_str = f"[b #00d4ff]{label:<9}[/]"
        if not values:
            lines.append(f"{label_str} [dim]collecting…[/]")
            return

        latest = values[-1]
        if len(values) >= 2 and values[0] != 0:
            delta_pct = (latest - values[0]) / abs(values[0]) * 100
            color = "#00ff88" if delta_pct >= 0 else "#ff3366"
            arrow = "▲" if delta_pct >= 0 else "▼"
            delta_part = f"[{color}]{arrow} {delta_pct:+.2f}%[/]"
        else:
            delta_part = "[dim](collecting…)[/]"

        annot = annotator(latest) if annotator else ""
        value_line = f"{label_str} [b]{formatter(latest)}[/]  {delta_part}"
        if annot:
            value_line = f"{value_line}  {annot}"

        sparkline = _sparkline(values, width=width)
        lines.append(value_line)
        lines.append(f"[#00d4ff]{sparkline}[/]")


def _rsi_annotation(rsi: float) -> str:
    if rsi < 30:
        return "[#00ff88][OVERSOLD][/]"
    if rsi > 70:
        return "[#ff3366][OVERBOUGHT][/]"
    return ""


def _ipp_annotation(ipp: float) -> str:
    if ipp < IPP_BUY_MAX:
        return "[#00ff88][BUY][/]"
    if ipp <= IPP_NEUTRAL_MAX:
        return "[#ffcc00][NEUTRAL][/]"
    if ipp < IPP_HOLD_MAX:
        return "[dim][HOLD-ONLY][/]"
    return "[#ff3366][TRIM][/]"


def _vol_annotation(vol: float) -> str:
    if vol > VOL_OVERRIDE_THRESHOLD:
        return "[#00ff88][override open][/]"
    return "[dim][override closed][/]"


def _fmt_price(price: float) -> str:
    if price >= 1:
        return f"{price:,.2f}"
    if price >= 0.01:
        return f"{price:.4f}"
    return f"{price:.8f}"


def _sparkline(values: list[float], width: int = 60) -> str:
    """Render a sparkline string of exactly ``width`` characters.

    Always fills the full width regardless of sample count so a fresh
    run with one or two samples still shows a visible chart instead of
    a blank line. Dense data is downsampled by nearest-neighbor; sparse
    data is stretched to width. When all values are equal (or there's
    only one value), renders a flat mid-level line so the user sees a
    line rather than an invisible row of spaces.
    """
    if not values or width <= 0:
        return ""

    if len(values) != width:
        if len(values) == 1:
            stretched = [values[0]] * width
        else:
            step = (len(values) - 1) / (width - 1) if width > 1 else 0
            stretched = [values[min(int(round(i * step)), len(values) - 1)] for i in range(width)]
        values = stretched

    lo = min(values)
    hi = max(values)
    if hi <= lo:
        mid = SPARK_CHARS[len(SPARK_CHARS) // 2]
        return mid * width

    span = hi - lo
    chars = []
    for v in values:
        normalized = (v - lo) / span
        idx = int(round(normalized * (len(SPARK_CHARS) - 1)))
        idx = max(0, min(len(SPARK_CHARS) - 1, idx))
        chars.append(SPARK_CHARS[idx])
    return "".join(chars)
