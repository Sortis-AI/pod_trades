"""Tests for pod_the_trader.level5.auth."""

import os
import sys
from pathlib import Path

import pytest

from pod_the_trader.level5.auth import Level5Auth, Level5Credentials


@pytest.fixture()
def auth(tmp_path: Path) -> Level5Auth:
    return Level5Auth(storage_dir=str(tmp_path))


class TestCredentialPersistence:
    def test_save_and_load(self, auth: Level5Auth) -> None:
        creds = Level5Credentials(
            api_token="test_token_123",
            deposit_address="SomeDepositAddress",
            is_new=False,
        )
        auth.save(creds)
        loaded = auth.load()
        assert loaded is not None
        assert loaded.api_token == "test_token_123"
        assert loaded.deposit_address == "SomeDepositAddress"
        assert loaded.is_new is False

    def test_load_returns_none_when_no_file(self, auth: Level5Auth) -> None:
        assert auth.load() is None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX mode bits are not enforceable on NTFS; Windows uses icacls instead",
    )
    def test_save_sets_file_permissions(self, auth: Level5Auth) -> None:
        creds = Level5Credentials(api_token="tok")
        auth.save(creds)
        mode = os.stat(auth._creds_path).st_mode & 0o777
        assert mode == 0o600

    def test_delete_removes_file(self, auth: Level5Auth) -> None:
        creds = Level5Credentials(api_token="tok")
        auth.save(creds)
        assert auth.has_credentials()
        auth.delete()
        assert not auth.has_credentials()

    def test_has_credentials_reflects_state(self, auth: Level5Auth) -> None:
        assert not auth.has_credentials()
        auth.save(Level5Credentials(api_token="tok"))
        assert auth.has_credentials()

    def test_load_with_minimal_fields(self, auth: Level5Auth) -> None:
        creds = Level5Credentials(api_token="minimal")
        auth.save(creds)
        loaded = auth.load()
        assert loaded is not None
        assert loaded.api_token == "minimal"
        assert loaded.deposit_address is None
        assert loaded.is_new is False
