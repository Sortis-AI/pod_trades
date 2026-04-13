"""Tests for pod_the_trader.wallet.setup."""

from pathlib import Path
from unittest.mock import patch

import pytest
from solders.keypair import Keypair

from pod_the_trader.wallet.manager import WalletManager
from pod_the_trader.wallet.setup import WalletSetup


@pytest.fixture()
def manager(tmp_path: Path) -> WalletManager:
    return WalletManager(storage_dir=str(tmp_path))


@pytest.fixture()
def setup(manager: WalletManager) -> WalletSetup:
    return WalletSetup(manager)


class TestEnvVarImport:
    def test_imports_from_env(self, setup: WalletSetup, monkeypatch: pytest.MonkeyPatch) -> None:
        import base58

        seed = bytes(range(32))
        encoded = base58.b58encode(seed).decode()
        monkeypatch.setenv("SOLANA_PRIVATE_KEY", encoded)
        kp = setup.run()
        assert kp is not None
        expected = Keypair.from_seed(seed)
        assert str(kp.pubkey()) == str(expected.pubkey())


class TestExistingWallet:
    def test_returns_existing(self, setup: WalletSetup, manager: WalletManager) -> None:
        manager.generate()
        kp = setup.run()
        assert kp is not None


class TestInteractiveGenerate:
    def test_generate_option(self, setup: WalletSetup) -> None:
        with patch("builtins.input", return_value="1"):
            kp = setup.run()
        assert kp is not None

    def test_cancel_option(self, setup: WalletSetup) -> None:
        with patch("builtins.input", return_value="3"):
            kp = setup.run()
        assert kp is None

    def test_import_option(self, setup: WalletSetup) -> None:
        import base58

        seed = bytes(range(32))
        encoded = base58.b58encode(seed).decode()
        with patch("builtins.input", side_effect=["2", encoded]):
            kp = setup.run()
        assert kp is not None

    def test_import_empty_key_cancels(self, setup: WalletSetup) -> None:
        with patch("builtins.input", side_effect=["2", ""]):
            kp = setup.run()
        assert kp is None
