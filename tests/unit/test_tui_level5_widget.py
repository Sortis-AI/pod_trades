"""Tests for the Level5 billing widget, specifically the session-spend
calculation that has repeatedly regressed.

The widget captures a session-start baseline on the first reading and shows
the delta on every subsequent reading. Two historical regressions:

1. Priming with (0.0, 0.0) before the first successful Level5 /balance
   call pinned ``session_start`` to zero and made ``spent`` clamp to 0 for
   the rest of the session.
2. Gating the spent computation on BOTH ``session_start_usdc`` AND
   ``session_start_credit`` being non-None broke accounts with zero
   promotional credits — credit side never seeded, and the gate zeroed
   out the USDC side too.

These tests exercise the widget through ``App.run_test()`` so the Textual
reactive machinery is real, not mocked.
"""

from __future__ import annotations

from textual.app import App, ComposeResult

from pod_the_trader.tui.widgets.level5 import Level5Widget


class _Level5Harness(App):
    """Minimal Textual app wrapping a single Level5Widget so we can drive
    its reactives and inspect the rendered output.
    """

    def compose(self) -> ComposeResult:
        yield Level5Widget(id="level5")

    @property
    def widget(self) -> Level5Widget:
        return self.query_one("#level5", Level5Widget)


class TestSessionSpendAccounting:
    async def test_first_reading_shows_zero_spent(self) -> None:
        app = _Level5Harness()
        async with app.run_test() as pilot:
            app.widget.usdc = 6.024548
            app.widget.credit = 0.0
            await pilot.pause()
            # Spent should be exactly $0 after the first reading (we just
            # seeded the baseline).
            rendered = str(app.widget.render())
            assert "Session:" in rendered
            assert "$0.000000" in rendered
            # USDC baseline captured on the first real reading. Note:
            # credit's session_start may remain None here — Textual's
            # reactive system skips ``watch_credit`` when the new value
            # equals the default (0.0), so the callback never fires. The
            # ``_format`` code handles ``None`` by treating credit spent
            # as 0, so the end-user display is still correct.
            assert app.widget._session_start_usdc == 6.024548

    async def test_usdc_drain_with_zero_credits_is_tracked(self) -> None:
        """The bug this test exists for: user has no promotional credits,
        USDC drains each cycle, Session used to show $0 because the credit
        side's session_start was None and gated out the USDC calculation.
        """
        app = _Level5Harness()
        async with app.run_test() as pilot:
            # Cycle 1: seed baseline
            app.widget.usdc = 6.024548
            app.widget.credit = 0.0
            await pilot.pause()

            # Cycles 2-4: USDC drains, credit stays 0
            app.widget.usdc = 6.009282
            await pilot.pause()
            app.widget.usdc = 6.003223
            await pilot.pause()
            app.widget.usdc = 6.000492
            await pilot.pause()

            # Spent should match the real drain: 6.024548 - 6.000492
            expected_spent = 6.024548 - 6.000492  # ≈ 0.024056
            rendered = str(app.widget.render())
            assert "Session:" in rendered
            # Render uses 6 decimals → $0.024056
            assert f"${expected_spent:.6f}" in rendered

    async def test_credit_drain_with_zero_usdc_is_tracked(self) -> None:
        """Mirror case: account has only promotional credits, no USDC."""
        app = _Level5Harness()
        async with app.run_test() as pilot:
            app.widget.usdc = 0.0
            app.widget.credit = 10.0
            await pilot.pause()

            app.widget.credit = 9.997
            await pilot.pause()

            rendered = str(app.widget.render())
            expected_spent = 0.003
            assert f"${expected_spent:.6f}" in rendered

    async def test_both_sides_drain_are_summed(self) -> None:
        app = _Level5Harness()
        async with app.run_test() as pilot:
            app.widget.usdc = 5.0
            app.widget.credit = 2.0
            await pilot.pause()

            app.widget.usdc = 4.99
            app.widget.credit = 1.995
            await pilot.pause()

            rendered = str(app.widget.render())
            # spent_usdc 0.01 + spent_credit 0.005 = 0.015
            assert "$0.015000" in rendered
            assert "$0.010000" in rendered  # usdc line
            assert "$0.005000" in rendered  # credit line

    async def test_spent_never_goes_negative_on_deposit(self) -> None:
        """If the user tops up mid-session, balance goes UP. Spent should
        clamp to 0 rather than going negative.
        """
        app = _Level5Harness()
        async with app.run_test() as pilot:
            app.widget.usdc = 6.0
            app.widget.credit = 0.0
            await pilot.pause()

            # Top-up: balance went up by $4
            app.widget.usdc = 10.0
            await pilot.pause()

            rendered = str(app.widget.render())
            # Spent clamped at 0.
            assert "$0.000000" in rendered

    async def test_zero_credit_does_not_block_usdc_spent(self) -> None:
        """Regression test for the specific fix: the credit side being
        stuck at (None or 0) must not gate out the USDC spent calculation.
        Drives only USDC updates and leaves credit untouched at 0.
        """
        app = _Level5Harness()
        async with app.run_test() as pilot:
            app.widget.usdc = 2.0
            app.widget.credit = 0.0
            await pilot.pause()

            # Simulate a $0.50 USDC charge
            app.widget.usdc = 1.5
            await pilot.pause()

            rendered = str(app.widget.render())
            assert "$0.500000" in rendered
            # Credit side is 0, but the overall spent reflects the USDC drain.
            # (Total spent = usdc spent + credit spent = 0.5 + 0 = 0.5)

    async def test_subsequent_readings_do_not_reset_baseline(self) -> None:
        app = _Level5Harness()
        async with app.run_test() as pilot:
            app.widget.usdc = 6.0
            app.widget.credit = 1.0
            await pilot.pause()
            baseline_usdc = app.widget._session_start_usdc
            baseline_credit = app.widget._session_start_credit

            # More cycles — session start must remain pinned.
            app.widget.usdc = 5.5
            app.widget.credit = 0.8
            await pilot.pause()
            app.widget.usdc = 5.0
            app.widget.credit = 0.5
            await pilot.pause()

            assert app.widget._session_start_usdc == baseline_usdc
            assert app.widget._session_start_credit == baseline_credit
