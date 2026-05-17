"""Log-tail panel that displays recent log lines via a RichLog widget."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from textual.widgets import RichLog


def _markup_escape(s: str) -> str:
    # See pod_the_trader/tui/widgets/cycle_status.py for rationale —
    # rich.markup.escape leaves bracket fragments like `[04-0` (a
    # `parse_decision` truncation) un-escaped, and the parser still
    # trips on them. Escaping every `[` is the safe baseline.
    return s.replace("[", r"\[")


class LogTailWidget(RichLog):
    """A RichLog pre-configured for the dashboard log panel.

    The title is rendered as part of the panel border via the styles.tcss
    border-title CSS feature (set in app.py on_mount).
    """

    DEFAULT_CSS = """
    LogTailWidget {
        height: 1fr;
        background: #0a0f1e;
        color: #556677;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(max_lines=500, wrap=False, markup=True, **kwargs)

    def append(self, level: str, message: str) -> None:
        color = {
            "CRITICAL": "#ff3366",
            "ERROR": "#ff3366",
            "WARNING": "#ffcc00",
            "INFO": "#00d4ff",
            "DEBUG": "#556677",
            "TRADE": "#00ff88",
        }.get(level.upper(), "#556677")
        # Escape the message: log lines routinely contain bracket
        # patterns ([04-06], list reprs, dict keys) and any unescaped
        # `[...]` here lights up the markup parser, which then raises
        # an error that the logging handler tries to write back into
        # this same widget — an infinite cascade that wedges the TUI.
        self.write(f"[{color}]{level:<5}[/] {_markup_escape(message)}")


class LogTailHandler(logging.Handler):
    """A logging.Handler that forwards records into a LogTailWidget.

    Textual widgets are only safe to touch from the main event loop, so we
    use ``app.call_from_thread`` to marshal back when records come from
    worker threads.
    """

    def __init__(self, widget: LogTailWidget, app) -> None:
        super().__init__()
        self._widget = widget
        self._app = app
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            level = record.levelname
            # If we're on the event loop already, schedule directly;
            # call_from_thread raises when invoked from the loop thread.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            with contextlib.suppress(Exception):
                if loop is not None:
                    loop.call_soon(self._widget.append, level, msg)
                else:
                    self._app.call_from_thread(self._widget.append, level, msg)
        except Exception:
            self.handleError(record)
