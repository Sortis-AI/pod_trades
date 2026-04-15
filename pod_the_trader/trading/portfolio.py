"""Portfolio tracking: balances, trade history, and PnL computation."""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

from pod_the_trader.trading.dex import JupiterDex

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")


@dataclass
class TradeRecord:
    """A single recorded trade."""

    timestamp: str
    side: str  # "buy" or "sell"
    input_mint: str
    output_mint: str
    input_amount: float
    output_amount: float
    price_usd: float
    value_usd: float
    signature: str


@dataclass
class PortfolioSummary:
    """Snapshot of portfolio value."""

    sol_balance: float
    sol_value_usd: float
    token_balances: dict[str, float]
    token_values_usd: dict[str, float]
    total_value_usd: float


@dataclass
class PnLSummary:
    """Profit and loss summary."""

    total_pnl_usd: float
    win_rate: float
    total_trades: int
    avg_trade_size: float
    largest_win: float
    largest_loss: float


class Portfolio:
    """Tracks positions, balances, and trade history."""

    def __init__(
        self,
        rpc_url: str,
        jupiter_dex: JupiterDex,
        storage_dir: str = "~/.pod_the_trader",
    ) -> None:
        self._rpc_url = rpc_url
        self._dex = jupiter_dex
        self._storage_dir = Path(storage_dir).expanduser()
        self._history_path = self._storage_dir / "trade_history.json"

    async def get_sol_balance(self, address: str) -> float:
        """Get real SOL balance from RPC."""
        async with AsyncClient(self._rpc_url) as client:
            resp = await client.get_balance(Pubkey.from_string(address))
            return resp.value / LAMPORTS_PER_SOL

    async def get_token_balance(self, owner_address: str, mint_address: str) -> float:
        """Get SPL token balance for a wallet+mint.

        Strategy (in order, first non-error wins):

        1. ``getTokenAccountsByOwner`` with a mint filter under BOTH the
           legacy Token program and Token-2022 program. Dedupes by token
           account pubkey (the RPC sometimes returns the same account
           under both program filters).
        2. Fallback: derive the Associated Token Account address for both
           program variants and fetch each one directly via
           ``getTokenAccountBalance``. This works even when the bulk
           ``getTokenAccountsByOwner`` call fails (some RPC providers
           rate-limit or return empty bodies on it).

        If all paths fail, logs a WARNING (not DEBUG) so the operator can
        see the real exception type and message instead of a silent zero.
        """
        from solana.rpc.types import TokenAccountOpts

        owner = Pubkey.from_string(owner_address)
        mint = Pubkey.from_string(mint_address)

        seen: dict[str, float] = {}
        errors: list[str] = []

        async with AsyncClient(self._rpc_url) as client:
            # ---- Path 1: getTokenAccountsByOwner under both programs ----
            for program_id in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
                try:
                    resp = await client.get_token_accounts_by_owner_json_parsed(
                        owner,
                        TokenAccountOpts(mint=mint, program_id=program_id),
                    )
                    for acc in resp.value:
                        pubkey = str(acc.pubkey)
                        if pubkey in seen:
                            continue
                        parsed = acc.account.data.parsed
                        info = parsed.get("info", {}) if isinstance(parsed, dict) else {}
                        ui_amount = info.get("tokenAmount", {}).get("uiAmount")
                        if ui_amount is not None:
                            seen[pubkey] = float(ui_amount)
                except Exception as e:
                    errors.append(
                        f"getTokenAccountsByOwner({str(program_id)[:12]}): "
                        f"{type(e).__name__}: {e!r}"
                    )

            if seen:
                return sum(seen.values())

            # ---- Path 2: ATA fallback for both programs ----
            for program_id in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
                try:
                    ata, _ = Pubkey.find_program_address(
                        [bytes(owner), bytes(program_id), bytes(mint)],
                        ASSOCIATED_TOKEN_PROGRAM_ID,
                    )
                    bal = await client.get_token_account_balance(ata)
                    if bal.value is not None and bal.value.ui_amount is not None:
                        seen[str(ata)] = float(bal.value.ui_amount)
                except Exception as e:
                    errors.append(
                        f"getTokenAccountBalance(ATA, {str(program_id)[:12]}): "
                        f"{type(e).__name__}: {e!r}"
                    )

        if seen:
            return sum(seen.values())

        # If every RPC path failed with "could not find account", the wallet
        # has simply never held this token — the ATA was never created.
        # That's the normal zero-balance case on a fresh wallet, not an
        # error worth scaring the operator with. Log at DEBUG and return 0.
        # Any other error category (timeouts, rate limits, unexpected RPC
        # faults) still WARNs so real problems surface.
        if errors and all("could not find account" in e.lower() for e in errors):
            logger.debug(
                "Token balance for %s on wallet %s is zero (ATA does not exist yet)",
                mint_address[:8],
                owner_address[:8],
            )
        elif errors:
            logger.warning(
                "Could not read token balance for %s on wallet %s. All RPC paths failed: %s",
                mint_address[:8],
                owner_address[:8],
                " | ".join(errors),
            )
        return 0.0

    async def get_portfolio_value(
        self,
        owner_address: str,
        token_mints: list[str] | None = None,
    ) -> PortfolioSummary:
        """Compute full portfolio value with real prices."""
        from pod_the_trader.trading.dex import SOL_MINT

        sol_balance = await self.get_sol_balance(owner_address)

        try:
            sol_price = await self._dex.get_token_price(SOL_MINT)
        except Exception:
            sol_price = 0.0
            logger.warning("Could not fetch SOL price, using $0")

        sol_value = sol_balance * sol_price

        token_balances: dict[str, float] = {}
        token_values: dict[str, float] = {}

        for mint in token_mints or []:
            balance = await self.get_token_balance(owner_address, mint)
            token_balances[mint] = balance
            if balance > 0:
                try:
                    price = await self._dex.get_token_price(mint)
                    token_values[mint] = balance * price
                except Exception:
                    token_values[mint] = 0.0
            else:
                token_values[mint] = 0.0

        total = sol_value + sum(token_values.values())

        return PortfolioSummary(
            sol_balance=sol_balance,
            sol_value_usd=sol_value,
            token_balances=token_balances,
            token_values_usd=token_values,
            total_value_usd=total,
        )

    def record_trade(self, trade: TradeRecord) -> None:
        """Append a trade to the history file."""
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        history = self._load_history()
        history.append(asdict(trade))
        self._history_path.write_text(json.dumps(history, indent=2))
        logger.info("Recorded %s trade: %s", trade.side, trade.signature)

    def get_trade_history(self, limit: int = 50) -> list[TradeRecord]:
        """Get recent trades from history."""
        history = self._load_history()
        records = [TradeRecord(**entry) for entry in history[-limit:]]
        return records

    def calculate_pnl(self) -> PnLSummary:
        """Compute PnL by pairing buy/sell trades chronologically."""
        history = self._load_history()

        if not history:
            return PnLSummary(
                total_pnl_usd=0.0,
                win_rate=0.0,
                total_trades=0,
                avg_trade_size=0.0,
                largest_win=0.0,
                largest_loss=0.0,
            )

        buys: list[dict] = []
        pnls: list[float] = []
        trade_sizes: list[float] = []

        for entry in history:
            trade_sizes.append(entry.get("value_usd", 0.0))

            if entry["side"] == "buy":
                buys.append(entry)
            elif entry["side"] == "sell" and buys:
                buy = buys.pop(0)
                buy_value = buy.get("value_usd", 0.0)
                sell_value = entry.get("value_usd", 0.0)
                pnls.append(sell_value - buy_value)

        wins = sum(1 for p in pnls if p > 0)
        total_paired = len(pnls)

        return PnLSummary(
            total_pnl_usd=sum(pnls),
            win_rate=(wins / total_paired * 100) if total_paired > 0 else 0.0,
            total_trades=len(history),
            avg_trade_size=sum(trade_sizes) / len(trade_sizes) if trade_sizes else 0.0,
            largest_win=max(pnls, default=0.0),
            largest_loss=min(pnls, default=0.0),
        )

    def _load_history(self) -> list[dict]:
        if not self._history_path.is_file():
            return []
        try:
            return json.loads(self._history_path.read_text())
        except Exception:
            return []
