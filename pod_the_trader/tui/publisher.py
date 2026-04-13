"""Publisher protocol — how the TradingAgent emits state to a consumer (e.g. the TUI).

Decouples the agent from the TUI: the agent calls methods on a ``Publisher`` and
the ``PodDashboardApp`` implements them by updating reactive state. A
``NullPublisher`` is the default when running in CLI mode — its methods are
no-ops so the agent's existing ``print()``-based output path stays intact.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Publisher(Protocol):
    """Contract for anything that wants to observe agent state changes.

    All methods are fire-and-forget. Implementations MUST NOT block or raise;
    the agent shouldn't care whether publishing succeeded.
    """

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
    ) -> None: ...

    def on_cycle_start(self, cycle_num: int, timestamp: str) -> None: ...

    def on_cycle_complete(self, summary: dict[str, Any]) -> None: ...

    def on_trade(self, entry: dict[str, Any], pnl: dict[str, Any]) -> None: ...

    def on_price_tick(self, mint: str, price_usd: float, timestamp: str) -> None: ...

    def on_portfolio_snapshot(self, snapshot: dict[str, Any]) -> None: ...

    def on_level5_balance(self, usdc: float, credit: float) -> None: ...

    def on_log(self, level: str, message: str) -> None: ...

    def on_shutdown(self, session_summary: dict[str, Any]) -> None: ...


class NullPublisher:
    """Default no-op publisher used when no consumer is attached.

    The agent always has a publisher; when nobody is listening we give it this
    one so the code path is identical whether or not a TUI is running.
    """

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
        pass

    def on_cycle_start(self, cycle_num: int, timestamp: str) -> None:
        pass

    def on_cycle_complete(self, summary: dict[str, Any]) -> None:
        pass

    def on_trade(self, entry: dict[str, Any], pnl: dict[str, Any]) -> None:
        pass

    def on_price_tick(self, mint: str, price_usd: float, timestamp: str) -> None:
        pass

    def on_portfolio_snapshot(self, snapshot: dict[str, Any]) -> None:
        pass

    def on_level5_balance(self, usdc: float, credit: float) -> None:
        pass

    def on_log(self, level: str, message: str) -> None:
        pass

    def on_shutdown(self, session_summary: dict[str, Any]) -> None:
        pass
