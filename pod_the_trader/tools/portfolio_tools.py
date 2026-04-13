"""Portfolio tools: overview, balances, history, PnL."""

import logging
from typing import Any

from pod_the_trader.config import Config
from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.trading.portfolio import Portfolio

logger = logging.getLogger(__name__)


def register_tools(
    registry: ToolRegistry,
    *,
    portfolio: Portfolio,
    wallet_address: str,
    config: Config,
) -> None:
    """Register all portfolio tools."""

    async def get_portfolio_overview(args: dict[str, Any]) -> dict[str, Any]:
        target = config.get("trading.target_token_address", "")
        token_mints = [target] if target else []

        summary = await portfolio.get_portfolio_value(wallet_address, token_mints)
        return {
            "sol_balance": summary.sol_balance,
            "sol_value_usd": summary.sol_value_usd,
            "token_balances": summary.token_balances,
            "token_values_usd": summary.token_values_usd,
            "total_value_usd": summary.total_value_usd,
        }

    registry.register(
        name="get_portfolio_overview",
        description="Get full portfolio overview with real token balances and USD values",
        input_schema={
            "type": "object",
            "properties": {},
        },
        handler=get_portfolio_overview,
    )

    async def get_token_balance(args: dict[str, Any]) -> dict[str, Any]:
        mint_address = args.get("mint_address") or config.get("trading.target_token_address", "")
        if not mint_address:
            return {"error": "No mint_address specified and no target token"}
        balance = await portfolio.get_token_balance(wallet_address, mint_address)
        return {"mint": mint_address, "balance": balance}

    registry.register(
        name="get_token_balance",
        description=(
            "Get balance of a specific token in the wallet "
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
        handler=get_token_balance,
    )

    async def get_trade_history(args: dict[str, Any]) -> dict[str, Any]:
        limit = args.get("limit", 20)
        trades = portfolio.get_trade_history(limit=limit)
        return {
            "trades": [
                {
                    "timestamp": t.timestamp,
                    "side": t.side,
                    "input_mint": t.input_mint,
                    "output_mint": t.output_mint,
                    "input_amount": t.input_amount,
                    "output_amount": t.output_amount,
                    "value_usd": t.value_usd,
                    "signature": t.signature,
                }
                for t in trades
            ],
            "count": len(trades),
        }

    registry.register(
        name="get_trade_history",
        description="Get recent trade history",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max number of trades to return",
                    "default": 20,
                },
            },
        },
        handler=get_trade_history,
    )

    async def calculate_pnl(args: dict[str, Any]) -> dict[str, Any]:
        pnl = portfolio.calculate_pnl()
        return {
            "total_pnl_usd": pnl.total_pnl_usd,
            "win_rate": pnl.win_rate,
            "total_trades": pnl.total_trades,
            "avg_trade_size": pnl.avg_trade_size,
            "largest_win": pnl.largest_win,
            "largest_loss": pnl.largest_loss,
        }

    registry.register(
        name="calculate_pnl",
        description="Calculate profit and loss summary from trade history",
        input_schema={
            "type": "object",
            "properties": {},
        },
        handler=calculate_pnl,
    )
