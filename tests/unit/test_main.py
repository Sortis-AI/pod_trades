"""Tests for pod_the_trader.main."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pod_the_trader.config import Config


class TestConfigureLogging:
    def test_configures_handlers(self, sample_config: Config, tmp_path: Path) -> None:
        import logging

        from pod_the_trader.main import _configure_logging

        # Clear existing handlers
        root = logging.getLogger()
        original_handlers = root.handlers[:]
        root.handlers.clear()

        try:
            _configure_logging(sample_config)
            assert len(root.handlers) >= 2  # console + file
        finally:
            root.handlers = original_handlers


class TestAsyncMain:
    async def test_exits_on_config_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pod_the_trader.main import async_main

        # Explicitly override the packaged default's target token with an
        # empty string so the validator fails with ConfigError.
        config_file = tmp_path / "bad_config.yaml"
        config_file.write_text('trading:\n  target_token_address: ""\n')
        with pytest.raises(SystemExit):
            await async_main(config_path=str(config_file))

    async def test_exits_without_level5_creds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pod_the_trader.main import async_main

        monkeypatch.setenv("TARGET_TOKEN_ADDRESS", "So111111111111111111111111111111111111111")
        with (
            patch("pod_the_trader.main.Level5Auth") as mock_auth_cls,
            pytest.raises(SystemExit),
        ):
            mock_auth = MagicMock()
            mock_auth.setup_interactive.return_value = None
            mock_auth_cls.return_value = mock_auth
            await async_main()

    async def test_exits_without_wallet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pod_the_trader.main import async_main

        monkeypatch.setenv("TARGET_TOKEN_ADDRESS", "So111111111111111111111111111111111111111")

        from pod_the_trader.level5.auth import Level5Credentials

        with (
            patch("pod_the_trader.main.Level5Auth") as mock_auth_cls,
            patch("pod_the_trader.main.WalletSetup") as mock_setup_cls,
            pytest.raises(SystemExit),
        ):
            mock_auth = MagicMock()
            mock_auth.setup_interactive.return_value = Level5Credentials(api_token="tok")
            mock_auth_cls.return_value = mock_auth

            mock_setup = MagicMock()
            mock_setup.run.return_value = None  # No wallet
            mock_setup_cls.return_value = mock_setup

            await async_main()
