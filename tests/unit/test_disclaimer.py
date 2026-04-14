"""Tests for the startup disclaimer."""

from __future__ import annotations

import io

import pytest

from pod_the_trader.disclaimer import ACCEPTANCE_PHRASE, require_acceptance


class TestRequireAcceptance:
    def test_exact_phrase_passes_through(self) -> None:
        out = io.StringIO()
        inp = io.StringIO("I ACCEPT\n")
        # Should return without raising SystemExit.
        require_acceptance(stream_in=inp, stream_out=out)
        rendered = out.getvalue()
        assert "POD THE TRADER — DISCLAIMER" in rendered
        assert "REAL FUNDS" in rendered

    def test_lowercase_is_accepted(self) -> None:
        out = io.StringIO()
        inp = io.StringIO("i accept\n")
        # Acceptance is case-insensitive.
        require_acceptance(stream_in=inp, stream_out=out)

    def test_whitespace_tolerated(self) -> None:
        out = io.StringIO()
        inp = io.StringIO("   I ACCEPT   \n")
        require_acceptance(stream_in=inp, stream_out=out)

    def test_wrong_phrase_exits_cleanly(self) -> None:
        out = io.StringIO()
        inp = io.StringIO("yes\n")
        with pytest.raises(SystemExit) as exc:
            require_acceptance(stream_in=inp, stream_out=out)
        assert exc.value.code == 0
        assert "declined" in out.getvalue().lower()

    def test_empty_input_exits_cleanly(self) -> None:
        out = io.StringIO()
        inp = io.StringIO("")  # EOF immediately
        with pytest.raises(SystemExit) as exc:
            require_acceptance(stream_in=inp, stream_out=out)
        assert exc.value.code == 0
        assert "no input" in out.getvalue().lower()

    def test_y_shortcut_is_rejected(self) -> None:
        """No shortcuts — user must type the full phrase every time."""
        out = io.StringIO()
        inp = io.StringIO("y\n")
        with pytest.raises(SystemExit):
            require_acceptance(stream_in=inp, stream_out=out)

    def test_partial_phrase_rejected(self) -> None:
        out = io.StringIO()
        inp = io.StringIO("I\n")
        with pytest.raises(SystemExit):
            require_acceptance(stream_in=inp, stream_out=out)

    def test_phrase_with_extra_text_rejected(self) -> None:
        # "I ACCEPT the terms" should not match — must be exact.
        out = io.StringIO()
        inp = io.StringIO("I ACCEPT the terms\n")
        with pytest.raises(SystemExit):
            require_acceptance(stream_in=inp, stream_out=out)

    def test_disclaimer_text_includes_required_sections(self) -> None:
        out = io.StringIO()
        inp = io.StringIO("I ACCEPT\n")
        require_acceptance(stream_in=inp, stream_out=out)
        text = out.getvalue()
        # Every numbered risk section the draft included.
        for needle in (
            "REAL FUNDS",
            "LLM-DRIVEN DECISIONS",
            "NO WARRANTY",
            "YOU ARE THE OPERATOR",
            "NOT FINANCIAL ADVICE",
            "KEY CUSTODY",
            "NO RECOURSE",
        ):
            assert needle in text, f"disclaimer missing section: {needle}"

    def test_acceptance_phrase_constant_matches_prompt(self) -> None:
        # Guardrail: the prompt text should reference the same phrase the
        # code actually checks for.
        assert ACCEPTANCE_PHRASE == "I ACCEPT"
