"""Tests for pod_the_trader.wallet.manager."""

import base64
import json
import os
from pathlib import Path

import base58
import pytest
from solders.keypair import Keypair

from pod_the_trader.wallet.manager import WalletError, WalletInfo, WalletManager


@pytest.fixture()
def manager(tmp_path: Path) -> WalletManager:
    return WalletManager(storage_dir=str(tmp_path))


class TestGenerate:
    def test_creates_and_saves_keypair(self, manager: WalletManager) -> None:
        info = manager.generate()
        assert isinstance(info, WalletInfo)
        assert isinstance(info.keypair, Keypair)
        assert len(info.address) > 0
        assert manager.exists()

    def test_file_has_correct_permissions(self, manager: WalletManager) -> None:
        manager.generate()
        keypair_path = manager._keypair_path
        mode = os.stat(keypair_path).st_mode & 0o777
        assert mode == 0o600


class TestLoadAndSave:
    def test_roundtrip(self, manager: WalletManager) -> None:
        original = manager.generate()
        loaded = manager.load()
        assert loaded is not None
        assert loaded.address == original.address
        assert bytes(loaded.keypair) == bytes(original.keypair)

    def test_load_returns_none_when_no_file(self, manager: WalletManager) -> None:
        assert manager.load() is None

    def test_exists_reflects_file_state(self, manager: WalletManager) -> None:
        assert not manager.exists()
        manager.generate()
        assert manager.exists()

    def test_save_creates_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested"
        mgr = WalletManager(storage_dir=str(nested))
        keypair = Keypair()
        mgr.save(keypair)
        assert mgr.exists()

    def test_saved_format_is_byte_list(self, manager: WalletManager) -> None:
        keypair = Keypair()
        manager.save(keypair)
        raw = json.loads(manager._keypair_path.read_text())
        assert isinstance(raw, list)
        assert len(raw) == 64
        assert all(isinstance(b, int) and 0 <= b <= 255 for b in raw)


class TestImportKey:
    def _seed(self) -> bytes:
        return bytes(range(32))

    def _full_keypair_bytes(self) -> bytes:
        kp = Keypair.from_seed(self._seed())
        return bytes(kp)

    def test_import_base58_seed(self, manager: WalletManager) -> None:
        encoded = base58.b58encode(self._seed()).decode()
        info = manager.import_key(encoded)
        expected = Keypair.from_seed(self._seed())
        assert info.address == str(expected.pubkey())

    def test_import_base64_seed(self, manager: WalletManager) -> None:
        encoded = base64.b64encode(self._seed()).decode()
        info = manager.import_key(encoded)
        expected = Keypair.from_seed(self._seed())
        assert info.address == str(expected.pubkey())

    def test_import_hex_seed(self, manager: WalletManager) -> None:
        encoded = self._seed().hex()
        info = manager.import_key(encoded)
        expected = Keypair.from_seed(self._seed())
        assert info.address == str(expected.pubkey())

    def test_import_base58_full_keypair(self, manager: WalletManager) -> None:
        full = self._full_keypair_bytes()
        encoded = base58.b58encode(full).decode()
        info = manager.import_key(encoded)
        expected = Keypair.from_bytes(full)
        assert info.address == str(expected.pubkey())

    def test_import_base64_full_keypair(self, manager: WalletManager) -> None:
        full = self._full_keypair_bytes()
        encoded = base64.b64encode(full).decode()
        info = manager.import_key(encoded)
        expected = Keypair.from_bytes(full)
        assert info.address == str(expected.pubkey())

    def test_import_hex_full_keypair(self, manager: WalletManager) -> None:
        full = self._full_keypair_bytes()
        encoded = full.hex()
        info = manager.import_key(encoded)
        expected = Keypair.from_bytes(full)
        assert info.address == str(expected.pubkey())

    def test_rejects_invalid_encoding(self, manager: WalletManager) -> None:
        with pytest.raises(WalletError, match="Could not decode"):
            manager.import_key("not-a-valid-key-at-all!!!")

    def test_rejects_wrong_length(self, manager: WalletManager) -> None:
        bad = base58.b58encode(bytes(16)).decode()
        with pytest.raises(WalletError, match="Could not decode"):
            manager.import_key(bad)

    def test_strips_whitespace(self, manager: WalletManager) -> None:
        encoded = base58.b58encode(self._seed()).decode()
        info = manager.import_key(f"  {encoded}  \n")
        expected = Keypair.from_seed(self._seed())
        assert info.address == str(expected.pubkey())
