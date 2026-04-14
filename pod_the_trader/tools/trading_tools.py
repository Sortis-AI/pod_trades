"""DEX trading tools: quotes, swaps, feasibility.

Amount convention:
- All amount inputs are in UI units of the INPUT token.
  "0.1" with input SOL means 0.1 SOL.
  "50000" with input SQUIRE means 50,000 SQUIRE.
- Token decimals are looked up from Jupiter's token list and cached.
- Optionally, `percent_of_balance` (0-100) can be used instead of `amount_in`
  to size the trade as a fraction of the actual on-chain wallet balance.
"""

import logging
from typing import Any

import httpx
from solders.keypair import Keypair

from pod_the_trader.config import Config
from pod_the_trader.data.ledger import (
    TradeEntry,
    TradeLedger,
    format_trade_pnl,
    now_iso,
)
from pod_the_trader.data.lot_ledger import LotLedger, emit_trade_events
from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.trading.dex import SOL_MINT, USDC_MINT, JupiterDex
from pod_the_trader.trading.portfolio import Portfolio
from pod_the_trader.tui.publisher import NullPublisher, Publisher

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000

# Module-level cache for token decimals — {mint: decimals}
_DECIMALS_CACHE: dict[str, int] = {SOL_MINT: 9}


async def _fetch_decimals(mint: str) -> int:
    """Look up token decimals from Jupiter's token search API, with cache."""
    if mint in _DECIMALS_CACHE:
        return _DECIMALS_CACHE[mint]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as http:
            resp = await http.get(
                "https://lite-api.jup.ag/tokens/v2/search",
                params={"query": mint},
                headers={"User-Agent": "pod-the-trader/0.1"},
            )
            resp.raise_for_status()
            data = resp.json()
        for token in data:
            if token.get("id") == mint:
                decimals = int(token.get("decimals", 6))
                _DECIMALS_CACHE[mint] = decimals
                return decimals
    except Exception as e:
        logger.warning("Failed to fetch decimals for %s: %s", mint[:12], e)
    # Default: 6 for SPL tokens (most common on Solana)
    return 6


async def _resolve_amount_raw(
    args: dict[str, Any],
    input_mint: str,
    portfolio: Portfolio,
    wallet_address: str,
) -> tuple[int, int, float, str | None]:
    """Resolve the amount to swap into raw input-token units.

    Supports three forms (in priority order):
      1. `amount_in_raw` — raw input-token units (int)
      2. `amount_in` — UI units of the input token (float)
      3. `percent_of_balance` — 0-100, fraction of on-chain balance
      4. legacy `amount_sol` — UI units (deprecated, logged warning)

    Returns: (amount_raw, decimals, amount_ui, error_or_None)
    """
    decimals = await _fetch_decimals(input_mint)

    # 1. Raw units bypass
    if "amount_in_raw" in args and args["amount_in_raw"] is not None:
        raw = int(args["amount_in_raw"])
        return raw, decimals, raw / (10**decimals), None

    # 2. UI units
    amount_ui: float | None = None
    if "amount_in" in args and args["amount_in"] is not None:
        amount_ui = float(args["amount_in"])
    elif "amount_sol" in args and args["amount_sol"] is not None:
        # Legacy parameter — treat as UI units of whatever the input token is.
        amount_ui = float(args["amount_sol"])
        logger.warning(
            "Swap tool called with deprecated 'amount_sol' parameter; "
            "treating as UI units of input token %s",
            input_mint[:12],
        )

    if amount_ui is not None:
        # Safety check: can't sell more than you have
        if input_mint != SOL_MINT:
            try:
                balance = await portfolio.get_token_balance(wallet_address, input_mint)
                if amount_ui > balance:
                    return (
                        0,
                        decimals,
                        0.0,
                        (
                            f"amount_in {amount_ui} exceeds wallet balance "
                            f"{balance:.6f} for {input_mint[:8]}..."
                        ),
                    )
            except Exception as e:
                logger.debug("Balance check failed: %s", e)
        else:
            # For SOL, compare to on-chain SOL balance minus a safety buffer
            try:
                sol_balance = await portfolio.get_sol_balance(wallet_address)
                # Keep 0.01 SOL reserve for gas
                if amount_ui > max(0, sol_balance - 0.01):
                    return (
                        0,
                        decimals,
                        0.0,
                        (
                            f"amount_in {amount_ui} SOL exceeds available "
                            f"{max(0, sol_balance - 0.01):.6f} "
                            "(wallet reserves 0.01 SOL for gas)"
                        ),
                    )
            except Exception as e:
                logger.debug("SOL balance check failed: %s", e)

        raw = int(amount_ui * (10**decimals))
        return raw, decimals, amount_ui, None

    # 3. Percent of balance
    if "percent_of_balance" in args and args["percent_of_balance"] is not None:
        pct = float(args["percent_of_balance"])
        if pct <= 0 or pct > 100:
            return 0, decimals, 0.0, f"percent_of_balance must be in (0, 100], got {pct}"
        try:
            if input_mint == SOL_MINT:
                balance = await portfolio.get_sol_balance(wallet_address)
                # Reserve 0.01 SOL for gas
                usable = max(0, balance - 0.01)
            else:
                usable = await portfolio.get_token_balance(wallet_address, input_mint)
        except Exception as e:
            return 0, decimals, 0.0, f"Failed to fetch balance: {e}"

        amount_ui = usable * (pct / 100.0)
        raw = int(amount_ui * (10**decimals))
        if raw <= 0:
            return 0, decimals, 0.0, "Computed amount is zero (empty balance?)"
        return raw, decimals, amount_ui, None

    return (
        0,
        decimals,
        0.0,
        (
            "Must specify one of: amount_in (UI units), amount_in_raw (raw units), "
            "or percent_of_balance (0-100)"
        ),
    )


AMOUNT_SCHEMA_PROPS = {
    "amount_in": {
        "type": "number",
        "description": (
            "Amount of INPUT token to swap, in UI units (e.g. 0.1 for "
            "0.1 SOL, or 50000 for 50,000 SQUIRE). Whatever the input_mint "
            "is, this is in its natural units."
        ),
    },
    "amount_in_raw": {
        "type": "integer",
        "description": (
            "Amount in raw atomic units of the input token (bypasses "
            "decimals conversion). Use only if you know the raw amount."
        ),
    },
    "percent_of_balance": {
        "type": "number",
        "description": (
            "Alternative to amount_in: swap this percentage (0-100) of your "
            "on-chain balance of the input token. Example: 50 means sell "
            "half of what you hold."
        ),
    },
    "slippage_bps": {
        "type": "integer",
        "description": "Slippage tolerance in basis points (50 = 0.5%)",
    },
}


def register_tools(
    registry: ToolRegistry,
    *,
    config: Config,
    jupiter_dex: JupiterDex,
    portfolio: Portfolio,
    wallet_address: str,
    ledger: TradeLedger | None = None,
    lot_ledger: LotLedger | None = None,
    session_id: str = "",
    publisher: Publisher | None = None,
) -> None:
    pub: Publisher = publisher or NullPublisher()
    tui_mode: bool = not isinstance(pub, NullPublisher)
    """Register all trading tools."""

    _keypair_holder: dict[str, Keypair | None] = {"keypair": None}
    # Late-bound: the agent calls registry._set_target_symbol("SQUIRE")
    # once Jupiter token metadata is fetched. Until then we still resolve
    # SOL/USDC aliases without it.
    _symbol_holder: dict[str, str] = {"target": ""}

    def set_keypair(kp: Keypair) -> None:
        _keypair_holder["keypair"] = kp

    def set_target_symbol(symbol: str) -> None:
        _symbol_holder["target"] = (symbol or "").strip()

    registry._set_trading_keypair = set_keypair  # type: ignore[attr-defined]
    registry._set_target_symbol = set_target_symbol  # type: ignore[attr-defined]

    def _resolve_mint(value: str) -> str:
        """Translate a symbol or alias into a real mint address.

        The model frequently passes ``"SOL"`` / ``"USDC"`` / ``"SQUIRE"``
        (the symbol) instead of the 43-char base58 mint, even when the
        system prompt asks for the address. Rather than refuse those
        calls (which the model has no way to recover from cleanly), we
        accept the common aliases and translate to the real mint.

        Anything that doesn't match an alias is returned untouched — the
        route guard then has the final say on whether it's tradeable.
        """
        if not isinstance(value, str):
            return value
        v = value.strip().upper()
        if v in ("SOL", "WSOL", "WRAPPED SOL"):
            return SOL_MINT
        if v in ("USDC", "USD"):
            return USDC_MINT
        tgt_symbol = _symbol_holder["target"]
        if tgt_symbol and v == tgt_symbol.upper():
            return config.get("trading.target_token_address", "") or value
        return value

    def _check_route(input_mint: str, output_mint: str) -> str | None:
        """Reject swap routes outside the allowed universe.

        The bot is restricted to trading SOL, USDC, and the configured
        target token. Any other mint as input or output is refused at the
        tool layer so the model can't accidentally route a swap through
        an unexpected asset (and so we don't have to track arbitrary
        tokens in the portfolio / cost-basis ledger).
        """
        target = config.get("trading.target_token_address", "")
        allowed = {SOL_MINT, USDC_MINT}
        if target:
            allowed.add(target)
        if input_mint not in allowed or output_mint not in allowed:
            return (
                f"Disallowed swap route {input_mint[:8]}…→{output_mint[:8]}…: "
                "only SOL, USDC, and the configured target token are tradeable. "
                "Pass the FULL base58 mint address (not the ticker symbol) on "
                "each side: SOL=" + SOL_MINT + ", USDC=" + USDC_MINT + "."
            )
        return None

    async def _check_min_size(input_mint: str, amount_ui: float) -> str | None:
        """Reject swaps below ``trading.min_trade_size_usdc``.

        The config has a ``min_trade_size_usdc`` knob (default $1) so the
        bot doesn't emit dust trades where network fees exceed the trade
        value. Without this check the model can happily execute a sell of
        0.25 tokens for $0.00004 of proceeds while paying ~$0.0004 of gas,
        which is a guaranteed money-loser.

        Computes the estimated USD value of the INPUT leg by fetching the
        input mint's spot price from Jupiter. Returns an error string to
        send back to the model (so it can resize and retry), or ``None``
        if the trade is above the minimum.
        """
        min_usdc = float(config.get("trading.min_trade_size_usdc", 1.0) or 0.0)
        if min_usdc <= 0:
            return None
        try:
            price = await jupiter_dex.get_token_price(input_mint)
        except Exception as e:
            logger.warning(
                "Min-size check: failed to fetch price for %s (%s); allowing trade to proceed.",
                input_mint[:8],
                e,
            )
            return None
        value_usd = amount_ui * price
        if value_usd < min_usdc:
            return (
                f"Trade size ${value_usd:.6f} is below the "
                f"min_trade_size_usdc of ${min_usdc:.2f}. Network fees "
                "would exceed the trade value. Increase amount_in or "
                "percent_of_balance so the USD value of the input leg "
                f"is at least ${min_usdc:.2f}."
            )
        return None

    async def get_swap_quote(args: dict[str, Any]) -> dict[str, Any]:
        input_mint = _resolve_mint(args["input_mint"])
        output_mint = _resolve_mint(args["output_mint"])
        if route_err := _check_route(input_mint, output_mint):
            return {"error": route_err}
        slippage_bps = int(args.get("slippage_bps", config.get("trading.max_slippage_bps", 50)))

        amount_raw, in_decimals, amount_ui, err = await _resolve_amount_raw(
            args, input_mint, portfolio, wallet_address
        )
        if err:
            return {"error": err}

        if size_err := await _check_min_size(input_mint, amount_ui):
            return {"error": size_err}

        out_decimals = await _fetch_decimals(output_mint)
        quote = await jupiter_dex.get_quote(input_mint, output_mint, amount_raw, slippage_bps)

        return {
            "input_mint": quote.input_mint,
            "output_mint": quote.output_mint,
            "in_amount_raw": quote.in_amount,
            "in_amount_ui": quote.in_amount / (10**in_decimals),
            "out_amount_raw": quote.out_amount,
            "out_amount_ui": quote.out_amount / (10**out_decimals),
            "price_impact_pct": quote.price_impact_pct,
            "slippage_bps": quote.slippage_bps,
            "summary": (
                f"Swap {amount_ui:,.6f} ({input_mint[:8]}...) for "
                f"{quote.out_amount / (10**out_decimals):,.6f} "
                f"({output_mint[:8]}...), impact: {quote.price_impact_pct:.2f}%"
            ),
        }

    registry.register(
        name="get_swap_quote",
        description=(
            "Get a swap quote from Jupiter DEX. Specify amount_in in UI "
            "units of the INPUT token (e.g. 0.1 for 0.1 SOL, 50000 for "
            "50,000 SQUIRE), or percent_of_balance (0-100) to size by %."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "input_mint": {
                    "type": "string",
                    "description": "Input token mint address",
                },
                "output_mint": {
                    "type": "string",
                    "description": "Output token mint address",
                },
                **AMOUNT_SCHEMA_PROPS,
            },
            "required": ["input_mint", "output_mint"],
        },
        handler=get_swap_quote,
    )

    async def execute_swap(args: dict[str, Any]) -> dict[str, Any]:
        keypair = _keypair_holder["keypair"]
        if keypair is None:
            return {"error": "Trading keypair not configured"}

        input_mint = _resolve_mint(args["input_mint"])
        output_mint = _resolve_mint(args["output_mint"])
        if route_err := _check_route(input_mint, output_mint):
            return {"error": route_err}
        slippage_bps = int(args.get("slippage_bps", config.get("trading.max_slippage_bps", 50)))

        amount_raw, input_decimals, amount_ui, err = await _resolve_amount_raw(
            args, input_mint, portfolio, wallet_address
        )
        if err:
            return {"error": err}

        if size_err := await _check_min_size(input_mint, amount_ui):
            logger.warning("Rejected below-minimum swap: %s", size_err)
            return {"error": size_err}

        output_decimals = await _fetch_decimals(output_mint)

        logger.info(
            "Executing swap: %.6f %s -> %s (raw=%d)",
            amount_ui,
            input_mint[:8],
            output_mint[:8],
            amount_raw,
        )

        result = await jupiter_dex.execute_swap(
            keypair, input_mint, output_mint, amount_raw, slippage_bps
        )

        if result.success:
            # Side is defined relative to the configured target token, not
            # to SOL. A USDC→TARGET swap is a BUY (we acquired target); a
            # TARGET→USDC swap is a SELL. Anything else (SOL→USDC, etc.)
            # is recorded as "swap" so it doesn't pollute trading P&L.
            target_mint = config.get("trading.target_token_address", "")
            if output_mint == target_mint:
                side = "buy"
            elif input_mint == target_mint:
                side = "sell"
            else:
                side = "swap"

            sol_price = await _get_sol_price(jupiter_dex)

            # Price each leg independently from Jupiter — never assume one
            # side is SOL or that input and output share a price (a recent
            # bug recorded a USDC→TARGET buy with USDC's $1 price applied
            # to the target token, producing a $74k phantom value).
            try:
                input_price = await jupiter_dex.get_token_price(input_mint)
            except Exception:
                input_price = 0.0
            try:
                output_price = await jupiter_dex.get_token_price(output_mint)
            except Exception:
                output_price = 0.0

            input_amount_ui = result.in_amount / (10**input_decimals)
            expected_out_ui = result.out_amount / (10**output_decimals)
            actual_out_ui = (
                result.actual_out_amount / (10**output_decimals)
                if result.actual_out_amount
                else expected_out_ui
            )

            input_value_usd = input_amount_ui * input_price
            output_value_usd = actual_out_ui * output_price

            slippage_realized = 0.0
            if result.out_amount > 0 and result.actual_out_amount > 0:
                slippage_realized = (
                    (result.out_amount - result.actual_out_amount) / result.out_amount * 10000
                )

            gas_sol = result.gas_lamports / LAMPORTS_PER_SOL
            gas_usd = gas_sol * sol_price

            if ledger is not None:
                entry = TradeEntry(
                    timestamp=now_iso(),
                    session_id=session_id,
                    side=side,
                    input_mint=input_mint,
                    input_symbol="SOL" if input_mint == SOL_MINT else "",
                    input_decimals=input_decimals,
                    input_amount_raw=result.in_amount,
                    input_amount_ui=input_amount_ui,
                    input_price_usd=input_price,
                    input_value_usd=input_value_usd,
                    output_mint=output_mint,
                    output_symbol="SOL" if output_mint == SOL_MINT else "",
                    output_decimals=output_decimals,
                    expected_out_raw=result.out_amount,
                    expected_out_ui=expected_out_ui,
                    actual_out_raw=result.actual_out_amount or result.out_amount,
                    actual_out_ui=actual_out_ui,
                    output_price_usd=output_price,
                    output_value_usd=output_value_usd,
                    slippage_bps_requested=result.slippage_bps_requested,
                    slippage_bps_realized=slippage_realized,
                    price_impact_pct=result.price_impact_pct,
                    sol_price_usd=sol_price,
                    gas_lamports=result.gas_lamports,
                    gas_sol=gas_sol,
                    gas_usd=gas_usd,
                    signature=result.signature or "",
                    block_slot=result.block_slot,
                    block_time=result.block_time,
                    wallet=wallet_address,
                    model=config.get("agent.model", ""),
                )
                ledger.append(entry)

                # Mirror the swap into the lot-based cost-basis ledger so
                # open-position state stays consistent with the trade.
                if lot_ledger is not None:
                    try:
                        lot_events = emit_trade_events(
                            timestamp=entry.timestamp,
                            input_mint=input_mint,
                            input_qty=input_amount_ui,
                            input_price_usd=input_price,
                            output_mint=output_mint,
                            output_qty=actual_out_ui,
                            output_price_usd=output_price,
                            gas_sol=gas_sol,
                            sol_price_usd=sol_price,
                            sol_mint=SOL_MINT,
                            tx_sig=entry.signature,
                        )
                        lot_ledger.append_many(lot_events)
                    except Exception as e:
                        logger.warning("Failed to append lot events: %s", e)

                try:
                    pnl = ledger.per_trade_pnl(entry)
                    summary_text = format_trade_pnl(pnl)
                    for line in summary_text.splitlines():
                        logger.info(line)

                    # Gather the post-trade portfolio snapshot once (used by
                    # both the CLI print path and the publisher event).
                    snapshot: dict[str, object] = {}
                    try:
                        sol_balance = await portfolio.get_sol_balance(wallet_address)
                        target = config.get("trading.target_token_address", "")
                        token_balance = 0.0
                        token_price = 0.0
                        if target:
                            token_balance = await portfolio.get_token_balance(
                                wallet_address, target
                            )
                            try:
                                token_price = await jupiter_dex.get_token_price(target)
                            except Exception:
                                token_price = 0.0
                        sol_value = sol_balance * sol_price
                        token_value = token_balance * token_price
                        total = sol_value + token_value
                        snapshot = {
                            "sol_ui": sol_balance,
                            "sol_value_usd": sol_value,
                            "token_ui": token_balance,
                            "token_price_usd": token_price,
                            "token_value_usd": token_value,
                            "total_usd": total,
                        }
                    except Exception as e:
                        logger.debug("Post-trade snapshot failed: %s", e)

                    # Emit to whoever is listening — TUI or no one.
                    pub.on_trade(
                        {
                            "timestamp": entry.timestamp,
                            "side": entry.side,
                            "input_value_usd": entry.input_value_usd,
                            "actual_out_ui": entry.actual_out_ui,
                            "signature": entry.signature,
                        },
                        pnl,
                    )
                    if snapshot:
                        pub.on_portfolio_snapshot(snapshot)

                    # CLI path still prints the block to stdout.
                    if not tui_mode:
                        print()
                        print(summary_text)
                        if snapshot:
                            print("  Portfolio after trade:")
                            print(
                                f"    SOL:     {snapshot['sol_ui']:.6f} "
                                f"(${snapshot['sol_value_usd']:,.4f})"
                            )
                            if config.get("trading.target_token_address", ""):
                                print(
                                    f"    target:  {snapshot['token_ui']:,.4f} "
                                    f"@ ${snapshot['token_price_usd']:.8f} "
                                    f"= ${snapshot['token_value_usd']:,.4f}"
                                )
                            print(f"    total:   ${snapshot['total_usd']:,.4f}")
                        print()
                except Exception as e:
                    logger.warning("Failed to compute per-trade P&L: %s", e)

        return {
            "success": result.success,
            "signature": result.signature,
            "in_amount_raw": result.in_amount,
            "in_amount_ui": result.in_amount / (10**input_decimals),
            "out_amount_raw": result.actual_out_amount or result.out_amount,
            "out_amount_ui": (
                (result.actual_out_amount or result.out_amount) / (10**output_decimals)
            ),
            "error": result.error,
        }

    registry.register(
        name="execute_swap",
        description=(
            "Execute a token swap via Jupiter DEX. Specify amount_in in UI "
            "units of the INPUT token (e.g. 0.1 to sell 0.1 SOL, or 200000 "
            "to sell 200,000 SQUIRE). Alternatively use percent_of_balance "
            "(0-100) to sell a fraction of your holdings. The tool will "
            "refuse if the amount exceeds your on-chain balance."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "input_mint": {
                    "type": "string",
                    "description": "Input token mint address",
                },
                "output_mint": {
                    "type": "string",
                    "description": "Output token mint address",
                },
                **AMOUNT_SCHEMA_PROPS,
            },
            "required": ["input_mint", "output_mint"],
        },
        handler=execute_swap,
    )

    async def check_swap_feasibility(args: dict[str, Any]) -> dict[str, Any]:
        input_mint = _resolve_mint(args["input_mint"])
        output_mint = _resolve_mint(args["output_mint"])
        if route_err := _check_route(input_mint, output_mint):
            return {"error": route_err}
        max_impact = args.get(
            "max_impact_pct",
            config.get("trading.max_price_impact_pct", 5.0),
        )

        amount_raw, _decimals, amount_ui, err = await _resolve_amount_raw(
            args, input_mint, portfolio, wallet_address
        )
        if err:
            return {"error": err}

        if size_err := await _check_min_size(input_mint, amount_ui):
            return {
                "feasible": False,
                "price_impact_pct": 0.0,
                "reason": size_err,
            }

        result = await jupiter_dex.check_feasibility(
            input_mint, output_mint, amount_raw, max_impact
        )

        return {
            "feasible": result.feasible,
            "price_impact_pct": result.price_impact_pct,
            "reason": result.reason,
        }

    registry.register(
        name="check_swap_feasibility",
        description=(
            "Check if a swap is feasible given liquidity and price impact "
            "constraints. Uses the same amount_in convention as execute_swap."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "input_mint": {
                    "type": "string",
                    "description": "Input token mint address",
                },
                "output_mint": {
                    "type": "string",
                    "description": "Output token mint address",
                },
                "max_impact_pct": {
                    "type": "number",
                    "description": "Max acceptable price impact %",
                },
                **AMOUNT_SCHEMA_PROPS,
            },
            "required": ["input_mint", "output_mint"],
        },
        handler=check_swap_feasibility,
    )

    async def get_token_price(args: dict[str, Any]) -> dict[str, Any]:
        mint_address = args.get("mint_address") or config.get("trading.target_token_address", "")
        if not mint_address:
            return {"error": "No mint_address specified and no target token"}
        price = await jupiter_dex.get_token_price(mint_address)
        return {"mint": mint_address, "price_usd": price}

    registry.register(
        name="get_token_price",
        description=(
            "Get the current USD price for a token "
            "(defaults to target token if mint_address omitted)"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mint_address": {
                    "type": "string",
                    "description": ("Token mint address (defaults to target token)"),
                },
            },
        },
        handler=get_token_price,
    )


async def _get_sol_price(dex: JupiterDex) -> float:
    try:
        return await dex.get_token_price(SOL_MINT)
    except Exception:
        return 0.0
