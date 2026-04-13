"""Trading agent: LLM interaction loop with tool dispatch."""

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import UTC, datetime

from openai import AsyncOpenAI

from pod_the_trader.agent.memory import ConversationMemory
from pod_the_trader.config import Config
from pod_the_trader.data.ledger import TradeLedger
from pod_the_trader.data.price_log import PriceLog, PriceTick, now_iso
from pod_the_trader.data.wallet_log import WalletLog, WalletSnapshot
from pod_the_trader.level5.client import Level5Client
from pod_the_trader.tools.registry import ToolRegistry
from pod_the_trader.trading.dex import SOL_MINT, JupiterDex
from pod_the_trader.trading.portfolio import Portfolio
from pod_the_trader.tui.publisher import NullPublisher, Publisher

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_BASE = (
    "You are Pod The Trader, an autonomous Solana trading agent.\n\n"
    "Your job is to analyze market conditions and make informed trading "
    "decisions for the configured target token. You have access to tools "
    "for checking prices, getting quotes, executing swaps, and monitoring "
    "your portfolio.\n\n"
    "CRITICAL — Swap sizing:\n"
    "The execute_swap / get_swap_quote / check_swap_feasibility tools take "
    "`amount_in` in UI units of the INPUT token. Examples:\n"
    "  - To buy with 0.1 SOL: input_mint=SOL, amount_in=0.1\n"
    "  - To sell 200000 SQUIRE: input_mint=SQUIRE, amount_in=200000\n"
    "Alternatively use `percent_of_balance` (0-100) to size by fraction of "
    "your on-chain holdings. Example: percent_of_balance=50 sells half.\n"
    "When selling a meaningful position (e.g. taking profit on a big winner), "
    "use percent_of_balance so the sizing is automatically correct — you do "
    "NOT need to know the exact token count.\n"
    "Check get_portfolio_overview or get_token_balance first to see how many "
    "tokens you actually hold before sizing any sell.\n\n"
    "OUTPUT FORMAT — This is important:\n"
    "Keep your response focused. You may include analysis as needed, but you "
    "MUST end your response with EXACTLY ONE summary line in this format:\n"
    "  DECISION: <HOLD|BUY|SELL> — <one-sentence reason under 120 chars>\n"
    "Example:\n"
    "  DECISION: HOLD — Price stable at $0.000159, volatility low, no clear "
    "entry signal.\n"
    "This line is parsed for the user-facing console summary. Be concise.\n\n"
    "Guidelines:\n"
    "- Always check your portfolio and market conditions before trading.\n"
    "- Consider price impact before executing swaps.\n"
    "- Track and report your PnL.\n"
    "- When taking profit on a winning position, size the sell appropriately "
    "(not a token dust amount — use percent_of_balance).\n"
    "- Explain your reasoning for each trading decision."
)


# Primary pattern: strict `DECISION: <ACTION> — <reason>` line (preferred)
_DECISION_STRICT_RE = re.compile(
    r"(?:\*\*)?DECISION(?:\*\*)?\s*:\s*"
    r"(?:\*\*)?(HOLD|BUY|SELL|NO\s*TRADE|WAIT|SKIP)(?:\*\*)?"
    r"\s*[—–\-:]\s*(.+?)(?:\n|$)",
    re.IGNORECASE,
)

# Fallback: `Trading Decision: NO TRADE` or `Decision: HOLD` style headings
# (what older minimax responses used before the prompt was tightened).
_DECISION_LOOSE_RE = re.compile(
    r"(?:Trading\s+)?Decision\s*:\s*"
    r"(?:\*\*)?(HOLD|BUY|SELL|NO\s*TRADE|WAIT|SKIP|BUY\s+MORE|TAKE\s+PROFIT)"
    r"(?:\*\*)?",
    re.IGNORECASE,
)

# Phrase-level fallback — look for action verbs in the response body.
_PHRASE_PATTERNS = [
    (re.compile(r"\bno\s+trade\b", re.IGNORECASE), "HOLD"),
    (re.compile(r"\bhold(?:ing)?\b", re.IGNORECASE), "HOLD"),
    (re.compile(r"\bwait(?:ing)?\b", re.IGNORECASE), "HOLD"),
    (re.compile(r"\btake\s+profit\b", re.IGNORECASE), "SELL"),
    (re.compile(r"\bexit(?:ing)?\b", re.IGNORECASE), "SELL"),
    (re.compile(r"\bsell(?:ing)?\b", re.IGNORECASE), "SELL"),
    (re.compile(r"\bbuy(?:ing)?\b", re.IGNORECASE), "BUY"),
    (re.compile(r"\benter(?:ing)?\s+position\b", re.IGNORECASE), "BUY"),
]


def _normalize_action(raw: str) -> str:
    """Canonicalize a raw action string to HOLD/BUY/SELL."""
    s = re.sub(r"\s+", " ", raw.strip().upper())
    if s in ("HOLD", "WAIT", "SKIP", "NO TRADE"):
        return "HOLD"
    if s in ("BUY", "BUY MORE"):
        return "BUY"
    if s in ("SELL", "TAKE PROFIT"):
        return "SELL"
    return s


def parse_decision(response: str) -> tuple[str, str]:
    """Extract the (action, reason) from a model response.

    Tries three strategies in order:
      1. Strict `DECISION: <ACTION> — <reason>` line (as the prompt requests)
      2. Loose `Trading Decision: NO TRADE` style heading
      3. Phrase-level inference ("no trade" → HOLD, "take profit" → SELL)

    If none match, returns ("UNKNOWN", short-preview of first meaningful line).
    """
    # Strategy 1: strict
    match = _DECISION_STRICT_RE.search(response)
    if match:
        action = _normalize_action(match.group(1))
        reason = match.group(2).strip().rstrip("*_ ").strip()
        return action, reason[:150]

    # Strategy 2: loose heading — find the action and use surrounding text
    match = _DECISION_LOOSE_RE.search(response)
    if match:
        action = _normalize_action(match.group(1))
        # Take the line containing the match + optionally the next line as reason
        lines = response.splitlines()
        reason = ""
        for i, line in enumerate(lines):
            if match.group(0).lower() in line.lower():
                # Try to get useful context from this line or the next
                after = line.split(":", 1)[-1].strip().rstrip("*_ ")
                if len(after) > len(match.group(1)) + 2:
                    reason = after
                elif i + 1 < len(lines):
                    reason = lines[i + 1].strip().lstrip("*_ ").rstrip("*_ ")
                break
        if not reason:
            reason = "(no reason extracted)"
        return action, reason[:150]

    # Strategy 3: phrase-level — search body for action verbs
    for pattern, action in _PHRASE_PATTERNS:
        m = pattern.search(response)
        if m:
            # Find the first non-heading line near the match for a reason
            for line in response.splitlines():
                stripped = line.strip().lstrip("#").lstrip("*").strip()
                if len(stripped) > 20 and not stripped.startswith("|"):
                    return action, stripped[:150]
            return action, "(inferred from phrasing)"

    # Fallback: use the first meaningful line as the reason, mark UNKNOWN
    for raw in response.splitlines():
        line = raw.strip().lstrip("#").lstrip("*").strip()
        if line and len(line) > 5:
            return "UNKNOWN", line[:150]
    return "UNKNOWN", "(no response)"


class TradingAgent:
    """LLM-powered trading agent using OpenAI-compatible chat completions."""

    def __init__(
        self,
        config: Config,
        level5_client: Level5Client,
        tool_registry: ToolRegistry,
        memory: ConversationMemory,
        ledger: TradeLedger | None = None,
        price_log: PriceLog | None = None,
        jupiter_dex: JupiterDex | None = None,
        wallet_log: WalletLog | None = None,
        portfolio: Portfolio | None = None,
        wallet_address: str = "",
        publisher: Publisher | None = None,
    ) -> None:
        self._config = config
        self._level5 = level5_client
        self._registry = tool_registry
        self._memory = memory
        self._ledger = ledger
        self._price_log = price_log
        self._dex = jupiter_dex
        self._wallet_log = wallet_log
        self._portfolio = portfolio
        self._wallet_address = wallet_address
        self._target_symbol: str = ""
        self._target_name: str = ""
        self._publisher: Publisher = publisher or NullPublisher()
        # TUI mode when the publisher is something other than NullPublisher:
        # skip the print() paths and let the TUI render instead.
        self._tui_mode: bool = not isinstance(self._publisher, NullPublisher)
        self._trade_count = 0
        self._cycle_count = 0
        self._last_trade_time: float | None = None

        # Level5 is the only provider
        if not level5_client.is_registered():
            raise ValueError("Level5 registration required — it is the only LLM provider")

        self._client = AsyncOpenAI(
            base_url=level5_client.get_api_base_url(),
            api_key="level5",
        )

    def bootstrap_context(self) -> None:
        """Build a startup summary from ledger + price log and inject it
        into the system prompt via trade_context.
        """
        if self._ledger is None and self._price_log is None:
            return

        parts: list[str] = []

        if self._ledger is not None:
            summary = self._ledger.summary()
            if summary["trade_count"] > 0:
                parts.append(
                    f"All-time ledger ({summary['trade_count']} trades): "
                    f"realized PnL ${summary['realized_pnl_usd']:.4f} "
                    f"({summary['realized_pnl_pct']:.2f}%), "
                    f"win rate {summary['win_rate_pct']:.0f}%, "
                    f"avg buy ${summary['avg_buy_price']:.6f}, "
                    f"avg sell ${summary['avg_sell_price']:.6f}, "
                    f"tokens held {summary['tokens_held']:.2f}, "
                    f"gas ${summary['gas_spent_usd']:.4f}."
                )

        if self._price_log is not None:
            target = self._config.get("trading.target_token_address", "")
            if target:
                ticks = self._price_log.read_for_mint(target)
                if ticks:
                    latest = ticks[-1]
                    vol = self._price_log.volatility(target)
                    parts.append(
                        f"Target token price log: {len(ticks)} ticks, "
                        f"latest ${latest.price_usd:.6f}, "
                        f"volatility {vol:.4f}."
                    )

        if parts:
            self._memory.set_trade_context(" ".join(parts))
            logger.info("Bootstrapped agent context: %s", " ".join(parts))

    def _build_system_prompt(self) -> str:
        """Construct the full system prompt with trade context."""
        parts = [SYSTEM_PROMPT_BASE]

        # Pin critical mint addresses so the model doesn't hallucinate them.
        # SOL's wrapped mint is 43 chars; a single off-by-one break quotes.
        parts.append(
            "\nCritical addresses (copy these EXACTLY, never retype):\n"
            "- SOL (wrapped): So11111111111111111111111111111111111111112\n"
            "- USDC: EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        )

        target = self._config.get("trading.target_token_address", "")
        if target:
            parts.append(f"- TARGET TOKEN: {target}")
            max_pos = self._config.get("trading.max_position_size_usdc")
            parts.append(f"\nMax position size: ${max_pos} USDC")
            parts.append(f"Max slippage: {self._config.get('trading.max_slippage_bps')} bps")

        if self._wallet_address:
            parts.append(
                f"\nYOUR WALLET ADDRESS: {self._wallet_address}\n"
                "All balance/transaction tools (get_solana_balance, "
                "get_spl_token_balance, get_recent_transactions, "
                "get_portfolio_overview, get_token_balance) operate on "
                "THIS wallet automatically. Do NOT pass an address "
                "argument — the tools ignore any address you supply and "
                "always use the wallet above. NEVER invent or guess a "
                "wallet address; if you find yourself typing one that is "
                "not the address above, stop and call the tool with no "
                "arguments instead."
            )

        trade_ctx = self._memory.get_trade_context()
        if trade_ctx:
            parts.append(f"\nRecent trading context:\n{trade_ctx}")

        return "\n".join(parts)

    async def run_turn(self, user_input: str) -> str:
        """Execute a single conversation turn with tool calling.

        Returns the agent's final text response.
        """
        self._memory.add_message("user", user_input)

        model = self._config.get("agent.model", "minimax-m2.7")
        max_tokens = self._config.get("agent.max_tokens", 2048)
        max_iterations = self._config.get("agent.max_iterations_per_turn", 10)
        system_prompt = self._build_system_prompt()
        tools = self._registry.get_all_definitions()

        messages = [
            {"role": "system", "content": system_prompt},
            *self._memory.get_messages(),
        ]

        response = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools or None,
            max_tokens=max_tokens,
        )

        text_parts: list[str] = []
        iterations = 0

        while iterations < max_iterations:
            if not response.choices:
                logger.error(
                    "LLM response has no choices. Raw response: %s",
                    response.model_dump() if hasattr(response, "model_dump") else response,
                )
                text_parts.append("Error: LLM returned an empty response. Check logs for details.")
                break
            choice = response.choices[0]
            msg = choice.message

            # Collect text
            if msg.content:
                text_parts.append(msg.content)

            # Store assistant message in memory
            assistant_msg: dict = {"role": "assistant"}
            if msg.content:
                assistant_msg["content"] = msg.content
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            self._memory.add_message("assistant", assistant_msg)

            # If no tool calls, we're done
            if choice.finish_reason != "tool_calls" or not msg.tool_calls:
                break

            # Execute tool calls and send results back
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    fn_args = {}

                logger.debug("Tool call: %s(id=%s) %s", fn_name, tc.id, fn_args)
                result = await self._registry.execute(fn_name, fn_args)

                self._memory.add_message(
                    "tool",
                    {"role": "tool", "tool_call_id": tc.id, "content": result},
                )

                if fn_name == "execute_swap":
                    self._trade_count += 1
                    self._last_trade_time = time.time()

            # Continue the conversation
            messages = [
                {"role": "system", "content": system_prompt},
                *self._memory.get_messages(),
            ]
            response = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools or None,
                max_tokens=max_tokens,
            )
            iterations += 1

        # Strip tool messages so future turns don't replay stale tool_call_ids
        # (minimax rejects them with "tool id not found").
        self._memory.strip_tool_messages()

        # Summarize if needed
        self._memory.summarize()

        return "\n".join(text_parts) or "No response generated."

    async def trade_loop(self, shutdown_event: asyncio.Event) -> None:
        """Run the autonomous trading cycle."""
        cooldown = self._config.get("trading.cooldown_seconds", 300)
        min_balance = self._config.get("level5.min_balance_threshold_usdc", 2.0)

        # In TUI mode, the orchestrator can't await print_startup_banner
        # before starting the worker (the worker IS the post-mount runtime).
        # Run it inline now so the dashboard gets seeded with real data on
        # the first tick instead of waiting an entire cooldown period.
        if self._tui_mode:
            try:
                await self.print_startup_banner()
            except Exception as e:
                logger.debug("Startup banner publish failed: %s", e)

        logger.info("Starting autonomous trading loop (cooldown: %ds)", cooldown)

        while not shutdown_event.is_set():
            try:
                # Check Level5 balance
                try:
                    balance = await self._level5.get_balance()
                    # Publish the split (USDC vs credits) to any observer.
                    self._publisher.on_level5_balance(
                        self._level5.last_usdc_balance,
                        self._level5.last_credit_balance,
                    )
                    if balance < min_balance:
                        logger.warning(
                            "Level5 balance low: $%.2f (min: $%.2f). Pausing.",
                            balance,
                            min_balance,
                        )
                        await self._wait_or_shutdown(shutdown_event, cooldown)
                        continue
                except Exception as e:
                    logger.error("Failed to check Level5 balance: %s", e)

                # Sample prices for SOL + target token into the price log
                await self._sample_prices()

                # Snapshot on-chain wallet balances
                await self._sample_wallet()

                # Emit cycle-start event to any observer (TUI).
                self._cycle_count += 1
                self._publisher.on_cycle_start(
                    self._cycle_count,
                    datetime.now(UTC).isoformat(),
                )

                # Run a trading analysis turn. Inject an authoritative
                # portfolio snapshot at the top of the prompt so the model
                # cannot carry forward stale "SOL balance 0" beliefs from
                # previous cycles when the wallet has since been refunded.
                snapshot_block = ""
                try:
                    snap = await self._fetch_portfolio_snapshot()
                    snapshot_block = (
                        "AUTHORITATIVE LIVE PORTFOLIO (just fetched, this is ground truth — "
                        "ignore any contradicting numbers in earlier messages):\n"
                        f"  SOL: {snap['sol_ui']:.6f} (${snap['sol_value_usd']:,.4f})\n"
                        f"  target token: {snap['token_ui']:,.4f} "
                        f"(${snap['token_value_usd']:,.4f})\n"
                        f"  total: ${snap['total_usd']:,.4f}\n\n"
                    )
                except Exception as e:
                    logger.debug("Could not fetch live snapshot for prompt: %s", e)

                prompt = (
                    f"{snapshot_block}"
                    "Analyze current market conditions for the target token. "
                    "Check your portfolio, review recent trades, and decide "
                    "whether to make a trade. If trading, get a quote first "
                    "and check feasibility."
                )
                response = await self.run_turn(prompt)

                # Full response goes to the file log only (debug level).
                logger.debug("Full cycle response:\n%s", response)

                # CLI path: print the summary block to stdout.
                # TUI path: publish the structured summary to the dashboard.
                if self._tui_mode:
                    await self._publish_cycle_summary(response, cooldown)
                else:
                    await self._print_cycle_summary(response, cooldown)

                # Save state
                self._memory.save()

            except Exception as e:
                logger.error("Trading cycle error: %s", e, exc_info=True)

            # Wait for cooldown or shutdown
            await self._wait_or_shutdown(shutdown_event, cooldown)

        logger.info("Trading loop stopped. Total trades: %d", self._trade_count)

    async def _wait_or_shutdown(self, shutdown_event: asyncio.Event, seconds: float) -> None:
        """Wait for the specified duration or until shutdown is signaled."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)

    async def fetch_target_metadata(self) -> None:
        """Look up the target token's symbol/name from the Jupiter token list.

        Cached on the agent so other code (e.g. the TUI startup banner) can
        display "SQUIRE" instead of the generic "TARGET" placeholder.
        """
        target = self._config.get("trading.target_token_address", "")
        if not target:
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=httpx.Timeout(15)) as http:
                resp = await http.get(
                    "https://lite-api.jup.ag/tokens/v2/search",
                    params={"query": target},
                )
                resp.raise_for_status()
                for token in resp.json():
                    if token.get("id") == target:
                        self._target_symbol = token.get("symbol", "") or ""
                        self._target_name = token.get("name", "") or ""
                        logger.info(
                            "Target token: %s (%s)",
                            self._target_name or self._target_symbol or target,
                            self._target_symbol,
                        )
                        return
        except Exception as e:
            logger.debug("Could not fetch target metadata: %s", e)

    async def print_startup_banner(self) -> None:
        """Emit a startup summary (print in CLI mode, publish in TUI mode).

        Includes a live on-chain portfolio snapshot with dollar values.
        """
        target = self._config.get("trading.target_token_address", "")
        model = self._config.get("agent.model", "?")
        cooldown = self._config.get("trading.cooldown_seconds", 300)
        ledger_summary = self._ledger.summary() if self._ledger else None

        # Resolve the human ticker for the target token (e.g. SQUIRE)
        if not self._target_symbol:
            await self.fetch_target_metadata()

        # Fetch a snapshot up front either way — TUI wants it, CLI prints it.
        snapshot = None
        try:
            snapshot = await self._fetch_portfolio_snapshot()
        except Exception as e:
            logger.warning("Could not fetch startup portfolio snapshot: %s", e)

        # Fetch Level5 balance up front so the TUI doesn't sit at "no balance"
        # until the first cycle finishes (~300s away).
        try:
            await self._level5.get_balance()
        except Exception as e:
            logger.debug("Startup Level5 balance fetch failed: %s", e)

        # TUI path: publish events and return.
        if self._tui_mode:
            self._publisher.on_startup(
                wallet=self._wallet_address,
                target=target,
                target_symbol=self._target_symbol,
                target_name=self._target_name,
                model=model,
                cooldown=cooldown,
                dashboard_url=self._level5.get_dashboard_url(),
                ledger_summary=ledger_summary,
            )
            if snapshot:
                self._publisher.on_portfolio_snapshot(snapshot)
            self._publisher.on_level5_balance(
                self._level5.last_usdc_balance,
                self._level5.last_credit_balance,
            )
            return

        # CLI path: keep printing the banner as before.
        bar = "━" * 66
        print()
        print(bar)
        print(" 🤖 Pod The Trader — live")
        print(bar)
        print(f"  Wallet:      {self._wallet_address}")
        print(f"  Target:      {target}")
        print(f"  Model:       {model}")
        print(f"  Cycle:       every {cooldown}s")
        if ledger_summary and ledger_summary["trade_count"] > 0:
            s = ledger_summary
            sign = "+" if s["realized_pnl_usd"] >= 0 else ""
            print(
                f"  Ledger:      {s['trade_count']} trades, "
                f"realized {sign}${s['realized_pnl_usd']:.4f}"
            )
        print()
        if snapshot is not None:
            print("  Portfolio (on-chain):")
            print(f"    SOL:       {snapshot['sol_ui']:.6f} (${snapshot['sol_value_usd']:,.4f})")
            if target:
                print(
                    f"    target:    {snapshot['token_ui']:,.4f} "
                    f"@ ${snapshot['token_price_usd']:.8f} "
                    f"= ${snapshot['token_value_usd']:,.4f}"
                )
            print(f"    total:     ${snapshot['total_usd']:,.4f}")
        print(bar)
        print(flush=True)

    async def _fetch_portfolio_snapshot(self) -> dict:
        """Fetch current on-chain SOL + target token balances with USD values.

        Shared by startup banner, per-cycle summary, and post-trade block.
        """
        target = self._config.get("trading.target_token_address", "")
        sol_ui = token_ui = token_value_usd = sol_value_usd = 0.0
        sol_price = token_price = 0.0
        if self._portfolio is not None:
            sol_ui = await self._portfolio.get_sol_balance(self._wallet_address)
            if target:
                token_ui = await self._portfolio.get_token_balance(self._wallet_address, target)
        if self._dex is not None:
            try:
                sol_price = await self._dex.get_token_price(SOL_MINT)
                sol_value_usd = sol_ui * sol_price
            except Exception:
                pass
            if target and token_ui > 0:
                try:
                    token_price = await self._dex.get_token_price(target)
                    token_value_usd = token_ui * token_price
                except Exception:
                    pass
        return {
            "sol_ui": sol_ui,
            "sol_price_usd": sol_price,
            "sol_value_usd": sol_value_usd,
            "token_ui": token_ui,
            "token_price_usd": token_price,
            "token_value_usd": token_value_usd,
            "total_usd": sol_value_usd + token_value_usd,
        }

    def print_portfolio_snapshot(self, snapshot: dict, indent: str = "  ") -> None:
        """Print a compact 3-line portfolio snapshot to stdout."""
        target = self._config.get("trading.target_token_address", "")
        print(f"{indent}SOL:       {snapshot['sol_ui']:.6f} (${snapshot['sol_value_usd']:,.4f})")
        if target:
            print(
                f"{indent}target:    {snapshot['token_ui']:,.4f} "
                f"@ ${snapshot['token_price_usd']:.8f} "
                f"= ${snapshot['token_value_usd']:,.4f}"
            )
        print(f"{indent}total:     ${snapshot['total_usd']:,.4f}")

    async def _print_cycle_summary(self, response: str, cooldown_seconds: float) -> None:
        """Print a clean one-block summary of the cycle to stdout.

        Shows: portfolio snapshot with USD values, decision, short reason,
        running PnL, next-cycle time. Full response is in the log file.
        """
        action, reason = parse_decision(response)

        try:
            snapshot = await self._fetch_portfolio_snapshot()
        except Exception as e:
            logger.debug("Cycle summary balance fetch failed: %s", e)
            snapshot = {
                "sol_ui": 0.0,
                "sol_value_usd": 0.0,
                "token_ui": 0.0,
                "token_price_usd": 0.0,
                "token_value_usd": 0.0,
                "total_usd": 0.0,
            }

        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        bar = "━" * 66
        icon = {"BUY": "📈", "SELL": "📉", "HOLD": "⏸", "UNKNOWN": "❓"}.get(action, "❓")

        print()
        print(bar)
        print(f" Cycle {self._cycle_count}  •  {ts}")
        print(bar)
        print("  Portfolio (on-chain):")
        self.print_portfolio_snapshot(snapshot, indent="    ")
        print(f"  Decision:   {icon} {action}")
        print(f"  Reason:     {reason}")
        if self._ledger is not None:
            s = self._ledger.summary()
            if s["trade_count"] > 0:
                sign = "+" if s["realized_pnl_usd"] >= 0 else ""
                print(
                    f"  PnL:        {sign}${s['realized_pnl_usd']:.4f} "
                    f"on {s['trade_count']} trades "
                    f"(win rate {s['win_rate_pct']:.0f}%)"
                )
        next_min = int(cooldown_seconds // 60)
        next_sec = int(cooldown_seconds % 60)
        print(f"  Next:       in {next_min}:{next_sec:02d}")
        print(bar)
        print(flush=True)

    async def _publish_cycle_summary(self, response: str, cooldown_seconds: float) -> None:
        """Publish a structured cycle summary to the TUI (no stdout output)."""
        action, reason = parse_decision(response)
        try:
            snapshot = await self._fetch_portfolio_snapshot()
        except Exception as e:
            logger.debug("Cycle summary balance fetch failed: %s", e)
            snapshot = {}

        summary = {
            "cycle_num": self._cycle_count,
            "decision": action,
            "reason": reason,
            "portfolio": snapshot,
            "cooldown_seconds": cooldown_seconds,
        }
        self._publisher.on_cycle_complete(summary)
        # Also push a fresh portfolio snapshot event.
        if snapshot:
            self._publisher.on_portfolio_snapshot(snapshot)

    async def _sample_prices(self) -> None:
        """Append a price tick for SOL + target token to the price log."""
        if self._price_log is None or self._dex is None:
            return

        target = self._config.get("trading.target_token_address", "")
        mints = [SOL_MINT]
        if target and target != SOL_MINT:
            mints.append(target)

        for mint in mints:
            try:
                price = await self._dex.get_token_price(mint)
                tick = PriceTick(
                    timestamp=now_iso(),
                    mint=mint,
                    symbol="SOL" if mint == SOL_MINT else "",
                    price_usd=price,
                    source="jupiter_v3",
                )
                self._price_log.append(tick)
            except Exception as e:
                logger.debug("Failed to sample price for %s: %s", mint[:12], e)

    async def _sample_wallet(self) -> None:
        """Snapshot on-chain wallet balances to the wallet log."""
        if self._wallet_log is None or self._portfolio is None or not self._wallet_address:
            return

        target = self._config.get("trading.target_token_address", "")
        try:
            sol_balance = await self._portfolio.get_sol_balance(self._wallet_address)
            sol_price = await self._dex.get_token_price(SOL_MINT) if self._dex is not None else 0.0
            sol_value = sol_balance * sol_price

            token_balance = 0.0
            token_price = 0.0
            if target:
                token_balance = await self._portfolio.get_token_balance(
                    self._wallet_address, target
                )
                if self._dex is not None:
                    try:
                        token_price = await self._dex.get_token_price(target)
                    except Exception:
                        token_price = 0.0

            token_value = token_balance * token_price
            total = sol_value + token_value

            snap = WalletSnapshot(
                timestamp=now_iso(),
                wallet=self._wallet_address,
                sol_balance=sol_balance,
                sol_value_usd=sol_value,
                token_mint=target,
                token_balance=token_balance,
                token_decimals=6,
                token_price_usd=token_price,
                token_value_usd=token_value,
                total_value_usd=total,
            )
            self._wallet_log.append(snap)
        except Exception as e:
            logger.debug("Failed to sample wallet: %s", e)

    @property
    def trade_count(self) -> int:
        return self._trade_count

    @property
    def last_trade_time(self) -> float | None:
        return self._last_trade_time
