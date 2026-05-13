"""Provider credential storage and interactive auth setup.

The on-disk file is still named ``level5_credentials.json`` for
backwards compatibility — when pod-the-trader only spoke to Level5
that was the only credential shape. The ``provider`` field added to
``Level5Credentials`` discriminates between Level5 and UsePod (and
defaults to ``"level5"`` so legacy files load without rewriting).
"""

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from pod_the_trader.level5.provider import (
    PROVIDERS,
    Provider,
    ProviderConfig,
    resolve_provider,
)
from pod_the_trader.util.fs import restrict_to_owner

logger = logging.getLogger(__name__)


@dataclass
class Level5Credentials:
    """Stored provider authentication state.

    ``deposit_address`` is the sovereign contract address where USDC
    lands; ``deposit_code`` is the per-account identifier the operator
    uses to route a deposit to the right account (provided by the
    /v1/register response under ``instructions.contract_address`` and
    ``deposit_code`` respectively). ``dashboard_url`` is stored so the
    TUI can link to it even after the initial setup flow finishes.

    ``provider`` identifies which LLM-proxy provider these credentials
    target (``"level5"`` or ``"usepod"``). Defaults to ``"level5"`` so
    credential files written before provider support was added load
    correctly as Level5 accounts.
    """

    api_token: str
    deposit_address: str | None = None
    deposit_code: str | None = None
    dashboard_url: str | None = None
    is_new: bool = False
    provider: str = Provider.LEVEL5.value


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
                "provider",
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

    def setup_interactive(self, provider: ProviderConfig | None = None) -> Level5Credentials | None:
        """Run interactive setup or read from environment.

        ``provider`` is the default provider to offer in the wizard
        (and the one whose env-var override is consulted). If omitted,
        defaults to Level5 so existing call sites keep working.

        Returns credentials on success, None on skip/cancel.
        """
        active = provider or resolve_provider(None)

        env_var = f"{active.key.upper()}_API_TOKEN"
        env_token = os.environ.get(env_var)
        if env_token:
            logger.info("Using %s API token from %s", active.display_name, env_var)
            creds = Level5Credentials(api_token=env_token, provider=active.key)
            self.save(creds)
            return creds

        existing = self.load()
        if existing:
            logger.info(
                "Using existing credentials for %s",
                resolve_provider(existing.provider).display_name,
            )
            return existing

        return self._interactive_menu(active)

    def _prompt_provider(self, default: ProviderConfig) -> ProviderConfig:
        """First wizard step: pick a provider.

        The default is the active provider (from CLI/config). Pressing
        enter without a choice accepts the default.
        """
        ordered = [PROVIDERS[Provider.LEVEL5.value], PROVIDERS[Provider.USEPOD.value]]
        print("\n=== Choose an LLM-proxy provider ===")
        for idx, cfg in enumerate(ordered, start=1):
            marker = " (default)" if cfg.key == default.key else ""
            print(f"{idx}. {cfg.display_name}{marker}")
        choice = input(f"\nSelect (1-{len(ordered)}, default {default.display_name}): ").strip()
        if not choice:
            return default
        try:
            idx = int(choice)
        except ValueError:
            print(f"Unrecognized choice; using {default.display_name}.")
            return default
        if 1 <= idx <= len(ordered):
            return ordered[idx - 1]
        print(f"Out of range; using {default.display_name}.")
        return default

    def _interactive_menu(self, default_provider: ProviderConfig) -> Level5Credentials | None:
        chosen = self._prompt_provider(default_provider)

        print(f"\n=== {chosen.display_name} Setup ===")
        print(f"1. Register a new {chosen.display_name} account")
        print(f"2. Enter an existing {chosen.display_name} API token")
        env_var = f"{chosen.key.upper()}_API_TOKEN"
        print(f"3. Skip (you can set {env_var} later)")

        choice = input("\nSelect an option (1-3): ").strip()

        if choice == "1":
            return Level5Credentials(api_token="", is_new=True, provider=chosen.key)

        if choice == "2":
            token = input(f"Enter your {chosen.display_name} API token: ").strip()
            if not token:
                print("No token provided. Cancelled.")
                return None
            creds = Level5Credentials(api_token=token, provider=chosen.key)
            self.save(creds)
            print(f"{chosen.display_name} credentials saved.")
            return creds

        print(f"{chosen.display_name} setup skipped.")
        return None
