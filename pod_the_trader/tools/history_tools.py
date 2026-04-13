"""Tools for querying the persistent trade ledger and price history."""

import logging
from typing import Any

from pod_the_trader.config import Config
from pod_the_trader.data.ledger import TradeLedger
from pod_the_trader.data.price_log import PriceLog
from pod_the_trader.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


def register_tools(
    registry: ToolRegistry,
    *,
    ledger: TradeLedger | None,
    price_log: PriceLog | None,
    config: Config,
) -> None:
    """Register history & analytics tools."""

    if ledger is not None:

        async def get_ledger_summary(args: dict[str, Any]) -> dict[str, Any]:
            """Return all-time P&L summary from the ledger."""
            return ledger.summary()

        registry.register(
            name="get_ledger_summary",
            description=(
                "Get all-time P&L summary from the persistent trade ledger: "
                "trade count, realized PnL, win rate, average buy/sell price, "
                "gas spent, tokens currently held."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=get_ledger_summary,
        )

        async def get_recent_ledger_trades(
            args: dict[str, Any],
        ) -> dict[str, Any]:
            limit = int(args.get("limit", 10))
            trades = ledger.read_all()[-limit:]
            return {
                "count": len(trades),
                "trades": [
                    {
                        "timestamp": t.timestamp,
                        "side": t.side,
                        "input_amount_ui": t.input_amount_ui,
                        "input_value_usd": t.input_value_usd,
                        "actual_out_ui": t.actual_out_ui,
                        "output_value_usd": t.output_value_usd,
                        "output_price_usd": t.output_price_usd,
                        "slippage_bps_realized": t.slippage_bps_realized,
                        "gas_usd": t.gas_usd,
                        "signature": t.signature,
                    }
                    for t in trades
                ],
            }

        registry.register(
            name="get_recent_ledger_trades",
            description="Read recent trades from the persistent CSV ledger",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max number of trades to return",
                        "default": 10,
                    },
                },
            },
            handler=get_recent_ledger_trades,
        )

    if price_log is not None:

        async def get_price_history(args: dict[str, Any]) -> dict[str, Any]:
            # Default to the target token if mint not provided — the model
            # frequently forgets and we'd rather return target-token history
            # than error out.
            mint = args.get("mint") or config.get(
                "trading.target_token_address", ""
            )
            if not mint:
                return {"error": "No mint specified and no target token configured"}
            limit = int(args.get("limit", 50))
            ticks = price_log.read_for_mint(mint)[-limit:]
            return {
                "mint": mint,
                "count": len(ticks),
                "ticks": [
                    {
                        "timestamp": t.timestamp,
                        "price_usd": t.price_usd,
                        "liquidity_usd": t.liquidity_usd,
                        "price_change_24h_pct": t.price_change_24h_pct,
                    }
                    for t in ticks
                ],
            }

        registry.register(
            name="get_price_history",
            description=(
                "Read recent price ticks from the persistent CSV price log "
                "for a given mint (defaults to the target token if omitted). "
                "Use this to compute returns, volatility, trends, or any "
                "quant metric."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mint": {
                        "type": "string",
                        "description": (
                            "Token mint address; defaults to target token"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of ticks to return",
                        "default": 50,
                    },
                },
            },
            handler=get_price_history,
        )

        async def get_price_volatility(args: dict[str, Any]) -> dict[str, Any]:
            # Default to the target token if mint not provided — the model
            # frequently omits it and we'd rather return target volatility
            # than error out.
            mint = args.get("mint") or config.get(
                "trading.target_token_address", ""
            )
            if not mint:
                return {"error": "No mint specified and no target token configured"}
            vol = price_log.volatility(mint)
            returns = price_log.returns(mint)
            return {
                "mint": mint,
                "volatility": vol,
                "sample_count": len(returns),
                "mean_return": sum(returns) / len(returns) if returns else 0.0,
            }

        registry.register(
            name="get_price_volatility",
            description=(
                "Compute period log-return volatility for a mint from the "
                "persistent price log (defaults to target token if omitted)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "mint": {
                        "type": "string",
                        "description": (
                            "Token mint; defaults to target token"
                        ),
                    },
                },
            },
            handler=get_price_volatility,
        )
