"""Tests for main.py CLI arg parsing and UI mode resolution."""

import sys
from unittest.mock import patch

import pytest

from pod_the_trader.main import _parse_cli_args, _resolve_ui_mode


class TestParseCliArgs:
    def test_no_args(self) -> None:
        assert _parse_cli_args([]) == (None, "auto", None)

    def test_config_path_positional(self) -> None:
        assert _parse_cli_args(["/path/to/config.yaml"]) == (
            "/path/to/config.yaml",
            "auto",
            None,
        )

    def test_tui_flag(self) -> None:
        assert _parse_cli_args(["--tui"]) == (None, "tui", None)

    def test_cli_flag(self) -> None:
        assert _parse_cli_args(["--cli"]) == (None, "cli", None)

    def test_flag_and_config(self) -> None:
        assert _parse_cli_args(["--tui", "config.yaml"]) == (
            "config.yaml",
            "tui",
            None,
        )

    def test_config_then_flag(self) -> None:
        assert _parse_cli_args(["config.yaml", "--cli"]) == (
            "config.yaml",
            "cli",
            None,
        )

    def test_base_domain_space_form(self) -> None:
        assert _parse_cli_args(["--base-domain", "usepod.ai"]) == (
            None,
            "auto",
            "usepod.ai",
        )

    def test_base_domain_equals_form(self) -> None:
        assert _parse_cli_args(["--base-domain=usepod.ai"]) == (
            None,
            "auto",
            "usepod.ai",
        )

    def test_base_domain_with_config_and_mode(self) -> None:
        assert _parse_cli_args(["config.yaml", "--cli", "--base-domain", "usepod.ai"]) == (
            "config.yaml",
            "cli",
            "usepod.ai",
        )

    def test_base_domain_missing_value_raises(self) -> None:
        with pytest.raises(SystemExit):
            _parse_cli_args(["--base-domain"])


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
