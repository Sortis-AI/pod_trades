"""Portfolio tracking: balances, trade history, and PnL computation."""

import asyncio
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


def _host_of(url: str) -> str:
    """Short hostname for log messages — keeps lines readable."""
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or url
    except Exception:
        return url


# Phrases that identify "this address has no account" RPC responses.
# The Solana RPC and solders/solana-py wrappers phrase this several
# different ways depending on which code path surfaced the error and
# whether str() or repr() is used to render it; check all of them.
_NOT_FOUND_PHRASES = (
    "could not find account",
    "account not found",
    "accountnotfound",
    "invalid param: could not find account",
)


def _is_account_not_found(e: Exception) -> bool:
    """True when an RPC error means "this address has no on-chain account".

    A fresh wallet that has never held a token raises this from both
    ``getTokenAccountsByOwner`` (when the owner has no account under
    the queried program) and ``getTokenAccountBalance`` (when the ATA
    derived address has never been created). Both are silent-zero
    conditions, not errors worth surfacing to the operator.

    Some wrappers (notably ``solana.rpc.core.SolanaRpcException``)
    render as a bare ``ClassName()`` under ``str()``/``repr()`` but
    stash the real message on ``__cause__`` / ``parent_exception`` /
    ``error_msg`` / ``args``. We walk all of those so the filter
    matches regardless of how the wrapper formatted itself.
    """
    parts: list[str] = [f"{e!s}", f"{e!r}"]

    cause = getattr(e, "__cause__", None)
    if cause is not None and cause is not e:
        parts.append(f"{cause!s}")
        parts.append(f"{cause!r}")

    for attr in ("error_msg", "parent_exception", "message"):
        val = getattr(e, attr, None)
        if val is not None:
            parts.append(str(val))

    for arg in getattr(e, "args", ()) or ():
        parts.append(str(arg))

    haystack = " ".join(parts).lower()
    return any(phrase in haystack for phrase in _NOT_FOUND_PHRASES)


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
        rpc_url: str | list[str],
        jupiter_dex: JupiterDex,
        storage_dir: str = "~/.pod_the_trader",
    ) -> None:
        # Accept a single URL or a prioritized list for failover. Reads
        # walk the list on every attempt so a rate-limited or unhealthy
        # endpoint can be skipped in favor of a working one.
        if isinstance(rpc_url, str):
            self._rpc_urls: list[str] = [rpc_url]
        else:
            self._rpc_urls = [u for u in rpc_url if u]
            if not self._rpc_urls:
                raise ValueError("Portfolio requires at least one rpc_url")
        # Kept for any caller that still reads it (transaction builder,
        # pollers) — the first URL is treated as primary.
        self._rpc_url = self._rpc_urls[0]
        self._dex = jupiter_dex
        self._storage_dir = Path(storage_dir).expanduser()
        self._history_path = self._storage_dir / "trade_history.json"

    async def get_sol_balance(self, address: str) -> float:
        """Get real SOL balance from RPC, with endpoint failover + retry.

        Walks the full RPC list on each pass. A pass that fails every
        endpoint waits with exponential backoff before the next pass.
        Intermediate failures stay at DEBUG — operators only see a
        WARN when every endpoint, every pass has exhausted. On
        exhaustion, raise: a fake zero would corrupt agent decisions.
        """
        pubkey = Pubkey.from_string(address)
        max_passes = 3
        last_error: Exception | None = None

        for attempt in range(max_passes):
            for url in self._rpc_urls:
                try:
                    async with AsyncClient(url) as client:
                        resp = await client.get_balance(pubkey)
                    return resp.value / LAMPORTS_PER_SOL
                except Exception as e:
                    last_error = e
                    logger.debug(
                        "get_sol_balance transient failure for %s via %s (pass %d/%d): %s: %r",
                        address[:8],
                        _host_of(url),
                        attempt + 1,
                        max_passes,
                        type(e).__name__,
                        e,
                    )
            if attempt < max_passes - 1:
                await asyncio.sleep(2**attempt)

        logger.warning(
            "get_sol_balance exhausted all %d endpoint(s) across %d pass(es) "
            "for %s: last error %s: %r",
            len(self._rpc_urls),
            max_passes,
            address[:8],
            type(last_error).__name__,
            last_error,
        )
        assert last_error is not None
        raise last_error

    async def get_token_balance(self, owner_address: str, mint_address: str) -> float:
        """Get SPL token balance for a wallet+mint with endpoint failover.

        Iterates the RPC pool in order. On each endpoint, runs:

        1. ``getTokenAccountsByOwner`` with a mint filter under BOTH the
           legacy Token program and Token-2022 program. Dedupes by token
           account pubkey (the RPC sometimes returns the same account
           under both program filters).
        2. Fallback: derive the Associated Token Account address for both
           program variants and fetch each one directly via
           ``getTokenAccountBalance``. This works even when the bulk
           ``getTokenAccountsByOwner`` call fails (some RPC providers
           rate-limit or return empty bodies on it).

        Errors that say "could not find account" are the expected state
        for a wallet that has never held the token — they are silently
        treated as zero. When Path 2 raises a real RPC error (not just
        "not found"), move on to the next endpoint. Only after every
        endpoint's Path 2 has errored do we WARN and return zero.
        """
        aggregated_path1_errors: list[str] = []
        aggregated_path2_errors: list[str] = []

        for url in self._rpc_urls:
            result, path1_errs, path2_errs = await self._read_token_balance_once(
                url, owner_address, mint_address
            )
            aggregated_path1_errors.extend(path1_errs)
            if result is not None:
                # Either we got a positive balance or Path 2 cleanly
                # reported "not found". Either is authoritative on this
                # endpoint — no need to try another.
                if aggregated_path1_errors:
                    logger.debug(
                        "Path 1 (getTokenAccountsByOwner) noise for %s on wallet %s: %s",
                        mint_address[:8],
                        owner_address[:8],
                        " | ".join(aggregated_path1_errors),
                    )
                return result
            # Path 2 errored on this endpoint — record and try next.
            aggregated_path2_errors.extend(f"[{_host_of(url)}] {e}" for e in path2_errs)

        # Every endpoint's Path 2 errored. This is the one case worth
        # surfacing to the operator.
        if aggregated_path1_errors:
            logger.debug(
                "Path 1 (getTokenAccountsByOwner) noise for %s on wallet %s: %s",
                mint_address[:8],
                owner_address[:8],
                " | ".join(aggregated_path1_errors),
            )
        logger.warning(
            "Could not read token balance for %s on wallet %s across %d endpoint(s): %s",
            mint_address[:8],
            owner_address[:8],
            len(self._rpc_urls),
            " | ".join(aggregated_path2_errors),
        )
        return 0.0

    async def _read_token_balance_once(
        self,
        url: str,
        owner_address: str,
        mint_address: str,
    ) -> tuple[float | None, list[str], list[str]]:
        """Single-endpoint body of ``get_token_balance``.

        Returns ``(balance, path1_errors, path2_errors)``. A non-None
        balance means this endpoint produced an authoritative answer
        (either a positive reading or a clean "not found"). A None
        balance combined with non-empty ``path2_errors`` means the
        caller should try the next endpoint.
        """
        from solana.rpc.types import TokenAccountOpts

        owner = Pubkey.from_string(owner_address)
        mint = Pubkey.from_string(mint_address)

        seen: dict[str, float] = {}
        path1_errors: list[str] = []
        path2_errors: list[str] = []

        async with AsyncClient(url) as client:
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
                    if _is_account_not_found(e):
                        continue
                    path1_errors.append(
                        f"getTokenAccountsByOwner({str(program_id)[:12]}): "
                        f"{type(e).__name__}: {e!r}"
                    )

            if seen:
                return sum(seen.values()), path1_errors, path2_errors

            # ---- Path 2: ATA fallback for both programs ----
            for program_id in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
                try:
                    ata, _ = Pubkey.find_program_address(
                        [bytes(owner), bytes(program_id), bytes(mint)],
                        ASSOCIATED_TOKEN_PROGRAM_ID,
                    )
                    info_resp = await client.get_account_info(ata)
                    if info_resp.value is None:
                        continue  # ATA has never been created — silent zero
                    bal = await client.get_token_account_balance(ata)
                    if bal.value is not None and bal.value.ui_amount is not None:
                        seen[str(ata)] = float(bal.value.ui_amount)
                except Exception as e:
                    if _is_account_not_found(e):
                        continue
                    path2_errors.append(
                        f"getTokenAccountBalance(ATA, {str(program_id)[:12]}): "
                        f"{type(e).__name__}: {e!r}"
                    )

        if seen:
            return sum(seen.values()), path1_errors, path2_errors

        if path2_errors:
            # Path 2 errored — caller should try next endpoint.
            return None, path1_errors, path2_errors

        # Path 2 cleanly reported "not found" — authoritative zero.
        return 0.0, path1_errors, path2_errors

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
