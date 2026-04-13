"""Shared test fixtures."""

import json
from pathlib import Path

import pytest
import yaml
from solders.keypair import Keypair

from pod_the_trader.config import Config

# A well-known SPL token mint for testing (Wrapped SOL)
TEST_TOKEN_ADDRESS = "So11111111111111111111111111111111111111112"


@pytest.fixture()
def tmp_storage(tmp_path: Path) -> Path:
    """Temporary storage directory replacing ~/.pod_the_trader."""
    storage = tmp_path / ".pod_the_trader"
    storage.mkdir()
    return storage


@pytest.fixture()
def sample_config(tmp_path: Path) -> Config:
    """Config instance with test-safe defaults."""
    config_file = tmp_path / "test_config.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "trading": {"target_token_address": TEST_TOKEN_ADDRESS},
                "solana": {"rpc_url": "https://api.devnet.solana.com"},
            }
        )
    )
    return Config(str(config_file))


@pytest.fixture()
def mock_keypair() -> Keypair:
    """A deterministic Solana Keypair for testing."""
    seed = bytes(range(32))
    return Keypair.from_seed(seed)


@pytest.fixture()
def sample_trade_history(tmp_storage: Path) -> Path:
    """Pre-populated trade_history.json for PnL tests."""
    history = [
        {
            "timestamp": "2026-04-01T10:00:00Z",
            "side": "buy",
            "input_mint": "So11111111111111111111111111111111111111112",
            "output_mint": TEST_TOKEN_ADDRESS,
            "input_amount": 1.0,
            "output_amount": 100.0,
            "price_usd": 0.15,
            "value_usd": 15.0,
            "signature": "fake_sig_1",
        },
        {
            "timestamp": "2026-04-02T10:00:00Z",
            "side": "sell",
            "input_mint": TEST_TOKEN_ADDRESS,
            "output_mint": "So11111111111111111111111111111111111111112",
            "input_amount": 100.0,
            "output_amount": 1.2,
            "price_usd": 0.18,
            "value_usd": 18.0,
            "signature": "fake_sig_2",
        },
    ]
    history_file = tmp_storage / "trade_history.json"
    history_file.write_text(json.dumps(history))
    return history_file
