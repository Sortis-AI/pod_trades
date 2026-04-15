"""Level5 credential storage and interactive auth setup."""

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from pod_the_trader.util.fs import restrict_to_owner

logger = logging.getLogger(__name__)


@dataclass
class Level5Credentials:
    """Stored Level5 authentication state.

    ``deposit_address`` is the sovereign contract address where USDC
    lands; ``deposit_code`` is the per-account identifier the operator
    uses to route a deposit to the right account (provided by Level5's
    /v1/register response under ``instructions.contract_address`` and
    ``deposit_code`` respectively). ``dashboard_url`` is stored so the
    TUI can link to it even after the initial setup flow finishes.
    """

    api_token: str
    deposit_address: str | None = None
    deposit_code: str | None = None
    dashboard_url: str | None = None
    is_new: bool = False


class Level5Auth:
    """Manages Level5 credential persistence."""

    def __init__(self, storage_dir: str = "~/.pod_the_trader") -> None:
        self._storage_dir = Path(storage_dir).expanduser()
        self._creds_path = self._storage_dir / "level5_credentials.json"

    def save(self, creds: Level5Credentials) -> None:
        """Write credentials to disk with restricted permissions."""
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._creds_path.write_text(json.dumps(asdict(creds)))
        restrict_to_owner(self._creds_path)
        logger.debug("Saved Level5 credentials to %s", self._creds_path)

    def load(self) -> Level5Credentials | None:
        """Load credentials from disk. Returns None if not found.

        Tolerant of older credential files that predate the
        ``deposit_code`` / ``dashboard_url`` fields: unknown keys are
        dropped and missing keys fall back to their dataclass defaults.
        """
        if not self._creds_path.is_file():
            return None
        try:
            data = json.loads(self._creds_path.read_text())
            known = {
                "api_token",
                "deposit_address",
                "deposit_code",
                "dashboard_url",
                "is_new",
            }
            filtered = {k: v for k, v in data.items() if k in known}
            return Level5Credentials(**filtered)
        except Exception as e:
            logger.warning("Failed to load Level5 credentials: %s", e)
            return None

    def delete(self) -> None:
        """Remove stored credentials."""
        if self._creds_path.is_file():
            self._creds_path.unlink()
            logger.info("Deleted Level5 credentials")

    def has_credentials(self) -> bool:
        """Check if credentials exist on disk."""
        return self._creds_path.is_file()

    def setup_interactive(self) -> Level5Credentials | None:
        """Run interactive setup or read from environment.

        Returns credentials on success, None on skip/cancel.
        """
        env_token = os.environ.get("LEVEL5_API_TOKEN")
        if env_token:
            logger.info("Using Level5 API token from environment")
            creds = Level5Credentials(api_token=env_token)
            self.save(creds)
            return creds

        existing = self.load()
        if existing:
            logger.info("Using existing Level5 credentials")
            return existing

        return self._interactive_menu()

    def _interactive_menu(self) -> Level5Credentials | None:
        print("\n=== Level5 Setup ===")
        print("1. Register a new Level5 account")
        print("2. Enter an existing API token")
        print("3. Skip (you can set LEVEL5_API_TOKEN later)")

        choice = input("\nSelect an option (1-3): ").strip()

        if choice == "1":
            return Level5Credentials(api_token="", is_new=True)

        if choice == "2":
            token = input("Enter your Level5 API token: ").strip()
            if not token:
                print("No token provided. Cancelled.")
                return None
            creds = Level5Credentials(api_token=token)
            self.save(creds)
            print("Level5 credentials saved.")
            return creds

        print("Level5 setup skipped.")
        return None
