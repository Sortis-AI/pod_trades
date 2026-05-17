"""Tests for CyclePriceGuard — the per-cycle cumulative slippage gate."""

from __future__ import annotations

from pod_the_trader.trading.cycle_guard import CyclePriceGuard

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
TARGET = "EN2nnxrg8uUi6x2sJkzNPd2eT6rB9rdSoQNNaENA4RZA"


class TestFirstSliceAlwaysAllowed:
    def test_no_anchor_returns_none(self) -> None:
        # Before any fill is recorded, every quote passes.
        guard = CyclePriceGuard(max_drift_pct=0.5)
        assert guard.check_quote(USDC, TARGET, 25_000_000, 33_803_000_000) is None


class TestAnchorBlocksDegradedFollowup:
    def test_drift_inside_threshold_allows(self) -> None:
        # First slice: 25 USDC → 33,803 TARGET (raw 33,803_000_000).
        # Anchor ratio = 33,803_000_000 / 25_000_000 = 1352.12.
        # Follow-up quote: 50 USDC → 67,520_000_000 → ratio 1350.4
        # → drift = (1350.4 - 1352.12) / 1352.12 = -0.127% → allow.
        guard = CyclePriceGuard(max_drift_pct=0.5)
        guard.record_fill(USDC, TARGET, 25_000_000, 33_803_000_000)
        assert guard.check_quote(USDC, TARGET, 50_000_000, 67_520_000_000) is None

    def test_drift_past_threshold_blocks(self) -> None:
        # Reproduce the production scenario: first $25 fills at 33,803
        # tokens per $25, follow-up $75 quote returns 99,890 tokens per
        # $75 — ratio walked from 1352.12 to 1331.87 → -1.50% → block
        # at 0.5%. The error string should also report the actual drift
        # so an operator scanning the log sees how far off we were.
        guard = CyclePriceGuard(max_drift_pct=0.5)
        guard.record_fill(USDC, TARGET, 25_000_000, 33_803_000_000)
        err = guard.check_quote(USDC, TARGET, 75_000_000, 99_890_000_000)
        assert err is not None
        assert "Cycle-cumulative slippage gate" in err
        assert "1.50%" in err

    def test_favorable_drift_always_allowed(self) -> None:
        # If the pool moved in our favor (better rate than the anchor),
        # the gate never fires — only unfavorable drift is blocked.
        guard = CyclePriceGuard(max_drift_pct=0.5)
        guard.record_fill(USDC, TARGET, 25_000_000, 33_803_000_000)
        # 50 USDC at a 2% better rate.
        better = int(33_803_000_000 / 25_000_000 * 50_000_000 * 1.02)
        assert guard.check_quote(USDC, TARGET, 50_000_000, better) is None


class TestAnchorOnlyTracksFirstFill:
    def test_subsequent_fills_dont_move_anchor(self) -> None:
        # The anchor is the FIRST slice's ratio; later fills don't
        # replace it (so a series of progressively-worse fills keeps
        # being judged against the cleanest one).
        guard = CyclePriceGuard(max_drift_pct=0.5)
        guard.record_fill(USDC, TARGET, 25_000_000, 33_803_000_000)
        guard.record_fill(USDC, TARGET, 50_000_000, 66_802_000_000)
        # A third quote that's only worse than the SECOND fill (but
        # not worse than the FIRST) — gate uses the first ratio.
        # First ratio: 1352.12. Second-fill ratio: 1336.04.
        # Quote rate 1340 is below first-anchor by 0.9% → block.
        bad_quote_out = int(75_000_000 * 1340)
        err = guard.check_quote(USDC, TARGET, 75_000_000, bad_quote_out)
        assert err is not None


class TestDirectionalAnchoring:
    def test_separate_anchors_per_direction(self) -> None:
        # A BUY anchor doesn't gate SELLs and vice-versa. Each pair
        # of (input_mint, output_mint) carries its own anchor — the
        # ratio scale is different in each direction so they must
        # not interfere.
        guard = CyclePriceGuard(max_drift_pct=0.5)
        guard.record_fill(USDC, TARGET, 25_000_000, 33_803_000_000)
        # Sell direction has no anchor yet → passes.
        assert guard.check_quote(TARGET, USDC, 33_803_000_000, 24_900_000) is None


class TestReset:
    def test_reset_clears_anchors(self) -> None:
        # Between cycles the agent calls reset() so the next cycle
        # establishes a fresh anchor instead of inheriting last
        # cycle's pool state.
        guard = CyclePriceGuard(max_drift_pct=0.5)
        guard.record_fill(USDC, TARGET, 25_000_000, 33_803_000_000)
        guard.reset()
        # Even a quote that would have been blocked previously now
        # passes — the slate is clean.
        assert guard.check_quote(USDC, TARGET, 75_000_000, 99_890_000_000) is None


class TestDegenerateInputs:
    def test_zero_amounts_dont_record_or_check(self) -> None:
        # Defensive: a malformed quote with zero amounts must neither
        # establish an anchor nor blow up the gate-check.
        guard = CyclePriceGuard(max_drift_pct=0.5)
        guard.record_fill(USDC, TARGET, 0, 0)
        # No anchor was recorded — a real quote afterward still passes.
        assert guard.check_quote(USDC, TARGET, 25_000_000, 33_803_000_000) is None
