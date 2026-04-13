"""Configuration loading, validation, and access."""

import copy
import os
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when configuration is invalid or incomplete."""


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override values win."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


_ENV_OVERRIDES: dict[str, str] = {
    "SOLANA_RPC_URL": "solana.rpc_url",
    "TARGET_TOKEN_ADDRESS": "trading.target_token_address",
    "LEVEL5_API_TOKEN": "level5.api_token",
    "SOLANA_PRIVATE_KEY": "wallet.private_key",
}


class Config:
    """Immutable, validated application configuration.

    Loads defaults from the bundled defaults.yaml, optionally merges a
    user-provided YAML file on top, then applies environment variable
    overrides.  Validates required fields at construction time.
    """

    def __init__(self, config_path: str | None = None) -> None:
        defaults_path = Path(__file__).parent / "defaults.yaml"
        with open(defaults_path) as f:
            data = yaml.safe_load(f)

        if config_path is not None:
            with open(config_path) as f:
                user_data = yaml.safe_load(f)
            if user_data:
                data = _deep_merge(data, user_data)

        self._apply_env_overrides(data)
        self._validate(data)
        self._data: dict = data

    def _apply_env_overrides(self, data: dict) -> None:
        for env_var, dotted_key in _ENV_OVERRIDES.items():
            value = os.environ.get(env_var)
            if value is not None:
                self._set_dotted(data, dotted_key, value)

    def _set_dotted(self, data: dict, dotted_key: str, value: Any) -> None:
        keys = dotted_key.split(".")
        target = data
        for key in keys[:-1]:
            if key not in target:
                target[key] = {}
            target = target[key]
        target[keys[-1]] = value

    def _validate(self, data: dict) -> None:
        token_addr = self._get_from_dict(data, "trading.target_token_address")
        is_placeholder = isinstance(token_addr, str) and (
            "_HERE" in token_addr or not token_addr.strip()
        )
        if not token_addr or is_placeholder:
            raise ConfigError(
                "trading.target_token_address must be set to a valid SPL token mint address. "
                "Set it in your config file or via the TARGET_TOKEN_ADDRESS environment variable."
            )

    def _get_from_dict(self, data: dict, dotted_key: str, default: Any = None) -> Any:
        keys = dotted_key.split(".")
        current = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        return current

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """Retrieve a config value by dot-separated key path."""
        return self._get_from_dict(self._data, dotted_key, default)

    @property
    def data(self) -> dict:
        """Return a deep copy of the full config dict."""
        return copy.deepcopy(self._data)
