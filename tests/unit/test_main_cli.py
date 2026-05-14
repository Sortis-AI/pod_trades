"""Tests for main.py CLI arg parsing and UI mode resolution."""

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pod_the_trader.level5.auth import Level5Credentials
from pod_the_trader.level5.provider import resolve_provider
from pod_the_trader.main import (
    _parse_cli_args,
    _reconcile_provider_with_creds,
    _resolve_ui_mode,
)


def _fake_config(values: dict[str, Any] | None = None) -> MagicMock:
    """Minimal Config stand-in that responds to ``.get(key, default)``."""
    data = values or {}

    def get(key: str, default: Any = None) -> Any:
        return data.get(key, default)

    config = MagicMock()
    config.get.side_effect = get
    return config


class TestParseCliArgs:
    def test_no_args(self) -> None:
        assert _parse_cli_args([]) == (None, "auto", None, None)

    def test_config_path_positional(self) -> None:
        assert _parse_cli_args(["/path/to/config.yaml"]) == (
            "/path/to/config.yaml",
            "auto",
            None,
            None,
        )

    def test_tui_flag(self) -> None:
        assert _parse_cli_args(["--tui"]) == (None, "tui", None, None)

    def test_cli_flag(self) -> None:
        assert _parse_cli_args(["--cli"]) == (None, "cli", None, None)

    def test_flag_and_config(self) -> None:
        assert _parse_cli_args(["--tui", "config.yaml"]) == (
            "config.yaml",
            "tui",
            None,
            None,
        )

    def test_config_then_flag(self) -> None:
        assert _parse_cli_args(["config.yaml", "--cli"]) == (
            "config.yaml",
            "cli",
            None,
            None,
        )

    def test_base_domain_space_form(self) -> None:
        assert _parse_cli_args(["--base-domain", "usepod.ai"]) == (
            None,
            "auto",
            "usepod.ai",
            None,
        )

    def test_base_domain_equals_form(self) -> None:
        assert _parse_cli_args(["--base-domain=usepod.ai"]) == (
            None,
            "auto",
            "usepod.ai",
            None,
        )

    def test_base_domain_with_config_and_mode(self) -> None:
        assert _parse_cli_args(["config.yaml", "--cli", "--base-domain", "usepod.ai"]) == (
            "config.yaml",
            "cli",
            "usepod.ai",
            None,
        )

    def test_base_domain_missing_value_raises(self) -> None:
        with pytest.raises(SystemExit):
            _parse_cli_args(["--base-domain"])

    def test_provider_space_form(self) -> None:
        assert _parse_cli_args(["--provider", "usepod"]) == (
            None,
            "auto",
            None,
            "usepod",
        )

    def test_provider_equals_form(self) -> None:
        assert _parse_cli_args(["--provider=usepod"]) == (
            None,
            "auto",
            None,
            "usepod",
        )

    def test_provider_with_base_domain_and_config(self) -> None:
        assert _parse_cli_args(
            ["config.yaml", "--provider", "usepod", "--base-domain", "alt.example"]
        ) == ("config.yaml", "auto", "alt.example", "usepod")

    def test_provider_missing_value_raises(self) -> None:
        with pytest.raises(SystemExit):
            _parse_cli_args(["--provider"])


class TestReconcileProviderWithCreds:
    """The reconciler is the fix for the 0.3.0 bug where picking
    UsePod in the wizard while the config default was Level5 caused
    the bot to register a Level5 account by mistake. ``creds.provider``
    is authoritative after the wizard returns; reconciliation aligns
    the active provider + base_domain with that field.
    """

    def test_creds_none_returns_resolved_default(self) -> None:
        config = _fake_config({"level5.base_domain": "level5.cloud"})
        provider = resolve_provider("level5")
        new_provider, base_domain = _reconcile_provider_with_creds(
            provider, None, config, base_domain_override=None
        )
        assert new_provider.key == "level5"
        assert base_domain == "level5.cloud"

    def test_creds_match_returns_same_provider(self) -> None:
        config = _fake_config({"level5.base_domain": "level5.cloud"})
        provider = resolve_provider("level5")
        creds = Level5Credentials(api_token="t", provider="level5")
        new_provider, base_domain = _reconcile_provider_with_creds(
            provider, creds, config, base_domain_override=None
        )
        assert new_provider.key == "level5"
        assert base_domain == "level5.cloud"

    def test_creds_override_default_when_provider_differs(self) -> None:
        # The exact bug fixed by this helper: outer provider resolved
        # to level5 (CLI/config default), but the wizard returned
        # creds with provider="usepod" → we must switch.
        config = _fake_config({"usepod.base_domain": "usepod.ai"})
        provider = resolve_provider("level5")
        creds = Level5Credentials(api_token="t", provider="usepod", is_new=True)
        new_provider, base_domain = _reconcile_provider_with_creds(
            provider, creds, config, base_domain_override=None
        )
        assert new_provider.key == "usepod"
        assert base_domain == "usepod.ai"

    def test_creds_override_falls_back_to_provider_default_domain(self) -> None:
        # Config doesn't have a usepod.base_domain entry (degenerate
        # custom YAML); provider's default_domain is the fallback.
        config = _fake_config({})
        provider = resolve_provider("level5")
        creds = Level5Credentials(api_token="t", provider="usepod")
        new_provider, base_domain = _reconcile_provider_with_creds(
            provider, creds, config, base_domain_override=None
        )
        assert new_provider.key == "usepod"
        assert base_domain == "usepod.ai"

    def test_cli_base_domain_override_wins_after_provider_switch(self) -> None:
        # Even when the wizard switches us to UsePod, an explicit
        # --base-domain still trumps both the config and the provider
        # default — this is the "self-hosted Level5-compat deployment
        # at a custom host" escape hatch.
        config = _fake_config({"usepod.base_domain": "usepod.ai"})
        provider = resolve_provider("level5")
        creds = Level5Credentials(api_token="t", provider="usepod")
        new_provider, base_domain = _reconcile_provider_with_creds(
            provider, creds, config, base_domain_override="custom.example.com"
        )
        assert new_provider.key == "usepod"
        assert base_domain == "custom.example.com"

    def test_loaded_creds_for_other_provider_switch_correctly(self) -> None:
        # A user with saved UsePod credentials but no --provider flag
        # (config default level5) should still launch on UsePod — the
        # creds win.
        config = _fake_config({"usepod.base_domain": "usepod.ai"})
        provider = resolve_provider("level5")
        creds = Level5Credentials(api_token="saved-usepod-token", provider="usepod", is_new=False)
        new_provider, base_domain = _reconcile_provider_with_creds(
            provider, creds, config, base_domain_override=None
        )
        assert new_provider.key == "usepod"
        assert base_domain == "usepod.ai"


class TestResolveUiMode:
    def test_explicit_tui(self) -> None:
        assert _resolve_ui_mode("tui") == "tui"

    def test_explicit_cli(self) -> None:
        assert _resolve_ui_mode("cli") == "cli"

    def test_auto_with_tty(self) -> None:
        with patch.object(sys.stdout, "isatty", return_value=True):
            assert _resolve_ui_mode("auto") == "tui"

    def test_auto_without_tty(self) -> None:
        with patch.object(sys.stdout, "isatty", return_value=False):
            assert _resolve_ui_mode("auto") == "cli"
