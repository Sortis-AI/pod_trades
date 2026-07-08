"""Tests for the Portfolio widget — clickable wallet-address copy."""

from __future__ import annotations

from unittest.mock import MagicMock

from textual.app import App, ComposeResult

from pod_the_trader.tui.widgets.portfolio import PortfolioWidget

WALLET = "HHCRB4Dq71JwoR784omiVyYhbRtpGaUQjm3aNtkaJdyk"
SNAPSHOT = {
    "sol_ui": 2.5,
    "sol_value_usd": 190.0,
    "usdc_ui": 0.0,
    "usdc_value_usd": 0.0,
    "token_ui": 264916.0,
    "token_value_usd": 1012.0,
    "total_usd": 1202.0,
}


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield PortfolioWidget(id="portfolio")

    @property
    def widget(self) -> PortfolioWidget:
        return self.query_one("#portfolio", PortfolioWidget)


class TestWalletCopy:
    async def test_wallet_renders_as_clickable_link(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            w = app.widget
            w.wallet_address = WALLET
            w.snapshot = SNAPSHOT
            await pilot.pause()
            content = w._format(w.snapshot)
            # The address is wrapped in a @click action link so Textual
            # makes it clickable, and it's the real wallet address, with a
            # visible affordance that it can be clicked.
            assert "@click=copy_wallet" in content
            assert WALLET in content
            assert "click to copy" in content

    async def test_click_action_copies_to_clipboard(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            w = app.widget
            w.wallet_address = WALLET
            w.snapshot = SNAPSHOT
            await pilot.pause()

            # Simulate what Textual does when the content link is clicked:
            # resolve and run the "copy_wallet" action against the widget.
            copied: list[str] = []
            app.copy_to_clipboard = MagicMock(side_effect=lambda s: copied.append(s))
            app.notify = MagicMock()

            await w.run_action("copy_wallet")

            assert copied == [WALLET]
            assert app.notify.called

    async def test_no_wallet_line_without_address(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            w = app.widget
            w.snapshot = SNAPSHOT  # no wallet_address set
            await pilot.pause()
            content = w._format(w.snapshot)
            assert "wallet:" not in content
            assert "@click=copy_wallet" not in content

    async def test_copy_action_is_noop_without_address(self) -> None:
        app = _Harness()
        async with app.run_test() as pilot:
            w = app.widget
            await pilot.pause()
            app.copy_to_clipboard = MagicMock()
            await w.run_action("copy_wallet")
            app.copy_to_clipboard.assert_not_called()
