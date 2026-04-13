"""Tests for the Publisher protocol and NullPublisher."""

from pod_the_trader.tui.publisher import NullPublisher, Publisher


class TestNullPublisher:
    def test_implements_protocol(self) -> None:
        pub = NullPublisher()
        assert isinstance(pub, Publisher)

    def test_all_methods_are_noops(self) -> None:
        pub = NullPublisher()
        # None of these should raise, and all should return None.
        assert (
            pub.on_startup(
                wallet="w",
                target="t",
                model="m",
                cooldown=300,
                ledger_summary={"trade_count": 0},
            )
            is None
        )
        assert pub.on_cycle_start(1, "ts") is None
        assert pub.on_cycle_complete({"decision": "HOLD"}) is None
        assert pub.on_trade({}, {}) is None
        assert pub.on_price_tick("mint", 1.0, "ts") is None
        assert pub.on_portfolio_snapshot({}) is None
        assert pub.on_level5_balance(0.0, 0.0) is None
        assert pub.on_log("INFO", "msg") is None
        assert pub.on_shutdown({}) is None
