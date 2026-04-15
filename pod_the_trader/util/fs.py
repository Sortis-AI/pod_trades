"""Filesystem helpers that paper over POSIX/Windows differences.

The bot stores two security-sensitive files — the Solana keypair and
the Level5 API token — and has historically relied on ``chmod 0o600``
to keep them readable only by the owning user. On Windows ``os.chmod``
only flips the read-only bit; it does not touch NTFS ACLs, so a naive
port would leave these files world-readable. ``restrict_to_owner``
papers over this by shelling out to ``icacls`` on Windows (ships with
every supported Windows version, no extra dependency) to reset the ACL
and grant only the current user full control.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def restrict_to_owner(path: Path) -> None:
    """Restrict *path* so only the current user can read or write it.

    On POSIX this is ``chmod 0o600``. On Windows this runs
    ``icacls <path> /inheritance:r /grant:r <user>:F`` which strips
    inherited ACEs and grants the current user exclusive full control.

    Failures are logged at WARNING level but never raised: the call
    sites previously used best-effort ``os.chmod`` and we preserve that
    contract so a broken ACL doesn't prevent the bot from starting. The
    warning is loud enough that an operator reviewing logs will see a
    file-permission problem immediately.
    """
    if sys.platform == "win32":
        _restrict_windows(path)
    else:
        _restrict_posix(path)


def _restrict_posix(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning("Could not chmod %s to 0o600: %s", path, e)


def _restrict_windows(path: Path) -> None:
    user = os.environ.get("USERNAME") or _fallback_getlogin()
    if not user:
        logger.warning(
            "Could not determine current Windows user; leaving %s with default ACL. "
            "The file may be readable by other local users.",
            path,
        )
        return

    try:
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{user}:F",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        logger.warning(
            "icacls not found on PATH; cannot restrict ACL on %s. "
            "The file may be readable by other local users.",
            path,
        )
    except subprocess.CalledProcessError as e:
        logger.warning(
            "icacls failed for %s (exit %d): %s",
            path,
            e.returncode,
            (e.stderr or e.stdout or "").strip(),
        )


def _fallback_getlogin() -> str | None:
    try:
        return os.getlogin()
    except OSError:
        return None
