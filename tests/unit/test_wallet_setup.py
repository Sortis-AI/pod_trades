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
        # Inputs: "1" selects generate, then "I SAVED IT" passes backup gate.
        with patch("builtins.input", side_effect=["1", "I SAVED IT"]):
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


class TestBackupPrompt:
    """The backup gate is the user's ONLY chance to capture the private
    key. These tests lock in that it actually runs on generation, prints
    a Phantom/Solflare-compatible base58 key, and loops until the
    confirmation phrase is typed exactly.
    """

    def test_prints_private_key_on_generate(
        self, setup: WalletSetup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import base58

        with patch("builtins.input", side_effect=["1", "I SAVED IT"]):
            kp = setup.run()
        assert kp is not None
        expected_b58 = base58.b58encode(bytes(kp)).decode()
        out = capsys.readouterr().out
        assert expected_b58 in out
        assert "BACK UP YOUR WALLET PRIVATE KEY" in out
        assert "ONLY time this key will be displayed" in out

    def test_loops_until_correct_confirmation(
        self, setup: WalletSetup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Generate, then two wrong tries, then the right phrase.
        with patch(
            "builtins.input",
            side_effect=["1", "ok", "i saved it", "I SAVED IT"],
        ):
            kp = setup.run()
        assert kp is not None
        out = capsys.readouterr().out
        # The "please type exactly" reprompt must have appeared (for each
        # wrong attempt).
        assert out.count('Please type exactly "I SAVED IT"') == 2

    def test_does_not_prompt_on_load_existing(
        self,
        setup: WalletSetup,
        manager: WalletManager,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Pre-existing wallet on disk: setup.run() must not print the
        # backup gate at all — it only applies to fresh generation.
        manager.generate()
        capsys.readouterr()  # drain any noise
        kp = setup.run()
        assert kp is not None
        out = capsys.readouterr().out
        assert "BACK UP YOUR WALLET PRIVATE KEY" not in out

    def test_does_not_prompt_on_import(
        self, setup: WalletSetup, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Imported key — the user already has it, so no need to re-display.
        import base58

        seed = bytes(range(32))
        encoded = base58.b58encode(seed).decode()
        with patch("builtins.input", side_effect=["2", encoded]):
            kp = setup.run()
        assert kp is not None
        out = capsys.readouterr().out
        assert "BACK UP YOUR WALLET PRIVATE KEY" not in out

    def test_does_not_prompt_on_env_import(
        self,
        setup: WalletSetup,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import base58

        seed = bytes(range(32))
        encoded = base58.b58encode(seed).decode()
        monkeypatch.setenv("SOLANA_PRIVATE_KEY", encoded)
        kp = setup.run()
        assert kp is not None
        out = capsys.readouterr().out
        assert "BACK UP YOUR WALLET PRIVATE KEY" not in out
