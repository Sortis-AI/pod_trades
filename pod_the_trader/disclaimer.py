"""Startup disclaimer.

Shown on every launch. The user must type ``I ACCEPT`` exactly (case-
insensitive, whitespace-stripped) to continue; anything else exits cleanly
with a one-line message. There is no bypass — not a CLI flag, not an env
var, not a file marker. Pod The Trader moves real funds on mainnet and the
operator should see the disclaimer every time so muscle memory never lets
them skip past the risk acknowledgement.

Non-interactive runs (e.g. ``scripts/e2e_test.sh``) must pipe the
acceptance phrase to stdin explicitly.
"""

from __future__ import annotations

import sys

ACCEPTANCE_PHRASE = "I ACCEPT"

DISCLAIMER_TEXT = """\
================================================================================
                           POD THE TRADER — DISCLAIMER
================================================================================

This is autonomous trading software that will buy and sell real tokens on
the Solana mainnet using your wallet and your money. Before you start it,
read this and make sure you understand what you're agreeing to.

  1. REAL FUNDS. Every trade moves actual SOL, USDC, and SPL tokens from
     your wallet. There is no testnet mode, no dry run, no simulation
     layer. Losses are real and on-chain transactions are irreversible.

  2. LLM-DRIVEN DECISIONS. Trading decisions are made by a large language
     model (minimax-m2.7 via Level5). The model can hallucinate, misread
     market data, make arithmetic errors, pick bad sizes, or act on stale
     context. It has already done all of these during development. Do not
     assume it will make profitable trades.

  3. NO WARRANTY. This software is experimental and provided as-is, with
     no guarantee of correctness, profitability, uptime, or data integrity.
     Recent history includes pricing bugs, unintended trade routes, dust
     trades, and decision-execution mismatches. More bugs almost certainly
     remain.

  4. YOU ARE THE OPERATOR. You are responsible for monitoring the bot,
     setting sensible position limits, funding the wallet appropriately,
     and shutting it down if something looks wrong. The bot will not stop
     itself just because it's losing money.

  5. NOT FINANCIAL ADVICE. Nothing this software outputs — on-screen,
     in logs, or in summaries — constitutes financial, legal, tax, or
     investment advice. Memecoin trading is high-risk and most positions
     lose money.

  6. KEY CUSTODY. Your private key lives in ~/.pod_the_trader/. Anyone
     with access to that file can drain your wallet. You are solely
     responsible for the security of that file and the machine it sits on.

  7. NO RECOURSE. If the bot loses your money, executes an unintended
     trade, fails to execute an intended trade, or misreports P&L, there
     is no one to appeal to. Do not put more into this wallet than you
     can afford to lose entirely.

By continuing, you confirm that you have read and understood the above,
that you accept full responsibility for any losses, and that you are
running this software voluntarily and at your own risk.

Type "I ACCEPT" to continue, or anything else to exit:
================================================================================
"""


def require_acceptance(
    stream_in: object | None = None,
    stream_out: object | None = None,
) -> None:
    """Print the disclaimer and block until the user types ``I ACCEPT``.

    Exits the process with status 0 if the user types anything else or
    hits EOF. Streams are injectable so tests can drive the function
    without touching real stdin/stdout.
    """
    out = stream_out if stream_out is not None else sys.stdout
    inp = stream_in if stream_in is not None else sys.stdin

    out.write(DISCLAIMER_TEXT)
    out.flush()

    try:
        raw = inp.readline()
    except KeyboardInterrupt:
        out.write("\nDisclaimer declined. Exiting.\n")
        out.flush()
        sys.exit(0)

    if not raw:
        # EOF (e.g. stdin closed) is treated as decline.
        out.write("Disclaimer declined (no input). Exiting.\n")
        out.flush()
        sys.exit(0)

    response = raw.strip()
    if response.upper() != ACCEPTANCE_PHRASE:
        out.write("Disclaimer declined. Exiting.\n")
        out.flush()
        sys.exit(0)

    out.write("\n")
    out.flush()
