"""Market data tools: prices, token details, analysis."""

import logging
from typing import Any

from pod_the_trader.config import Config
from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.trading.dex import SOL_MINT, JupiterDex

logger = logging.getLogger(__name__)


def register_tools(
    registry: ToolRegistry,
    *,
    config: Config,
    jupiter_dex: JupiterDex,
) -> None:
    """Register all market data tools."""

    async def get_market_price(args: dict[str, Any]) -> dict[str, Any]:
        mint_address = args.get("mint_address") or config.get("trading.target_token_address", "")
        if not mint_address:
            return {"error": "No mint_address specified and no target token"}
        price = await jupiter_dex.get_token_price(mint_address)
        return {"mint": mint_address, "price_usd": price}

    registry.register(
        name="get_market_price",
        description="Get the current USD price for a token via Jupiter Price API",
        input_schema={
            "type": "object",
            "properties": {
                "mint_address": {
                    "type": "string",
                    "description": ("Token mint address (defaults to target token)"),
                },
            },
        },
        handler=get_market_price,
    )

    async def get_token_details(args: dict[str, Any]) -> dict[str, Any]:
        import httpx

        mint_address = args.get("mint_address") or config.get("trading.target_token_address", "")
        if not mint_address:
            return {"error": "No mint_address specified and no target token"}
        price = await jupiter_dex.get_token_price(mint_address)

        async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as http:
            resp = await http.get(
                "https://lite-api.jup.ag/tokens/v2/search",
                params={"query": mint_address},
            )
            resp.raise_for_status()
            tokens = resp.json()
            for token in tokens:
                if token.get("id") == mint_address:
                    return {
                        "address": mint_address,
                        "symbol": token.get("symbol", ""),
                        "name": token.get("name", ""),
                        "decimals": token.get("decimals", 0),
                        "price_usd": price,
                    }

        return {"address": mint_address, "price_usd": price, "metadata": "not found"}

    registry.register(
        name="get_token_details",
        description="Get token metadata combined with current price",
        input_schema={
            "type": "object",
            "properties": {
                "mint_address": {
                    "type": "string",
                    "description": ("Token mint address (defaults to target token)"),
                },
            },
        },
        handler=get_token_details,
    )

    async def analyze_market_conditions(args: dict[str, Any]) -> dict[str, Any]:
        mint_address = args.get("mint_address") or config.get("trading.target_token_address", "")
        if not mint_address:
            return {"error": "No mint_address specified and no target token"}
        try:
            price = await jupiter_dex.get_token_price(mint_address)
            sol_price = await jupiter_dex.get_token_price(SOL_MINT)

            return {
                "mint": mint_address,
                "price_usd": price,
                "sol_price_usd": sol_price,
                "analysis": {
                    "current_price": price,
                    "sol_reference_price": sol_price,
                    "note": (
                        "Real-time price snapshot. Compare with historical data for trend analysis."
                    ),
                },
            }
        except Exception as e:
            return {"error": f"Market analysis failed: {e}"}

    registry.register(
        name="analyze_market_conditions",
        description=(
            "Analyze current market conditions for a token including price and reference data"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mint_address": {
                    "type": "string",
                    "description": ("Token mint address to analyze (defaults to target token)"),
                },
            },
        },
        handler=analyze_market_conditions,
    )

    async def get_target_token_status(args: dict[str, Any]) -> dict[str, Any]:
        target = config.get("trading.target_token_address", "")
        if not target:
            return {"error": "No target token configured"}

        try:
            price = await jupiter_dex.get_token_price(target)
            return {
                "target_token": target,
                "price_usd": price,
                "max_position_usdc": config.get("trading.max_position_size_usdc"),
                "max_slippage_bps": config.get("trading.max_slippage_bps"),
            }
        except Exception as e:
            return {"error": f"Could not fetch target token status: {e}"}

    registry.register(
        name="get_target_token_status",
        description="Get status and price of the configured target trading token",
        input_schema={
            "type": "object",
            "properties": {},
        },
        handler=get_target_token_status,
    )
