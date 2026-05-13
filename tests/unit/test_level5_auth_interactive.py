"""Tests for pod_the_trader.level5.auth interactive flows."""

from pathlib import Path
from unittest.mock import patch

import pytest

from pod_the_trader.level5.auth import Level5Auth, Level5Credentials
from pod_the_trader.level5.provider import PROVIDERS, Provider, resolve_provider


@pytest.fixture()
def auth(tmp_path: Path) -> Level5Auth:
    return Level5Auth(storage_dir=str(tmp_path))


class TestSetupInteractive:
    def test_uses_env_var(self, auth: Level5Auth, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEVEL5_API_TOKEN", "env_token_123")
        creds = auth.setup_interactive()
        assert creds is not None
        assert creds.api_token == "env_token_123"
        assert creds.provider == Provider.LEVEL5.value
        assert auth.has_credentials()

    def test_uses_usepod_env_var_when_active(
        self, auth: Level5Auth, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("USEPOD_API_TOKEN", "up_token_abc")
        # Make sure the other provider's env is absent so we're testing
        # the active-provider lookup, not a fallback.
        monkeypatch.delenv("LEVEL5_API_TOKEN", raising=False)
        creds = auth.setup_interactive(PROVIDERS[Provider.USEPOD.value])
        assert creds is not None
        assert creds.api_token == "up_token_abc"
        assert creds.provider == Provider.USEPOD.value

    def test_inactive_provider_env_does_not_skip_wizard(
        self, auth: Level5Auth, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # USEPOD_API_TOKEN set, but active provider is Level5 — env is
        # ignored, the wizard runs.
        monkeypatch.delenv("LEVEL5_API_TOKEN", raising=False)
        monkeypatch.setenv("USEPOD_API_TOKEN", "ignored_token")
        # Provider chooser default + skip option → returns None.
        with patch("builtins.input", side_effect=["", "3"]):
            creds = auth.setup_interactive(PROVIDERS[Provider.LEVEL5.value])
        assert creds is None
        assert not auth.has_credentials()

    def test_returns_existing_credentials(self, auth: Level5Auth) -> None:
        auth.save(Level5Credentials(api_token="saved_token", provider="usepod"))
        creds = auth.setup_interactive()
        assert creds is not None
        assert creds.api_token == "saved_token"
        assert creds.provider == "usepod"

    def test_register_option_picks_default_provider(self, auth: Level5Auth) -> None:
        # Empty input on the provider prompt = accept default (Level5).
        # Then "1" = register new account.
        with patch("builtins.input", side_effect=["", "1"]):
            creds = auth.setup_interactive()
        assert creds is not None
        assert creds.is_new is True
        assert creds.provider == Provider.LEVEL5.value

    def test_register_option_picks_usepod(self, auth: Level5Auth) -> None:
        # "2" on the provider prompt = pick UsePod. Then "1" = register.
        with patch("builtins.input", side_effect=["2", "1"]):
            creds = auth.setup_interactive()
        assert creds is not None
        assert creds.is_new is True
        assert creds.provider == Provider.USEPOD.value

    def test_enter_token_option(self, auth: Level5Auth) -> None:
        # Provider default + paste + token value.
        with patch("builtins.input", side_effect=["", "2", "my_token_abc"]):
            creds = auth.setup_interactive()
        assert creds is not None
        assert creds.api_token == "my_token_abc"
        assert creds.provider == Provider.LEVEL5.value
        assert auth.has_credentials()

    def test_enter_token_option_for_usepod(self, auth: Level5Auth) -> None:
        with patch("builtins.input", side_effect=["2", "2", "up_token_xyz"]):
            creds = auth.setup_interactive()
        assert creds is not None
        assert creds.api_token == "up_token_xyz"
        assert creds.provider == Provider.USEPOD.value

    def test_enter_empty_token_cancels(self, auth: Level5Auth) -> None:
        with patch("builtins.input", side_effect=["", "2", ""]):
            creds = auth.setup_interactive()
        assert creds is None

    def test_skip_option(self, auth: Level5Auth) -> None:
        with patch("builtins.input", side_effect=["", "3"]):
            creds = auth.setup_interactive()
        assert creds is None

    def test_provider_chooser_unrecognized_falls_back_to_default(self, auth: Level5Auth) -> None:
        # "garbage" doesn't parse as int and isn't blank → falls back
        # to the default Level5; then skip to keep the test simple.
        with patch("builtins.input", side_effect=["garbage", "3"]):
            creds = auth.setup_interactive(resolve_provider("level5"))
        assert creds is None

    def test_provider_chooser_out_of_range_falls_back_to_default(self, auth: Level5Auth) -> None:
        with patch("builtins.input", side_effect=["99", "3"]):
            creds = auth.setup_interactive(resolve_provider("usepod"))
        assert creds is None
