"""Solana RPC tools: balance, token info, transactions."""

import logging
from typing import Any

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.trading.portfolio import LAMPORTS_PER_SOL, Portfolio

logger = logging.getLogger(__name__)


def register_tools(
    registry: ToolRegistry,
    *,
    rpc_url: str,
    wallet_address: str = "",
    portfolio: Portfolio | None = None,
) -> None:
    """Register all Solana RPC tools.

    ``wallet_address`` is the bot's own wallet; tools that take an owner/
    address argument default to it when the model omits or hallucinates
    one (common failure mode).
    """

    async def get_solana_balance(args: dict[str, Any]) -> dict[str, Any]:
        # IMPORTANT: this tool always checks the *bot's* own wallet, even if
        # the model supplies an address argument. Models routinely hallucinate
        # plausible-looking addresses, get back 0 SOL, and then refuse to
        # trade. Hard-pinning to the bot's wallet eliminates that failure
        # mode entirely.
        requested = args.get("address")
        if requested and wallet_address and requested != wallet_address:
            logger.warning(
                "get_solana_balance ignoring hallucinated address %s — using bot wallet %s instead",
                requested,
                wallet_address,
            )
        address = wallet_address or requested
        if not address:
            return {"error": "No default wallet configured"}
        async with AsyncClient(rpc_url) as client:
            resp = await client.get_balance(Pubkey.from_string(address))
            sol = resp.value / LAMPORTS_PER_SOL
            return {
                "address": address,
                "balance_sol": sol,
                "balance_lamports": resp.value,
                "note": "this tool always returns the bot's own wallet balance",
            }

    registry.register(
        name="get_solana_balance",
        description=(
            "Get the SOL balance for a Solana wallet address. "
            "Defaults to the bot's own wallet if address is omitted."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": ("Solana wallet address (defaults to bot's wallet)"),
                },
            },
        },
        handler=get_solana_balance,
    )

    async def get_spl_token_balance(args: dict[str, Any]) -> dict[str, Any]:
        # Hard-pin to the bot's wallet (see get_solana_balance for why).
        requested = args.get("owner_address")
        if requested and wallet_address and requested != wallet_address:
            logger.warning(
                "get_spl_token_balance ignoring hallucinated owner %s — "
                "using bot wallet %s instead",
                requested,
                wallet_address,
            )
        owner_address = wallet_address or requested
        mint_address = args["mint_address"]
        if not owner_address:
            return {"error": "No default wallet configured"}
        if portfolio is None:
            return {"error": "Portfolio not wired into solana_tools"}

        # Delegate to Portfolio.get_token_balance — single source of truth
        # for the multi-program + ATA fallback strategy.
        balance = await portfolio.get_token_balance(owner_address, mint_address)
        return {
            "owner": owner_address,
            "mint": mint_address,
            "balance": balance,
            "note": "this tool always returns the bot's own wallet balance",
        }

    registry.register(
        name="get_spl_token_balance",
        description=(
            "Get SPL token balance for a wallet. Handles both the legacy "
            "Token program and Token-2022. owner_address defaults to the "
            "bot's own wallet if omitted."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "owner_address": {
                    "type": "string",
                    "description": (
                        "Wallet address that owns the tokens (defaults to bot's wallet)"
                    ),
                },
                "mint_address": {"type": "string", "description": "Token mint address"},
            },
            "required": ["mint_address"],
        },
        handler=get_spl_token_balance,
    )

    async def get_token_info(args: dict[str, Any]) -> dict[str, Any]:
        import httpx

        mint_address = args["mint_address"]
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
                        "address": token["id"],
                        "symbol": token.get("symbol", ""),
                        "name": token.get("name", ""),
                        "decimals": token.get("decimals", 0),
                        "logo": token.get("icon", ""),
                    }
            return {"error": f"Token {mint_address} not found in Jupiter token list"}

    registry.register(
        name="get_token_info",
        description="Get token metadata (symbol, name, decimals) from Jupiter token list",
        input_schema={
            "type": "object",
            "properties": {
                "mint_address": {"type": "string", "description": "Token mint address"},
            },
            "required": ["mint_address"],
        },
        handler=get_token_info,
    )

    async def get_recent_transactions(args: dict[str, Any]) -> dict[str, Any]:
        # Hard-pin to the bot's wallet (see get_solana_balance for why).
        requested = args.get("address")
        if requested and wallet_address and requested != wallet_address:
            logger.warning(
                "get_recent_transactions ignoring hallucinated address %s — "
                "using bot wallet %s instead",
                requested,
                wallet_address,
            )
        address = wallet_address or requested
        if not address:
            return {"error": "No default wallet configured"}
        limit = args.get("limit", 10)
        async with AsyncClient(rpc_url) as client:
            resp = await client.get_signatures_for_address(Pubkey.from_string(address), limit=limit)
            txns = []
            for sig_info in resp.value:
                txns.append(
                    {
                        "signature": str(sig_info.signature),
                        "slot": sig_info.slot,
                        "block_time": sig_info.block_time,
                        "err": str(sig_info.err) if sig_info.err else None,
                    }
                )
            return {"address": address, "transactions": txns, "count": len(txns)}

    registry.register(
        name="get_recent_transactions",
        description=(
            "Get recent transaction signatures for a Solana address "
            "(defaults to bot's own wallet if address is omitted)"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "address": {
                    "type": "string",
                    "description": ("Solana wallet address (defaults to bot's wallet)"),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max transactions to return",
                    "default": 10,
                },
            },
        },
        handler=get_recent_transactions,
    )
