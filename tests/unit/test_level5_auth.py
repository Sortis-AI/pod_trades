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
        # Default provider must be Level5 so credentials predating
        # provider support still resolve to a working configuration.
        assert loaded.provider == "level5"

    def test_provider_round_trips(self, auth: Level5Auth) -> None:
        creds = Level5Credentials(api_token="tok", provider="usepod")
        auth.save(creds)
        loaded = auth.load()
        assert loaded is not None
        assert loaded.provider == "usepod"

    def test_legacy_file_without_provider_loads_as_level5(self, auth: Level5Auth) -> None:
        # Simulate a credentials file written before the provider field
        # existed. The dataclass default must fill in for the missing key.
        import json

        auth._storage_dir.mkdir(parents=True, exist_ok=True)
        auth._creds_path.write_text(
            json.dumps(
                {
                    "api_token": "legacy_token",
                    "deposit_address": "Addr",
                    "is_new": False,
                    # Note: no "provider" key.
                }
            )
        )
        loaded = auth.load()
        assert loaded is not None
        assert loaded.api_token == "legacy_token"
        assert loaded.provider == "level5"

    def test_load_drops_unknown_keys(self, auth: Level5Auth) -> None:
        # The known-set filter must still drop foreign keys so a
        # forward-looking file (e.g. one written by a future version)
        # still loads without raising TypeError on __init__.
        import json

        auth._storage_dir.mkdir(parents=True, exist_ok=True)
        auth._creds_path.write_text(
            json.dumps(
                {
                    "api_token": "tok",
                    "provider": "level5",
                    "future_field": "ignored",
                }
            )
        )
        loaded = auth.load()
        assert loaded is not None
        assert loaded.api_token == "tok"
