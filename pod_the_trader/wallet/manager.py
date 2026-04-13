"""Wallet lifecycle: generate, import, load, save."""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import base58
from solders.keypair import Keypair

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WalletInfo:
    """Loaded wallet with address and keypair."""

    address: str
    keypair: Keypair


class WalletError(Exception):
    """Raised on wallet operations failure."""


class WalletManager:
    """Manages Solana keypair storage and retrieval."""

    def __init__(self, storage_dir: str = "~/.pod_the_trader") -> None:
        self._storage_dir = Path(storage_dir).expanduser()
        self._keypair_path = self._storage_dir / "keypair.json"

    def exists(self) -> bool:
        """Check whether a saved keypair exists on disk."""
        return self._keypair_path.is_file()

    def generate(self) -> WalletInfo:
        """Generate a new random keypair, save it, and return WalletInfo."""
        keypair = Keypair()
        self.save(keypair)
        info = WalletInfo(address=str(keypair.pubkey()), keypair=keypair)
        logger.info("Generated new wallet: %s", info.address)
        return info

    def load(self) -> WalletInfo | None:
        """Load a keypair from disk. Returns None if no file exists."""
        if not self._keypair_path.is_file():
            return None
        try:
            raw = json.loads(self._keypair_path.read_text())
            keypair = Keypair.from_bytes(bytes(raw))
            return WalletInfo(address=str(keypair.pubkey()), keypair=keypair)
        except Exception as e:
            raise WalletError(f"Failed to load keypair from {self._keypair_path}: {e}") from e

    def save(self, keypair: Keypair) -> None:
        """Persist a keypair to disk with restricted permissions."""
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        raw = list(bytes(keypair))
        self._keypair_path.write_text(json.dumps(raw))
        os.chmod(self._keypair_path, 0o600)
        logger.debug("Saved keypair to %s", self._keypair_path)

    def import_key(self, key_string: str) -> WalletInfo:
        """Import a private key from a base58, base64, or hex string.

        Accepts 32-byte seeds or 64-byte full keypairs.
        """
        key_string = key_string.strip()
        decoded = self._decode_key(key_string)

        if len(decoded) == 32:
            keypair = Keypair.from_seed(decoded)
        elif len(decoded) == 64:
            keypair = Keypair.from_bytes(decoded)
        else:
            raise WalletError(
                f"Invalid key length: {len(decoded)} bytes. "
                "Expected 32 (seed) or 64 (full keypair)."
            )

        self.save(keypair)
        info = WalletInfo(address=str(keypair.pubkey()), keypair=keypair)
        logger.info("Imported wallet: %s", info.address)
        return info

    def _decode_key(self, key_string: str) -> bytes:
        """Try base58, then base64, then hex decoding."""
        # Try base58
        try:
            decoded = base58.b58decode(key_string)
            if len(decoded) in (32, 64):
                return decoded
        except Exception:
            pass

        # Try base64
        import base64

        try:
            decoded = base64.b64decode(key_string, validate=True)
            if len(decoded) in (32, 64):
                return decoded
        except Exception:
            pass

        # Try hex
        try:
            decoded = bytes.fromhex(key_string)
            if len(decoded) in (32, 64):
                return decoded
        except Exception:
            pass

        raise WalletError(
            "Could not decode key. Provide a valid base58, base64, or hex-encoded "
            "private key (32 or 64 bytes)."
        )
