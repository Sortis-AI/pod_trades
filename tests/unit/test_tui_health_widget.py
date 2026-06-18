"""Tests for the Health widget's P&L gauge.

Regression for the contradictory-numbers bug: the panel showed a
turnover-weighted realized-% next to a lifetime realized-$, so e.g.
"+2.93% / +$539.60" on a $1,202.93 book. The % and $ must now describe
the same thing — total P&L (realized + unrealized) and that same total
as a fraction of the live portfolio value.
"""

from __future__ import annotations

from pod_the_trader.tui.widgets.health import HealthWidget


def _fmt(summary: dict) -> str:
    return HealthWidget()._format(summary)


class TestHealthGauge:
    def test_no_trades(self) -> None:
        assert "no trades" in _fmt({"trade_count": 0})

    def test_total_pnl_pct_against_portfolio(self) -> None:
        # The exact numbers from the reported screenshot: total P&L of
        # $539.5975 on a $1,202.93 book → +44.86%, NOT the old +2.93%.
        out = _fmt(
            {
                "trade_count": 495,
                "total_pnl_usd": 539.5975,
                "realized_pnl_usd": 539.5975,
                "realized_pnl_pct": 2.93,  # stale turnover-% must be ignored
                "win_rate_pct": 50.0,
                "portfolio_total_usd": 1202.93,
            }
        )
        assert "+44.86%" in out
        assert "+$539.59" in out  # dollar still the total P&L
        assert "2.93%" not in out

    def test_dollar_is_total_not_realized_only(self) -> None:
        # total = realized 100 + unrealized 50 = 150; the gauge shows the
        # total, not the realized-only 100.
        out = _fmt(
            {
                "trade_count": 3,
                "realized_pnl_usd": 100.0,
                "unrealized_pnl_usd": 50.0,
                "total_pnl_usd": 150.0,
                "win_rate_pct": 66.0,
                "portfolio_total_usd": 1500.0,
            }
        )
        assert "+$150.00" in out
        assert "+10.00%" in out  # 150 / 1500

    def test_negative_total_pnl_shows_down_arrow(self) -> None:
        out = _fmt(
            {
                "trade_count": 5,
                "total_pnl_usd": -80.0,
                "win_rate_pct": 20.0,
                "portfolio_total_usd": 800.0,
            }
        )
        assert "▼" in out
        assert "-10.00%" in out
        assert "$-80.00" in out  # sign is part of the number at 4dp

    def test_falls_back_to_realized_pct_without_portfolio_total(self) -> None:
        # Before the first portfolio snapshot arrives, portfolio_total is
        # 0 — the gauge must not divide by zero; it falls back to the
        # summary's own pct rather than showing a bogus 0%.
        out = _fmt(
            {
                "trade_count": 4,
                "total_pnl_usd": 25.0,
                "realized_pnl_pct": 7.5,
                "win_rate_pct": 50.0,
                "portfolio_total_usd": 0.0,
            }
        )
        assert "+7.50%" in out

    def test_falls_back_to_realized_usd_without_total(self) -> None:
        # Legacy TradeLedger summary has no total_pnl_usd — use realized.
        out = _fmt(
            {
                "trade_count": 4,
                "realized_pnl_usd": 12.0,
                "realized_pnl_pct": 3.0,
                "win_rate_pct": 50.0,
            }
        )
        assert "+$12.00" in out
