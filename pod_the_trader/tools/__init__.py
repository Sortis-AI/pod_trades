"""Tool registration and factory."""

from pod_the_trader.config import Config
from pod_the_trader.data.ledger import TradeLedger
from pod_the_trader.data.lot_ledger import LotLedger
from pod_the_trader.data.price_log import PriceLog
from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.trading.dex import JupiterDex
from pod_the_trader.trading.portfolio import Portfolio
from pod_the_trader.trading.transaction import TransactionBuilder
from pod_the_trader.tui.publisher import Publisher


def create_registry(
    *,
    config: Config,
    portfolio: Portfolio,
    jupiter_dex: JupiterDex,
    transaction_builder: TransactionBuilder,
    rpc_url: str,
    wallet_address: str,
    ledger: TradeLedger | None = None,
    lot_ledger: LotLedger | None = None,
    price_log: PriceLog | None = None,
    session_id: str = "",
    publisher: Publisher | None = None,
) -> ToolRegistry:
    """Wire up all tool modules and return a populated registry."""
    from pod_the_trader.tools import (
        history_tools,
        market_tools,
        portfolio_tools,
        solana_tools,
        trading_tools,
    )

    registry = ToolRegistry()
    solana_tools.register_tools(
        registry,
        rpc_url=rpc_url,
        wallet_address=wallet_address,
        portfolio=portfolio,
    )
    market_tools.register_tools(registry, config=config, jupiter_dex=jupiter_dex)
    trading_tools.register_tools(
        registry,
        config=config,
        jupiter_dex=jupiter_dex,
        portfolio=portfolio,
        wallet_address=wallet_address,
        ledger=ledger,
        lot_ledger=lot_ledger,
        session_id=session_id,
        publisher=publisher,
    )
    portfolio_tools.register_tools(
        registry, portfolio=portfolio, wallet_address=wallet_address, config=config
    )
    if ledger is not None or price_log is not None:
        history_tools.register_tools(
            registry,
            ledger=ledger,
            price_log=price_log,
            config=config,
        )
    return registry
