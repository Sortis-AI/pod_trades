"""Cycle status panel: current decision, reason, countdown to next cycle."""

from __future__ import annotations

import time

from textual.reactive import reactive
from textual.widgets import Static


class CycleStatusWidget(Static):
    """Shows the current cycle number, status (idle/analyzing/sleeping),
    the most recent decision and reason, and a live countdown to the
    next cycle."""

    DEFAULT_CSS = """
    CycleStatusWidget {
        height: 1fr;
    }
    """

    cycle_num: reactive[int] = reactive(0, init=False)
    status: reactive[str] = reactive("waiting", init=False)
    decision: reactive[str] = reactive("", init=False)
    reason: reactive[str] = reactive("", init=False)
    next_cycle_at: reactive[float] = reactive(0.0, init=False)

    def __init__(self, **kwargs) -> None:
        super().__init__(
            "[b #ffcc00]Cycle[/]\n[dim]waiting for first cycle…[/]",
            markup=True,
            **kwargs,
        )

    def on_mount(self) -> None:
        self.update(self._format())
        # 1 Hz countdown refresh
        self.set_interval(1.0, self._refresh)

    def _refresh(self) -> None:
        self.update(self._format())

    def watch_cycle_num(self) -> None:
        self._refresh()

    def watch_status(self) -> None:
        self._refresh()

    def watch_decision(self) -> None:
        self._refresh()

    def watch_reason(self) -> None:
        self._refresh()

    def watch_next_cycle_at(self) -> None:
        self._refresh()

    def _format(self) -> str:
        icon = {
            "BUY": "[b #00ff88]📈 BUY[/]",
            "SELL": "[b #ff3366]📉 SELL[/]",
            "HOLD": "[b #ffcc00]⏸ HOLD[/]",
            "UNKNOWN": "[dim]❓ UNKNOWN[/]",
            "": "[dim]—[/]",
        }.get(self.decision, f"[dim]{self.decision}[/]")

        # countdown
        remaining = max(0, int(self.next_cycle_at - time.time()))
        mins, secs = divmod(remaining, 60)
        next_str = f"in {mins}:{secs:02d}" if remaining > 0 else "due"

        reason_str = self.reason or "[dim]no reason yet[/]"
        if len(reason_str) > 180:
            reason_str = reason_str[:177] + "…"

        title_num = self.cycle_num if self.cycle_num else "—"
        return "\n".join(
            [
                f"[b #ffcc00]Cycle {title_num}[/]",
                f"[#ffcc00]Status:[/]   [b]{self.status}[/]",
                f"[#ffcc00]Decision:[/] {icon}",
                "[#ffcc00]Reason:[/]",
                f"  {reason_str}",
                "",
                f"[#ffcc00]Next:[/]     [b]{next_str}[/]",
            ]
        )
