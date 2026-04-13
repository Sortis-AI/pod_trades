"""Tests for main.py CLI arg parsing and UI mode resolution."""

import sys
from unittest.mock import patch

from pod_the_trader.main import _parse_cli_args, _resolve_ui_mode


class TestParseCliArgs:
    def test_no_args(self) -> None:
        assert _parse_cli_args([]) == (None, "auto")

    def test_config_path_positional(self) -> None:
        assert _parse_cli_args(["/path/to/config.yaml"]) == (
            "/path/to/config.yaml",
            "auto",
        )

    def test_tui_flag(self) -> None:
        assert _parse_cli_args(["--tui"]) == (None, "tui")

    def test_cli_flag(self) -> None:
        assert _parse_cli_args(["--cli"]) == (None, "cli")

    def test_flag_and_config(self) -> None:
        assert _parse_cli_args(["--tui", "config.yaml"]) == (
            "config.yaml",
            "tui",
        )

    def test_config_then_flag(self) -> None:
        assert _parse_cli_args(["config.yaml", "--cli"]) == (
            "config.yaml",
            "cli",
        )


class TestResolveUiMode:
    def test_explicit_tui(self) -> None:
        assert _resolve_ui_mode("tui") == "tui"

    def test_explicit_cli(self) -> None:
        assert _resolve_ui_mode("cli") == "cli"

    def test_auto_with_tty(self) -> None:
        with patch.object(sys.stdout, "isatty", return_value=True):
            assert _resolve_ui_mode("auto") == "tui"

    def test_auto_without_tty(self) -> None:
        with patch.object(sys.stdout, "isatty", return_value=False):
            assert _resolve_ui_mode("auto") == "cli"
