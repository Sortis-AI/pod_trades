"""Interactive wallet setup wizard."""

import logging
import os

import base58
from solders.keypair import Keypair

from pod_the_trader.wallet.manager import WalletManager

logger = logging.getLogger(__name__)

_BACKUP_CONFIRMATION = "I SAVED IT"


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
            print(f"\nNew wallet generated: {info.address}")
            self._print_backup_and_confirm(info.keypair)
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

    def _print_backup_and_confirm(self, keypair: Keypair) -> None:
        """Print the private key and block until the user confirms backup.

        This runs exactly once, immediately after a fresh wallet is
        generated. The user will never see this key again — it is not
        recoverable from the keypair.json file in a format Phantom or
        Solflare can import without manual conversion, and the seed
        phrase does not exist (solders keypairs are random bytes, not
        BIP-39 derived). So this prompt is the one and only chance to
        capture it.

        The format is base58(bytes(keypair)) — a 64-byte (32-byte
        secret + 32-byte pubkey) base58 string, which is what
        Phantom's "Import Private Key" field accepts directly.
        """
        private_key_b58 = base58.b58encode(bytes(keypair)).decode()

        bar = "=" * 68
        print()
        print(bar)
        print("  BACK UP YOUR WALLET PRIVATE KEY — DO THIS NOW")
        print(bar)
        print()
        print("  This is the ONLY time this key will be displayed.")
        print("  If you lose it, the funds in this wallet are gone forever.")
        print("  Anyone who has it can drain the wallet.")
        print()
        print("  Private key (base58, Phantom/Solflare compatible):")
        print()
        print(f"    {private_key_b58}")
        print()
        print("  How to back it up:")
        print("    1. Copy the string above into a password manager")
        print("       (1Password, Bitwarden, etc.), OR")
        print("    2. Write it down on paper and store it somewhere safe.")
        print()
        print("  Do NOT:")
        print("    - Paste it into chat, email, or a screenshot")
        print("    - Store it in plain text on a shared machine")
        print("    - Share it with anyone, including support staff")
        print()
        print(bar)
        print(f'  Type exactly "{_BACKUP_CONFIRMATION}" once you have saved it.')
        print(bar)

        while True:
            response = input("> ").strip()
            if response == _BACKUP_CONFIRMATION:
                print("Backup confirmed.\n")
                return
            print(
                f'Please type exactly "{_BACKUP_CONFIRMATION}" '
                "(case-sensitive, no quotes) to continue."
            )
