"""Tests for pod_the_trader.data.price_log."""

import math
from pathlib import Path

import pytest

from pod_the_trader.data.price_log import PRICE_COLUMNS, PriceLog, PriceTick, now_iso


@pytest.fixture()
def log(tmp_path: Path) -> PriceLog:
    return PriceLog(storage_dir=str(tmp_path))


def _tick(mint: str, price: float, ts: str | None = None) -> PriceTick:
    return PriceTick(
        timestamp=ts or now_iso(),
        mint=mint,
        symbol="SOL" if mint == "SOL" else "",
        price_usd=price,
        liquidity_usd=1000.0,
        source="test",
    )


class TestAppendAndRead:
    def test_writes_header(self, log: PriceLog) -> None:
        log.append(_tick("SOL", 100.0))
        content = log.path.read_text().splitlines()
        assert content[0] == ",".join(PRICE_COLUMNS)
        assert len(content) == 2

    def test_round_trip(self, log: PriceLog) -> None:
        log.append(_tick("SOL", 100.5))
        ticks = log.read_all()
        assert len(ticks) == 1
        assert ticks[0].price_usd == 100.5
        assert ticks[0].mint == "SOL"

    def test_filter_by_mint(self, log: PriceLog) -> None:
        log.append(_tick("SOL", 100.0))
        log.append(_tick("TARGET", 0.001))
        log.append(_tick("SOL", 101.0))
        assert len(log.read_for_mint("SOL")) == 2
        assert len(log.read_for_mint("TARGET")) == 1
        assert len(log.read_for_mint("OTHER")) == 0

    def test_latest(self, log: PriceLog) -> None:
        log.append(_tick("SOL", 100.0))
        log.append(_tick("SOL", 105.0))
        log.append(_tick("SOL", 102.0))
        latest = log.latest("SOL")
        assert latest is not None
        assert latest.price_usd == 102.0

    def test_latest_no_data(self, log: PriceLog) -> None:
        assert log.latest("SOL") is None


class TestQuantMetrics:
    def test_returns(self, log: PriceLog) -> None:
        log.append(_tick("SOL", 100.0))
        log.append(_tick("SOL", 110.0))
        log.append(_tick("SOL", 121.0))
        rets = log.returns("SOL")
        assert len(rets) == 2
        assert rets[0] == pytest.approx(math.log(110 / 100))
        assert rets[1] == pytest.approx(math.log(121 / 110))

    def test_returns_insufficient_data(self, log: PriceLog) -> None:
        log.append(_tick("SOL", 100.0))
        assert log.returns("SOL") == []

    def test_volatility(self, log: PriceLog) -> None:
        # Constant price = zero volatility
        for _ in range(10):
            log.append(_tick("SOL", 100.0))
        assert log.volatility("SOL") == 0.0

    def test_volatility_nonzero(self, log: PriceLog) -> None:
        prices = [100.0, 105.0, 95.0, 110.0, 90.0]
        for p in prices:
            log.append(_tick("SOL", p))
        vol = log.volatility("SOL")
        assert vol > 0
