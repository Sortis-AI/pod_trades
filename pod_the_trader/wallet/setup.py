"""Interactive wallet setup wizard."""

import logging
import os

from solders.keypair import Keypair

from pod_the_trader.wallet.manager import WalletManager

logger = logging.getLogger(__name__)


class WalletSetup:
    """Guides the user through wallet creation or import."""

    def __init__(self, wallet_manager: WalletManager) -> None:
        self._manager = wallet_manager

    def run(self) -> Keypair | None:
        """Run the setup flow. Returns a Keypair on success, None on cancel.

        If SOLANA_PRIVATE_KEY is set, imports non-interactively.
        """
        env_key = os.environ.get("SOLANA_PRIVATE_KEY")
        if env_key:
            logger.info("Importing wallet from SOLANA_PRIVATE_KEY environment variable")
            info = self._manager.import_key(env_key)
            return info.keypair

        existing = self._manager.load()
        if existing:
            # Wallet address is shown in the main startup banner — don't
            # double-print here.
            logger.debug("Loaded existing wallet: %s", existing.address)
            return existing.keypair

        return self._interactive_setup()

    def _interactive_setup(self) -> Keypair | None:
        print("\n=== Wallet Setup ===")
        print("1. Generate a new wallet")
        print("2. Import an existing private key")
        print("3. Cancel")

        choice = input("\nSelect an option (1-3): ").strip()

        if choice == "1":
            info = self._manager.generate()
            print(f"New wallet generated: {info.address}")
            print("IMPORTANT: Fund this wallet with SOL before proceeding.")
            return info.keypair

        if choice == "2":
            key_str = input("Enter your private key (base58/base64/hex): ").strip()
            if not key_str:
                print("No key provided. Cancelled.")
                return None
            info = self._manager.import_key(key_str)
            print(f"Wallet imported: {info.address}")
            return info.keypair

        print("Wallet setup cancelled.")
        return None
