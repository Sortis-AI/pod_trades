"""Tests for pod_the_trader.config."""

from pathlib import Path

import pytest
import yaml

from pod_the_trader.config import Config, ConfigError

TEST_TOKEN = "So11111111111111111111111111111111111111112"


class TestConfigLoading:
    def test_loads_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "c.yaml"
        config_file.write_text(yaml.dump({"trading": {"target_token_address": TEST_TOKEN}}))
        config = Config(str(config_file))
        assert config.get("agent.name") == "Pod The Trader"
        assert config.get("trading.max_slippage_bps") == 50

    def test_deep_merges_user_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "c.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "trading": {"target_token_address": TEST_TOKEN, "max_slippage_bps": 100},
                    "agent": {"name": "Custom Bot"},
                }
            )
        )
        config = Config(str(config_file))
        assert config.get("agent.name") == "Custom Bot"
        assert config.get("trading.max_slippage_bps") == 100
        # Unmerged defaults still present
        assert config.get("agent.model") == "minimax-m2.7"

    def test_env_var_overrides(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TARGET_TOKEN_ADDRESS", TEST_TOKEN)
        monkeypatch.setenv("SOLANA_RPC_URL", "https://custom-rpc.example.com")
        config = Config()
        assert config.get("trading.target_token_address") == TEST_TOKEN
        assert config.get("solana.rpc_url") == "https://custom-rpc.example.com"

    def test_env_var_creates_missing_section(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TARGET_TOKEN_ADDRESS", TEST_TOKEN)
        monkeypatch.setenv("LEVEL5_API_TOKEN", "test_token_value")
        config = Config()
        assert config.get("level5.api_token") == "test_token_value"


class TestConfigValidation:
    def test_raises_on_empty_token_address(self) -> None:
        with pytest.raises(ConfigError, match="target_token_address must be set"):
            Config()

    def test_raises_on_placeholder_token_address(self, tmp_path: Path) -> None:
        config_file = tmp_path / "c.yaml"
        config_file.write_text(
            yaml.dump({"trading": {"target_token_address": "SQUIRE_TOKEN_ADDRESS_HERE"}})
        )
        with pytest.raises(ConfigError, match="target_token_address must be set"):
            Config(str(config_file))

    def test_raises_on_whitespace_only_token(self, tmp_path: Path) -> None:
        config_file = tmp_path / "c.yaml"
        config_file.write_text(yaml.dump({"trading": {"target_token_address": "   "}}))
        with pytest.raises(ConfigError, match="target_token_address must be set"):
            Config(str(config_file))


class TestConfigGet:
    def test_returns_correct_types(self, sample_config: Config) -> None:
        assert isinstance(sample_config.get("trading.max_slippage_bps"), int)
        assert isinstance(sample_config.get("level5.max_daily_spend_usdc"), float)
        assert isinstance(sample_config.get("agent.name"), str)

    def test_returns_default_for_missing_key(self, sample_config: Config) -> None:
        assert sample_config.get("nonexistent.key") is None
        assert sample_config.get("nonexistent.key", 42) == 42

    def test_returns_nested_dict(self, sample_config: Config) -> None:
        trading = sample_config.get("trading")
        assert isinstance(trading, dict)
        assert "max_slippage_bps" in trading

    def test_data_property_returns_copy(self, sample_config: Config) -> None:
        data1 = sample_config.data
        data2 = sample_config.data
        assert data1 == data2
        assert data1 is not data2
        data1["agent"]["name"] = "mutated"
        assert sample_config.get("agent.name") != "mutated"
