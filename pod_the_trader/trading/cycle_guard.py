"""Per-cycle cumulative slippage gate.

The model can fire multiple ``execute_swap`` calls inside a single LLM
turn — e.g. one $25 slice followed by $50 then $75 when the full $150
fails feasibility. Each market buy walks the AMM curve, so the second
and third slices fill at materially worse prices than the first. The
per-swap ``slippage_bps`` gate doesn't catch this: it only sees one
swap's quote-to-fill drift, not the cumulative price walk between
slices.

``CyclePriceGuard`` tracks the first successful swap's actual
``output/input`` ratio per ``(input_mint, output_mint)`` direction and
rejects any later swap in the same direction whose quoted ratio is
unfavorable beyond ``max_drift_pct``. The agent calls ``reset()`` at
the top of every cycle so the anchor doesn't survive across the 5-min
cooldown (the pool has time to mean-revert).
"""

from __future__ import annotations


class CyclePriceGuard:
    """Cumulative-slippage gate scoped to a single trading cycle."""

    def __init__(self, max_drift_pct: float) -> None:
        self._max_drift_pct = float(max_drift_pct)
        self._anchor: dict[tuple[str, str], float] = {}

    def reset(self) -> None:
        """Forget all anchors. Called at the top of each cycle."""
        self._anchor.clear()

    def check_quote(
        self,
        input_mint: str,
        output_mint: str,
        in_amount_raw: int,
        out_amount_raw: int,
    ) -> str | None:
        """Return None to allow the swap, or an error string to reject it.

        The comparison uses ``output/input`` so it's invariant to slice
        size: a quote that would yield 99.5 tokens per $1 is worse than
        one that would yield 100 tokens per $1, regardless of whether
        the slice is $25 or $75. Only drifts that hurt the trader (less
        output per input than the anchor) are blocked — a quote that's
        better than the anchor is always allowed.
        """
        if in_amount_raw <= 0 or out_amount_raw <= 0:
            return None
        key = (input_mint, output_mint)
        anchor = self._anchor.get(key)
        if anchor is None:
            return None
        new_ratio = out_amount_raw / in_amount_raw
        drift_pct = (new_ratio - anchor) / anchor * 100.0
        if drift_pct < -self._max_drift_pct:
            return (
                f"Cycle-cumulative slippage gate hit: this quote returns "
                f"{abs(drift_pct):.2f}% less output per input than the "
                f"first slice of this cycle (max allowed drift: "
                f"{self._max_drift_pct:.2f}%). The pool has moved "
                "against us inside this cycle — additional slicing "
                "would stack self-impact. Stop trading this cycle and "
                "wait for the next one so the AMM mean-reverts."
            )
        return None

    def record_fill(
        self,
        input_mint: str,
        output_mint: str,
        in_amount_raw: int,
        out_amount_raw: int,
    ) -> None:
        """Anchor the first successful fill's ratio for this direction.

        Subsequent calls in the same direction within the same cycle
        are no-ops — the anchor is only the FIRST slice. Resetting
        between cycles (via ``reset``) lets every cycle start fresh.
        """
        if in_amount_raw <= 0 or out_amount_raw <= 0:
            return
        key = (input_mint, output_mint)
        if key not in self._anchor:
            self._anchor[key] = out_amount_raw / in_amount_raw
