"""Tests for pod_the_trader.data.ledger."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pod_the_trader.data.ledger import (
    LEDGER_COLUMNS,
    TradeEntry,
    TradeLedger,
    now_iso,
)


@pytest.fixture()
def ledger(tmp_path: Path) -> TradeLedger:
    return TradeLedger(storage_dir=str(tmp_path))


def _make_entry(
    side: str,
    in_value: float,
    out_value: float,
    out_amount: float = 100.0,
    in_amount: float = 1.0,
    timestamp: str | None = None,
    gas_usd: float = 0.001,
) -> TradeEntry:
    return TradeEntry(
        timestamp=timestamp or now_iso(),
        side=side,
        input_mint="So11111111111111111111111111111111111111112" if side == "buy" else "TARGET",
        input_amount_ui=in_amount,
        input_value_usd=in_value,
        output_mint="TARGET" if side == "buy" else "So11111111111111111111111111111111111111112",
        actual_out_ui=out_amount,
        actual_out_raw=int(out_amount * 1e6),
        output_value_usd=out_value,
        output_price_usd=out_value / out_amount if out_amount else 0,
        input_price_usd=in_value / in_amount if in_amount else 0,
        gas_usd=gas_usd,
        gas_sol=gas_usd / 100,
    )


class TestAppendAndRead:
    def test_creates_file_with_header(self, ledger: TradeLedger) -> None:
        ledger.append(_make_entry("buy", 10.0, 0.0))
        assert ledger.path.exists()
        content = ledger.path.read_text().splitlines()
        assert content[0] == ",".join(LEDGER_COLUMNS)
        assert len(content) == 2  # header + 1 row

    def test_append_multiple(self, ledger: TradeLedger) -> None:
        for i in range(5):
            ledger.append(_make_entry("buy", 10.0 + i, 0.0))
        trades = ledger.read_all()
        assert len(trades) == 5
        assert [t.input_value_usd for t in trades] == [10, 11, 12, 13, 14]

    def test_round_trip_field_types(self, ledger: TradeLedger) -> None:
        ledger.append(_make_entry("buy", 10.5, 0.0, out_amount=50.123))
        loaded = ledger.read_all()[0]
        assert isinstance(loaded.input_value_usd, float)
        assert loaded.input_value_usd == 10.5
        assert isinstance(loaded.actual_out_raw, int)
        assert isinstance(loaded.actual_out_ui, float)
        assert loaded.actual_out_ui == 50.123


class TestSummary:
    def test_empty_ledger(self, ledger: TradeLedger) -> None:
        summary = ledger.summary()
        assert summary["trade_count"] == 0
        assert summary["realized_pnl_usd"] == 0.0
        assert summary["win_rate_pct"] == 0.0

    def test_winning_pair(self, ledger: TradeLedger) -> None:
        # buy 1 SOL ($10), get 100 tokens
        ledger.append(_make_entry("buy", 10.0, 0.0, out_amount=100.0))
        # sell 100 tokens for $15
        ledger.append(_make_entry("sell", 0.0, 15.0, in_amount=100.0))
        summary = ledger.summary()
        assert summary["trade_count"] == 2
        assert summary["buy_count"] == 1
        assert summary["sell_count"] == 1
        # realized_pnl = sell ($15) - buy ($10) - gas (~$0.002)
        assert summary["realized_pnl_usd"] == pytest.approx(4.998, abs=1e-3)
        assert summary["win_rate_pct"] == 100.0
        assert summary["tokens_held"] == 0.0

    def test_losing_pair(self, ledger: TradeLedger) -> None:
        ledger.append(_make_entry("buy", 10.0, 0.0, out_amount=100.0))
        ledger.append(_make_entry("sell", 0.0, 7.0, in_amount=100.0))
        summary = ledger.summary()
        assert summary["realized_pnl_usd"] < 0
        assert summary["win_rate_pct"] == 0.0

    def test_open_position(self, ledger: TradeLedger) -> None:
        ledger.append(_make_entry("buy", 10.0, 0.0, out_amount=100.0))
        summary = ledger.summary()
        assert summary["trade_count"] == 1
        assert summary["tokens_held"] == 100.0
        # No sells yet, no realized PnL from pairs (only gas spent)
        assert summary["realized_pnl_usd"] < 0  # just gas
        assert summary["realized_pnl_usd"] > -0.01

    def test_filter_by_session_start(self, ledger: TradeLedger) -> None:
        old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        ledger.append(_make_entry("buy", 10.0, 0.0, timestamp=old))
        ledger.append(_make_entry("buy", 20.0, 0.0))  # now

        all_summary = ledger.summary()
        assert all_summary["trade_count"] == 2

        session_summary = ledger.summary(since=datetime.now(UTC) - timedelta(hours=1))
        assert session_summary["trade_count"] == 1


class TestPerTradePnl:
    def test_buy_entry_price(self, ledger: TradeLedger) -> None:
        entry = _make_entry("buy", 10.0, 0.0, out_amount=100.0)
        ledger.append(entry)
        pnl = ledger.per_trade_pnl(entry)
        assert pnl["type"] == "buy"
        assert pnl["entry_price"] == pytest.approx(0.1)
        assert pnl["tokens_acquired"] == 100.0
        assert pnl["cost_usd"] == 10.0
        assert pnl["position_total_tokens"] == 100.0

    def test_buy_updates_avg_entry(self, ledger: TradeLedger) -> None:
        # First buy: 100 tokens @ $0.10 each
        ledger.append(_make_entry("buy", 10.0, 0.0, out_amount=100.0))
        # Second buy: 50 tokens @ $0.20 each
        e2 = _make_entry("buy", 10.0, 0.0, out_amount=50.0)
        ledger.append(e2)
        pnl = ledger.per_trade_pnl(e2)
        # Avg entry: (10 + 10) / (100 + 50) = 0.1333
        assert pnl["position_avg_entry"] == pytest.approx(20.0 / 150.0)
        assert pnl["position_total_tokens"] == 150.0

    def test_sell_winning_full_close(self, ledger: TradeLedger) -> None:
        # Buy 100 @ $0.10, gas $0.001
        ledger.append(_make_entry("buy", 10.0, 0.0, out_amount=100.0))
        # Sell 100 @ $0.15, gas $0.001
        sell = _make_entry("sell", 0.0, 15.0, in_amount=100.0)
        ledger.append(sell)
        pnl = ledger.per_trade_pnl(sell)
        assert pnl["type"] == "sell"
        assert pnl["entry_price"] == pytest.approx(0.1)
        assert pnl["exit_price"] == pytest.approx(0.15)
        assert pnl["cost_basis_usd"] == pytest.approx(10.0)
        assert pnl["proceeds_usd"] == pytest.approx(15.0)
        # Realized = 15 - 10 - 0.001 (gas)
        assert pnl["realized_pnl_usd"] == pytest.approx(4.999, abs=1e-3)
        assert pnl["realized_pnl_pct"] > 49

    def test_sell_losing(self, ledger: TradeLedger) -> None:
        ledger.append(_make_entry("buy", 10.0, 0.0, out_amount=100.0))
        sell = _make_entry("sell", 0.0, 7.0, in_amount=100.0)
        ledger.append(sell)
        pnl = ledger.per_trade_pnl(sell)
        assert pnl["realized_pnl_usd"] < 0
        assert pnl["realized_pnl_pct"] < 0

    def test_sell_partial_close(self, ledger: TradeLedger) -> None:
        # Buy 100 @ $0.10
        ledger.append(_make_entry("buy", 10.0, 0.0, out_amount=100.0))
        # Sell only 50 of those tokens @ $0.20
        sell = _make_entry("sell", 0.0, 10.0, in_amount=50.0)
        ledger.append(sell)
        pnl = ledger.per_trade_pnl(sell)
        # Matched cost = $5 (half of $10), proceeds = $10
        assert pnl["cost_basis_usd"] == pytest.approx(5.0)
        assert pnl["proceeds_usd"] == pytest.approx(10.0)
        assert pnl["tokens_matched"] == pytest.approx(50.0)
        # Realized = 10 - 5 - 0.001
        assert pnl["realized_pnl_usd"] == pytest.approx(4.999, abs=1e-3)

    def test_sell_fifo_across_multiple_buys(self, ledger: TradeLedger) -> None:
        # Buy 50 @ $0.10 ($5)
        ledger.append(_make_entry("buy", 5.0, 0.0, out_amount=50.0))
        # Buy 50 @ $0.20 ($10)
        ledger.append(_make_entry("buy", 10.0, 0.0, out_amount=50.0))
        # Sell 75 @ $0.30 ($22.50) -- closes the first buy and half the second
        sell = _make_entry("sell", 0.0, 22.5, in_amount=75.0)
        ledger.append(sell)
        pnl = ledger.per_trade_pnl(sell)
        # Matched cost = 5 (full first buy) + 5 (half of second buy) = 10
        assert pnl["cost_basis_usd"] == pytest.approx(10.0, abs=1e-3)
        assert pnl["proceeds_usd"] == pytest.approx(22.5)
        # Realized = 22.5 - 10 - 0.001
        assert pnl["realized_pnl_usd"] == pytest.approx(12.499, abs=1e-2)

    def test_sell_with_no_prior_buy_unmatched(self, ledger: TradeLedger) -> None:
        sell = _make_entry("sell", 0.0, 5.0, in_amount=10.0)
        ledger.append(sell)
        pnl = ledger.per_trade_pnl(sell)
        assert pnl["unmatched_tokens"] == 10.0
        assert pnl["cost_basis_usd"] == 0.0


class TestFormatTradePnl:
    def test_buy_format(self) -> None:
        from pod_the_trader.data.ledger import format_trade_pnl

        pnl = {
            "type": "buy",
            "entry_price": 0.1,
            "tokens_acquired": 100.0,
            "cost_usd": 10.0,
            "gas_usd": 0.001,
            "position_avg_entry": 0.1,
            "position_total_tokens": 100.0,
            "position_total_cost": 10.0,
        }
        text = format_trade_pnl(pnl)
        assert "TRADE BUY" in text
        assert "100" in text
        assert "$10" in text or "10.0" in text

    def test_sell_format(self) -> None:
        from pod_the_trader.data.ledger import format_trade_pnl

        pnl = {
            "type": "sell",
            "entry_price": 0.1,
            "exit_price": 0.15,
            "tokens_sold": 100.0,
            "tokens_matched": 100.0,
            "cost_basis_usd": 10.0,
            "proceeds_usd": 15.0,
            "gas_usd": 0.001,
            "realized_pnl_usd": 4.999,
            "realized_pnl_pct": 49.99,
            "unmatched_tokens": 0.0,
        }
        text = format_trade_pnl(pnl)
        assert "TRADE SELL" in text
        assert "REALIZED" in text
        assert "+$4" in text


class TestMigration:
    def test_imports_legacy_json(self, tmp_path: Path) -> None:
        legacy_data = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "side": "buy",
                "input_mint": "SOL",
                "output_mint": "TOKEN",
                "input_amount": 0.1,
                "output_amount": 50.0,
                "price_usd": 0.2,
                "value_usd": 10.0,
                "signature": "legacy_sig",
            }
        ]
        legacy_file = tmp_path / "trade_history.json"
        legacy_file.write_text(json.dumps(legacy_data))

        ledger = TradeLedger(storage_dir=str(tmp_path))
        trades = ledger.read_all()
        assert len(trades) == 1
        assert trades[0].signature == "legacy_sig"
        assert "migrated" in trades[0].notes

    def test_no_legacy_no_migration(self, tmp_path: Path) -> None:
        ledger = TradeLedger(storage_dir=str(tmp_path))
        assert ledger.read_all() == []
