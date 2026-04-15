"""Tests for pod_the_trader.util.fs.restrict_to_owner."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from pod_the_trader.util import fs


@pytest.fixture()
def tmp_file(tmp_path: Path) -> Path:
    p = tmp_path / "secret.json"
    p.write_text("{}")
    return p


class TestPosixBranch:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
    def test_chmods_to_0600(self, tmp_file: Path) -> None:
        fs.restrict_to_owner(tmp_file)
        mode = os.stat(tmp_file).st_mode & 0o777
        assert mode == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only")
    def test_logs_warning_but_does_not_raise_on_missing_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # chmod on a path that doesn't exist should warn, not raise.
        missing = tmp_path / "nope.json"
        with caplog.at_level("WARNING"):
            fs.restrict_to_owner(missing)
        assert any("chmod" in r.message for r in caplog.records)


class TestWindowsBranch:
    """The Windows branch is exercised on every platform via mocks — we
    never actually shell out to icacls in the test suite.
    """

    def test_invokes_icacls_with_current_user(self, tmp_file: Path) -> None:
        with (
            patch.object(fs.sys, "platform", "win32"),
            patch.dict(os.environ, {"USERNAME": "alice"}, clear=False),
            patch.object(fs.subprocess, "run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            fs.restrict_to_owner(tmp_file)

        assert mock_run.call_count == 1
        call_args = mock_run.call_args
        cmd = call_args.args[0]
        assert cmd[0] == "icacls"
        assert cmd[1] == str(tmp_file)
        assert "/inheritance:r" in cmd
        assert "/grant:r" in cmd
        assert "alice:F" in cmd
        assert call_args.kwargs.get("check") is True

    def test_warns_when_username_missing(
        self, tmp_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch.object(fs.sys, "platform", "win32"),
            patch.dict(os.environ, {}, clear=True),
            patch.object(fs, "_fallback_getlogin", return_value=None),
            patch.object(fs.subprocess, "run") as mock_run,
            caplog.at_level("WARNING"),
        ):
            fs.restrict_to_owner(tmp_file)

        mock_run.assert_not_called()
        assert any("Windows user" in r.message for r in caplog.records)

    def test_warns_when_icacls_missing(
        self, tmp_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch.object(fs.sys, "platform", "win32"),
            patch.dict(os.environ, {"USERNAME": "alice"}, clear=False),
            patch.object(fs.subprocess, "run", side_effect=FileNotFoundError),
            caplog.at_level("WARNING"),
        ):
            fs.restrict_to_owner(tmp_file)

        assert any("icacls not found" in r.message for r in caplog.records)

    def test_warns_when_icacls_fails(
        self, tmp_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with (
            patch.object(fs.sys, "platform", "win32"),
            patch.dict(os.environ, {"USERNAME": "alice"}, clear=False),
            patch.object(
                fs.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(
                    returncode=5, cmd=["icacls"], output="", stderr="access denied"
                ),
            ),
            caplog.at_level("WARNING"),
        ):
            fs.restrict_to_owner(tmp_file)

        assert any("icacls failed" in r.message for r in caplog.records)
