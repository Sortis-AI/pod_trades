"""Reconciliation: bridge the gap between the ledger and on-chain truth.

The ``LotLedger`` models what the bot *thinks* the wallet holds, using every
bot trade plus every prior reconciliation. This module compares that expected
state against the actual on-chain balance each cycle (and at startup) and
emits synthetic ``reconcile`` events to absorb any delta.

## How it handles each scenario

* **SOL or token deposited** — on-chain balance exceeds expected. Emit a
  single ``open`` lot at the current spot price. Subsequent trades match
  against this basis FIFO like any other open lot.

* **SOL or token withdrawn** — on-chain balance is below expected. Emit a
  ``close`` event at the current spot price. ``PositionState`` consumes open
  lots FIFO; because the close is sourced as ``reconcile`` (not ``trade``),
  no realized P&L is booked — the basis is simply retired. We don't try to
  interpret whether the outflow was a sale or a transfer; both remove basis.

* **External swap** — one mint drops, another rises. The reconciler runs
  per-mint, so an external swap naturally decomposes into one withdrawal
  event on the sold mint and one deposit event on the bought mint. Neither
  produces realized P&L (consistent with the rule above).

* **Bot running when change happens** — the reconciler runs at the start of
  every trade loop iteration, before the LLM turn. Any change in the cooldown
  window is booked before the model reasons about it, so the authoritative
  snapshot the model sees is always consistent with the lot ledger.

* **Change while offline** — the reconciler also runs once at startup,
  before the first cycle. The delta since the last persisted state is
  absorbed exactly the same way as an in-session drift.

## Pricing

Synthetic events use the current spot price at the moment of detection.
That's imprecise for events that happened minutes or hours earlier, but it's
conservative and avoids any dependency on on-chain transaction parsing. An
accuracy upgrade path (parse ``getSignaturesForAddress`` between snapshots
and use the real swap prices) can be added later without changing the event
model.

## Dust threshold

Sub-threshold deltas are ignored to avoid flutter from rent rebates, priority
fee refunds, and float noise. Default 1e-6 of a unit (i.e. one millionth of
a whole SOL or token — well below anything a user would care about).
"""

from __future__ import annotations

import logging

from pod_the_trader.data.lot_ledger import (
    KIND_CLOSE,
    KIND_OPEN,
    SOURCE_RECONCILE,
    LotEvent,
    LotLedger,
    now_iso,
)

logger = logging.getLogger(__name__)

DEFAULT_DUST = 1e-6


def reconcile_mint(
    ledger: LotLedger,
    *,
    mint: str,
    actual_qty: float,
    current_price_usd: float,
    timestamp: str | None = None,
    dust: float = DEFAULT_DUST,
    notes: str = "",
) -> LotEvent | None:
    """Reconcile one mint's on-chain balance against the ledger.

    Returns the synthetic event if one was appended, else ``None``.
    """
    state = ledger.position_state(mint)
    expected = state.open_qty
    delta = actual_qty - expected

    if abs(delta) <= dust:
        return None

    ts = timestamp or now_iso()
    if delta > 0:
        event = LotEvent(
            timestamp=ts,
            mint=mint,
            kind=KIND_OPEN,
            qty=delta,
            unit_price_usd=current_price_usd,
            source=SOURCE_RECONCILE,
            notes=notes or f"reconcile_in: observed +{delta:.6f} units",
        )
    else:
        # When the ledger has no basis yet (first run, wallet already held
        # tokens before tracking started) we can't truly "close" anything.
        # Guard here so we don't produce an unmatched close on a zero-basis
        # ledger — that's not a withdrawal, it's a missing-basis problem.
        drop = -delta
        if expected <= 0:
            logger.warning(
                "Reconcile wants to close %.6f %s but ledger has no open "
                "basis; skipping close (actual balance is lower than any "
                "basis the ledger tracks). This usually indicates the "
                "wallet held tokens before the ledger existed.",
                drop,
                mint[:8],
            )
            return None
        event = LotEvent(
            timestamp=ts,
            mint=mint,
            kind=KIND_CLOSE,
            qty=drop,
            unit_price_usd=current_price_usd,
            source=SOURCE_RECONCILE,
            notes=notes or f"reconcile_out: observed -{drop:.6f} units",
        )

    ledger.append(event)
    logger.info(
        "Reconciled %s: expected %.6f, actual %.6f, delta %+.6f @ $%.8f",
        mint[:8],
        expected,
        actual_qty,
        delta,
        current_price_usd,
    )
    return event


def reconcile_portfolio(
    ledger: LotLedger,
    *,
    sol_mint: str,
    sol_balance: float,
    sol_price_usd: float,
    token_mint: str,
    token_balance: float,
    token_price_usd: float,
    usdc_mint: str = "",
    usdc_balance: float = 0.0,
    usdc_price_usd: float = 0.0,
    timestamp: str | None = None,
    dust_sol: float = 1e-6,
    dust_token: float = 1e-3,
    dust_usdc: float = 1e-3,
    notes: str = "",
) -> list[LotEvent]:
    """Reconcile SOL, USDC, and the configured target token in one shot.

    Separate dust thresholds because each mint has different decimals and
    a different "meaningful" floor. Default SOL dust is 1e-6 (one millionth
    of a SOL ~ $0.0001), USDC dust is 1e-3 ($0.001), and token dust is
    1e-3 (noise on tokens with 6+ decimals).

    USDC reconciliation is skipped if ``usdc_mint`` is empty — the bot may
    be running in a SOL-only configuration where USDC isn't tracked.
    """
    ts = timestamp or now_iso()
    emitted: list[LotEvent] = []

    sol_event = reconcile_mint(
        ledger,
        mint=sol_mint,
        actual_qty=sol_balance,
        current_price_usd=sol_price_usd,
        timestamp=ts,
        dust=dust_sol,
        notes=notes,
    )
    if sol_event is not None:
        emitted.append(sol_event)

    if usdc_mint:
        usdc_event = reconcile_mint(
            ledger,
            mint=usdc_mint,
            actual_qty=usdc_balance,
            current_price_usd=usdc_price_usd,
            timestamp=ts,
            dust=dust_usdc,
            notes=notes,
        )
        if usdc_event is not None:
            emitted.append(usdc_event)

    if token_mint:
        token_event = reconcile_mint(
            ledger,
            mint=token_mint,
            actual_qty=token_balance,
            current_price_usd=token_price_usd,
            timestamp=ts,
            dust=dust_token,
            notes=notes,
        )
        if token_event is not None:
            emitted.append(token_event)

    return emitted
