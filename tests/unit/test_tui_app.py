"""Test that the Textual App mounts and reacts to Publisher events.

Uses Textual's built-in `App.run_test()` which simulates a virtual terminal
so we don't need a real TTY.
"""

from pathlib import Path

import pytest

from pod_the_trader.data.ledger import TradeEntry, TradeLedger
from pod_the_trader.data.price_log import PriceLog, PriceTick, now_iso
from pod_the_trader.tui.app import PodDashboardApp


@pytest.fixture()
def ledger(tmp_path: Path) -> TradeLedger:
    return TradeLedger(storage_dir=str(tmp_path))


@pytest.fixture()
def price_log(tmp_path: Path) -> PriceLog:
    log = PriceLog(storage_dir=str(tmp_path))
    # Seed a few SOL price ticks so the sparkline has data.
    for price in (80.0, 81.0, 82.5, 81.9, 83.1):
        log.append(
            PriceTick(
                timestamp=now_iso(),
                mint="So11111111111111111111111111111111111111112",
                symbol="SOL",
                price_usd=price,
                source="test",
            )
        )
    return log


@pytest.fixture()
def populated_ledger(ledger: TradeLedger) -> TradeLedger:
    ledger.append(
        TradeEntry(
            timestamp=now_iso(),
            side="buy",
            input_mint="So11111111111111111111111111111111111111112",
            input_symbol="SOL",
            input_decimals=9,
            input_amount_raw=100_000_000,
            input_amount_ui=0.1,
            input_value_usd=8.0,
            output_mint="TARGET",
            output_decimals=6,
            expected_out_raw=50_000_000,
            expected_out_ui=50.0,
            actual_out_raw=49_500_000,
            actual_out_ui=49.5,
            output_price_usd=0.16,
            output_value_usd=7.92,
            signature="fakesig123",
        )
    )
    return ledger


class TestPodDashboardAppBoot:
    async def test_app_mounts_cleanly(
        self, ledger: TradeLedger, price_log: PriceLog
    ) -> None:
        app = PodDashboardApp(
            ledger=ledger,
            price_log=price_log,
            target_mint="TARGET",
        )
        async with app.run_test() as pilot:
            # Header should display after startup event.
            app.on_startup(
                wallet="11111111111111111111111111111111",
                target="TARGET",
                model="minimax-m2.7",
                cooldown=300,
                ledger_summary={"trade_count": 0},
            )
            await pilot.pause()
            # Still alive.
            assert app.is_running

    async def test_app_handles_cycle_events(
        self,
        populated_ledger: TradeLedger,
        price_log: PriceLog,
    ) -> None:
        app = PodDashboardApp(
            ledger=populated_ledger,
            price_log=price_log,
            target_mint="TARGET",
        )
        async with app.run_test() as pilot:
            app.on_cycle_start(1, "2026-04-13T10:00:00+00:00")
            app.on_cycle_complete(
                {
                    "cycle_num": 1,
                    "decision": "HOLD",
                    "reason": "Price stable",
                    "portfolio": {
                        "sol_ui": 0.5,
                        "sol_value_usd": 40.0,
                        "token_ui": 1000.0,
                        "token_price_usd": 0.16,
                        "token_value_usd": 160.0,
                        "total_usd": 200.0,
                    },
                    "cooldown_seconds": 300,
                }
            )
            await pilot.pause()
            assert app.is_running

    async def test_app_handles_trade_event(
        self,
        populated_ledger: TradeLedger,
        price_log: PriceLog,
    ) -> None:
        app = PodDashboardApp(
            ledger=populated_ledger,
            price_log=price_log,
            target_mint="TARGET",
        )
        async with app.run_test() as pilot:
            app.on_trade(
                {
                    "timestamp": now_iso(),
                    "side": "buy",
                    "input_value_usd": 8.0,
                    "actual_out_ui": 49.5,
                    "signature": "sig",
                },
                pnl={"type": "buy"},
            )
            await pilot.pause()
            assert app.is_running
