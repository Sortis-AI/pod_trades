"""Price action panel — multiple labeled sparklines stacked vertically."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Static

if TYPE_CHECKING:
    from pod_the_trader.data.price_log import PriceLog

SPARK_CHARS = " ▁▂▃▄▅▆▇█"


class PriceActionWidget(Static):
    """Stacks one or more (label, mint) sparklines in a single panel.

    Each row renders as: ``LABEL  $price  ▲ +x.xx%`` followed by a sparkline
    on the next line. The panel title is shown on the first line.
    """

    DEFAULT_CSS = """
    PriceActionWidget {
        height: 1fr;
    }
    """

    def __init__(
        self,
        title: str,
        series: list[tuple[str, str]],
        price_log: PriceLog | None = None,
        **kwargs,
    ) -> None:
        self._title = title
        # ``series`` is a list of (label, mint) — label is mutable so we can
        # update e.g. "TARGET" → "SQUIRE" after metadata loads.
        self._series: list[list[str]] = [[label, mint] for label, mint in series]
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
        for row in self._series:
            if row[1] == mint:
                row[0] = label
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
        for label, mint in self._series:
            ticks = self._price_log.read_for_mint(mint)[-240:]
            values = [t.price_usd for t in ticks if t.price_usd > 0]
            label_str = f"[b #00d4ff]{label:<7}[/]"
            if not values:
                lines.append(f"{label_str} [dim]no data[/]")
                continue

            latest = values[-1]
            if len(values) >= 2:
                delta = (latest - values[0]) / values[0] * 100
                color = "#00ff88" if delta >= 0 else "#ff3366"
                arrow = "▲" if delta >= 0 else "▼"
                price_line = (
                    f"{label_str} [b]${_fmt_price(latest)}[/]  "
                    f"[{color}]{arrow} {delta:+.2f}%[/]"
                )
            else:
                price_line = f"{label_str} [b]${_fmt_price(latest)}[/]"

            sparkline = _sparkline(values, width=spark_w)
            lines.append(price_line)
            lines.append(f"[#00d4ff]{sparkline}[/]")

        return "\n".join(lines)


def _fmt_price(price: float) -> str:
    if price >= 1:
        return f"{price:,.2f}"
    if price >= 0.01:
        return f"{price:.4f}"
    return f"{price:.8f}"


def _sparkline(values: list[float], width: int = 60) -> str:
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]

    lo = min(values)
    hi = max(values)
    span = hi - lo if hi > lo else 1.0
    chars = []
    for v in values:
        normalized = (v - lo) / span
        idx = int(round(normalized * (len(SPARK_CHARS) - 1)))
        idx = max(0, min(len(SPARK_CHARS) - 1, idx))
        chars.append(SPARK_CHARS[idx])
    return "".join(chars)
