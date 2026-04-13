"""Tests for the parse_decision() robustness."""

from pod_the_trader.agent.core import parse_decision


class TestStrictFormat:
    def test_basic_hold(self) -> None:
        action, reason = parse_decision("DECISION: HOLD — Price stable, no signal.")
        assert action == "HOLD"
        assert "Price stable" in reason

    def test_basic_buy(self) -> None:
        action, reason = parse_decision("DECISION: BUY — Strong breakout above resistance.")
        assert action == "BUY"
        assert "breakout" in reason

    def test_basic_sell(self) -> None:
        action, reason = parse_decision("DECISION: SELL — Taking profit at 2x entry.")
        assert action == "SELL"
        assert "profit" in reason

    def test_bolded_decision(self) -> None:
        action, _ = parse_decision("**DECISION:** HOLD — Bolded format should still match.")
        assert action == "HOLD"

    def test_different_dashes(self) -> None:
        for dash in ("—", "–", "-", ":"):
            action, _ = parse_decision(f"DECISION: HOLD {dash} reason here")
            assert action == "HOLD"

    def test_case_insensitive(self) -> None:
        action, _ = parse_decision("decision: hold — lowercase")
        assert action == "HOLD"

    def test_strict_wins_over_body(self) -> None:
        # Body contains "buy" but DECISION line says HOLD
        response = (
            "I considered whether to buy more but decided against it.\n"
            "DECISION: HOLD — waiting for clearer signal."
        )
        action, _ = parse_decision(response)
        assert action == "HOLD"

    def test_no_trade_normalizes_to_hold(self) -> None:
        action, _ = parse_decision("DECISION: NO TRADE — insufficient capital")
        assert action == "HOLD"


class TestLooseHeading:
    def test_trading_decision_heading(self) -> None:
        response = "## 🎯 Trading Decision: **NO TRADE**\n\nNot enough SOL to trade."
        action, reason = parse_decision(response)
        assert action == "HOLD"

    def test_decision_heading_simple(self) -> None:
        response = "Decision: HOLD\n\nMarket is flat."
        action, _ = parse_decision(response)
        assert action == "HOLD"

    def test_take_profit_heading(self) -> None:
        response = "## Trading Decision: TAKE PROFIT\n\nUp 30%, exit half."
        action, _ = parse_decision(response)
        assert action == "SELL"

    def test_buy_more_heading(self) -> None:
        response = "Trading Decision: BUY MORE — averaging down."
        action, _ = parse_decision(response)
        assert action == "BUY"


class TestPhraseInference:
    def test_no_trade_phrase(self) -> None:
        response = (
            "After reviewing the data I have determined that no trade is warranted.\n"
            "The market conditions are not favorable for entry at this time."
        )
        action, _ = parse_decision(response)
        assert action == "HOLD"

    def test_sell_phrase(self) -> None:
        response = (
            "The position is up significantly.\n"
            "Recommendation: take profit on half the position immediately."
        )
        action, _ = parse_decision(response)
        assert action == "SELL"


class TestUnknownFallback:
    def test_no_decision_keywords(self) -> None:
        response = "Market data received. Analysis pending further review."
        action, reason = parse_decision(response)
        assert action == "UNKNOWN"
        assert len(reason) > 0

    def test_empty_response(self) -> None:
        action, reason = parse_decision("")
        assert action == "UNKNOWN"
        assert reason == "(no response)"
