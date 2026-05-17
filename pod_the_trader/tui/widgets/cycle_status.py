"""Cycle status panel: current decision, reason, countdown to next cycle."""

from __future__ import annotations

import time

from textual.reactive import reactive
from textual.widgets import Static


def _markup_escape(s: str) -> str:
    # Backslash every `[`. rich.markup.escape and textual.markup.escape both
    # only escape brackets they recognize as tag-like (`[a-z#/@]...`), so a
    # truncated string like `... outside [04-0` (parse_decision caps the
    # reason at 150 chars and can cut a closing `]`) passes through them
    # un-escaped and still trips the Textual parser. Escaping every `[`
    # is safe — the parser only cares about openers; closers are matched
    # against the open stack.
    return s.replace("[", r"\[")


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
        # The decision and reason both originate from LLM output and
        # routinely contain literal `[...]` patterns (e.g. the strategy
        # references `[04-06] UTC window`). Pass them through the markup
        # parser unescaped and Textual raises MarkupError, which then
        # propagates up through trade_loop and crashes the cycle.
        icon = {
            "BUY": "[b #00ff88]📈 BUY[/]",
            "SELL": "[b #ff3366]📉 SELL[/]",
            "HOLD": "[b #ffcc00]⏸ HOLD[/]",
            "UNKNOWN": "[dim]❓ UNKNOWN[/]",
            "": "[dim]—[/]",
        }.get(self.decision, f"[dim]{_markup_escape(self.decision)}[/]")

        # countdown
        remaining = max(0, int(self.next_cycle_at - time.time()))
        mins, secs = divmod(remaining, 60)
        next_str = f"in {mins}:{secs:02d}" if remaining > 0 else "due"

        if self.reason:
            reason_str = _markup_escape(self.reason)
            if len(reason_str) > 180:
                reason_str = reason_str[:177] + "…"
        else:
            reason_str = "[dim]no reason yet[/]"

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
