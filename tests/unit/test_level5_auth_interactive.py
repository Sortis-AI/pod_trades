"""Tests for pod_the_trader.level5.auth interactive flows."""

from pathlib import Path
from unittest.mock import patch

import pytest

from pod_the_trader.level5.auth import Level5Auth, Level5Credentials


@pytest.fixture()
def auth(tmp_path: Path) -> Level5Auth:
    return Level5Auth(storage_dir=str(tmp_path))


class TestSetupInteractive:
    def test_uses_env_var(self, auth: Level5Auth, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEVEL5_API_TOKEN", "env_token_123")
        creds = auth.setup_interactive()
        assert creds is not None
        assert creds.api_token == "env_token_123"
        assert auth.has_credentials()

    def test_returns_existing_credentials(self, auth: Level5Auth) -> None:
        auth.save(Level5Credentials(api_token="saved_token"))
        creds = auth.setup_interactive()
        assert creds is not None
        assert creds.api_token == "saved_token"

    def test_register_option(self, auth: Level5Auth) -> None:
        with patch("builtins.input", return_value="1"):
            creds = auth.setup_interactive()
        assert creds is not None
        assert creds.is_new is True

    def test_enter_token_option(self, auth: Level5Auth) -> None:
        with patch("builtins.input", side_effect=["2", "my_token_abc"]):
            creds = auth.setup_interactive()
        assert creds is not None
        assert creds.api_token == "my_token_abc"
        assert auth.has_credentials()

    def test_enter_empty_token_cancels(self, auth: Level5Auth) -> None:
        with patch("builtins.input", side_effect=["2", ""]):
            creds = auth.setup_interactive()
        assert creds is None

    def test_skip_option(self, auth: Level5Auth) -> None:
        with patch("builtins.input", return_value="3"):
            creds = auth.setup_interactive()
        assert creds is None
