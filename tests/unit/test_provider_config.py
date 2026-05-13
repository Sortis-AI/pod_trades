"""Tests for pod_the_trader.level5.provider — Provider enum,
ProviderConfig, PROVIDERS table, and resolve_provider helper.
"""

import pytest

from pod_the_trader.level5.provider import (
    PROVIDERS,
    Provider,
    ProviderConfig,
    resolve_provider,
)


class TestProvidersTable:
    def test_contains_level5_and_usepod(self) -> None:
        assert Provider.LEVEL5.value in PROVIDERS
        assert Provider.USEPOD.value in PROVIDERS

    def test_level5_config(self) -> None:
        cfg = PROVIDERS[Provider.LEVEL5.value]
        assert isinstance(cfg, ProviderConfig)
        assert cfg.key == "level5"
        assert cfg.display_name == "Level5"
        assert cfg.default_domain == "level5.cloud"
        assert cfg.has_credits is True
        # Path-style dashboard URL.
        assert "/dashboard/{token}" in cfg.dashboard_url_template
        assert "?token=" not in cfg.dashboard_url_template

    def test_usepod_config(self) -> None:
        cfg = PROVIDERS[Provider.USEPOD.value]
        assert cfg.key == "usepod"
        assert cfg.display_name == "UsePod"
        assert cfg.default_domain == "usepod.ai"
        assert cfg.has_credits is False
        # Query-style dashboard URL.
        assert "?token={token}" in cfg.dashboard_url_template


class TestResolveProvider:
    def test_explicit_level5(self) -> None:
        cfg = resolve_provider("level5")
        assert cfg.key == "level5"

    def test_explicit_usepod(self) -> None:
        cfg = resolve_provider("usepod")
        assert cfg.key == "usepod"

    def test_none_defaults_to_level5(self) -> None:
        assert resolve_provider(None).key == "level5"

    def test_empty_string_defaults_to_level5(self) -> None:
        assert resolve_provider("").key == "level5"

    def test_case_insensitive(self) -> None:
        assert resolve_provider("LEVEL5").key == "level5"
        assert resolve_provider("UsePod").key == "usepod"
        assert resolve_provider("  USEPOD  ").key == "usepod"

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider"):
            resolve_provider("openai")

    def test_provider_is_frozen(self) -> None:
        # ProviderConfig is frozen so configs can't be mutated at runtime
        # by surprise. This protects the PROVIDERS dict from drift.
        from dataclasses import FrozenInstanceError

        cfg = resolve_provider("level5")
        with pytest.raises(FrozenInstanceError):
            cfg.display_name = "Pwned"  # type: ignore[misc]


class TestDashboardUrlTemplate:
    def test_level5_path_style(self) -> None:
        cfg = resolve_provider("level5")
        url = cfg.dashboard_url_template.format(domain="level5.cloud", token="abc123")
        assert url == "https://level5.cloud/dashboard/abc123"

    def test_usepod_query_style(self) -> None:
        cfg = resolve_provider("usepod")
        url = cfg.dashboard_url_template.format(domain="usepod.ai", token="abc123")
        assert url == "https://usepod.ai/dashboard?token=abc123"
