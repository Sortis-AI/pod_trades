# Pod The Trader — Full Rewrite Requirements

## 1. Project Overview

Pod The Trader is an autonomous Solana trading agent that uses an LLM (Claude via Anthropic API) to make trading decisions for a configurable SPL token. It self-funds its API costs through Level5.cloud, manages a local Solana wallet, executes swaps via the Jupiter aggregator, and tracks portfolio performance.

This document specifies requirements for a ground-up rewrite of the original `pod_trades` codebase, addressing every known defect and gap.

---

## 2. Goals

- Correct, runnable code from first launch — no placeholder stubs, no broken imports, no API format mismatches.
- Clean separation of concerns with well-defined module boundaries.
- Full test suite: unit tests with mocks, integration tests against test fixtures, and end-to-end smoke tests.
- Proper error handling, logging, and graceful shutdown.
- Configurable for any SPL token, not hardcoded to a single token.

---

## 3. Architecture

```
pod_the_trader/
├── pod_the_trader/              # Main package
│   ├── __init__.py
│   ├── main.py                  # Entry point, orchestrates startup flow
│   ├── config/
│   │   ├── __init__.py          # Config class, get_config(), validation
│   │   └── defaults.yaml        # Default configuration values
│   ├── wallet/
│   │   ├── __init__.py
│   │   ├── manager.py           # Wallet lifecycle (generate, load, save)
│   │   └── setup.py             # Interactive setup wizard
│   ├── level5/
│   │   ├── __init__.py
│   │   ├── auth.py              # Credential storage and interactive auth wizard
│   │   ├── client.py            # Level5 API client (register, balance, proxy URL)
│   │   └── poller.py            # Balance polling and auto-deposit orchestration
│   ├── trading/
│   │   ├── __init__.py
│   │   ├── dex.py               # Jupiter aggregator: quotes, swap tx, execution
│   │   ├── transaction.py       # Low-level Solana transaction building
│   │   └── portfolio.py         # Position tracking, trade history, PnL
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── registry.py          # Tool registry with Anthropic-format definitions
│   │   ├── solana_tools.py      # Solana RPC tools (balance, token info, txns)
│   │   ├── market_tools.py      # Market data tools (price, details, analysis)
│   │   ├── trading_tools.py     # DEX tools (quote, execute, feasibility)
│   │   └── portfolio_tools.py   # Portfolio tools (overview, balance, PnL)
│   └── agent/
│       ├── __init__.py
│       ├── core.py              # Agent loop, LLM interaction, tool dispatch
│       └── memory.py            # Conversation state, persistence, summarization
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # Shared fixtures (mock RPC, mock Level5, mock Jupiter)
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── test_config.py
│   │   ├── test_wallet_manager.py
│   │   ├── test_level5_auth.py
│   │   ├── test_level5_client.py
│   │   ├── test_poller.py
│   │   ├── test_transaction.py
│   │   ├── test_dex.py
│   │   ├── test_portfolio.py
│   │   ├── test_registry.py
│   │   ├── test_tools_solana.py
│   │   ├── test_tools_market.py
│   │   ├── test_tools_trading.py
│   │   ├── test_tools_portfolio.py
│   │   ├── test_agent_core.py
│   │   └── test_agent_memory.py
│   ├── integration/
│   │   ├── __init__.py
│   │   ├── test_startup_flow.py
│   │   ├── test_trade_cycle.py
│   │   └── test_tool_dispatch.py
│   └── e2e/
│       ├── __init__.py
│       └── test_smoke.py
├── pyproject.toml                # Project metadata, dependencies, tool config
├── .env.example                  # Documented env var template
└── .gitignore
```

### 3.1 Key Structural Rules

- Every directory that is a Python package MUST contain an `__init__.py`.
- No module may use `sys.path.insert` hacks. All imports use the `pod_the_trader` package namespace.
- No module-level singletons with mutable global state. Use dependency injection: constructors accept their dependencies, and `main.py` wires them together. A thin `get_X()` accessor may exist for convenience but must be overridable for testing.
- No `from __future__` annotations required — use `typing` imports explicitly so type hints are always valid at runtime.

---

## 4. Module Requirements

### 4.1 `config/`

**Purpose**: Load, validate, and provide typed access to configuration.

**Requirements**:

1. Load `defaults.yaml` from the package directory.
2. Optionally deep-merge a user-provided YAML file on top.
3. Apply environment variable overrides for secrets: `SOLANA_RPC_URL`, `SOLANA_PRIVATE_KEY`, `LEVEL5_API_TOKEN`, `TARGET_TOKEN_ADDRESS`.
4. **Validate at load time**: if `trading.target_token_address` is unset or still a placeholder string (e.g. contains `_HERE`), raise `ConfigError` with an actionable message. Do not silently proceed with a broken config.
5. Expose typed accessors via dot-notation (`config.get("trading.max_position_size_usdc")`) that return the correct Python type.
6. Configuration is immutable after construction. No mutation methods.

**Defects this fixes**:
- Original had placeholder `SQUIRE_TOKEN_ADDRESS_HERE` that silently passed through, causing runtime failures deep in the trading loop.

---

### 4.2 `wallet/`

**Purpose**: Generate, import, load, and persist Solana keypairs.

#### `wallet/manager.py`

1. `WalletManager` class with methods: `generate() -> WalletInfo`, `load() -> Optional[WalletInfo]`, `save(keypair)`, `exists() -> bool`.
2. Storage location: `~/.pod_the_trader/keypair.json`.
3. File permissions set to `0o600` after every write.
4. `WalletInfo` dataclass: `address: str`, `keypair: Keypair`. No raw private key strings stored in memory beyond the `Keypair` object itself.
5. Private key import supports base58, base64, and hex with auto-detection. Validation: decoded bytes must be exactly 32 or 64 bytes.

#### `wallet/setup.py`

1. Interactive CLI wizard with numbered menu.
2. Non-interactive mode: if `SOLANA_PRIVATE_KEY` env var is set, import it directly and skip the wizard. This enables headless/CI operation.
3. Returns a `Keypair` on success, `None` on cancellation.

**Defects this fixes**:
- Original `WalletInfo` carried raw private key strings around in a dataclass field, increasing exposure surface.

---

### 4.3 `level5/`

**Purpose**: Authenticate with Level5, manage credentials, poll for funding, deposit SOL.

#### `level5/auth.py`

1. `Level5Credentials` dataclass: `api_token: str`, `deposit_address: Optional[str]`, `is_new: bool`.
2. `Level5Auth` class: `save(creds)`, `load() -> Optional[Level5Credentials]`, `delete()`, `has_credentials() -> bool`.
3. Storage: `~/.pod_the_trader/level5_credentials.json`, permissions `0o600`.
4. Interactive setup wizard with 3 options: register new, enter existing token, skip.
5. Non-interactive mode: if `LEVEL5_API_TOKEN` env var is set, use it directly.

#### `level5/client.py`

1. `Level5Client` class. Constructor takes `api_token: Optional[str]`, `deposit_address: Optional[str]`.
2. Methods: `register() -> Level5Account`, `get_balance() -> float`, `check_can_afford(cost) -> bool`, `get_anthropic_base_url() -> str`, `is_registered() -> bool`.
3. All HTTP calls use a shared `httpx.AsyncClient` instance (created in `__aenter__`, closed in `__aexit__`). Do NOT create a new `httpx.AsyncClient` per request.
4. Timeout on all HTTP requests: 15 seconds connect, 30 seconds total.
5. Retry with exponential backoff (3 attempts) on 5xx and network errors.

#### `level5/poller.py`

1. `BalancePoller`: polls Solana RPC for wallet SOL balance.
2. `FundingOrchestrator`: coordinates wait-for-funding and auto-deposit-to-Level5.
3. Polling interval configurable (default 10s). Max wait time configurable (default 1 hour) to avoid infinite hangs.
4. Uses `trading/transaction.py` for the deposit transfer.

---

### 4.4 `trading/`

**Purpose**: Build transactions, execute Jupiter swaps, track portfolio.

#### `trading/transaction.py`

1. `TransactionBuilder` class with method `transfer_sol(keypair, to_address, amount_sol) -> str` that returns a transaction signature.
2. Use `solders.system_program.transfer()` to create a transfer instruction — do NOT manually construct `Instruction` objects with raw `data` fields.
3. Use the legacy `Transaction` class with `sign()` and `send_transaction()`, which is simpler and well-tested. Reserve `VersionedTransaction` / `MessageV0` for Jupiter swap transactions that require them.
4. Confirmation: `confirm_transaction(signature, timeout=60) -> bool`.

**Defects this fixes**:
- Original mixed `system_transfer()` output with manual `Instruction` construction, likely producing malformed transactions.

#### `trading/dex.py`

1. `JupiterDex` class for all Jupiter aggregator operations.
2. `get_quote(input_mint, output_mint, amount_lamports, slippage_bps) -> SwapQuote`.
3. `execute_swap(keypair, input_mint, output_mint, amount_lamports, slippage_bps) -> TradeExecution`.
4. `get_token_price(mint_address) -> float`.
5. `check_feasibility(input_mint, output_mint, amount, max_impact_pct) -> FeasibilityResult`.
6. Shared `httpx.AsyncClient` for Jupiter API calls.
7. Retry logic: 3 attempts with backoff on quote/swap-tx fetch.
8. Swap transaction bytes from Jupiter must be base64-decoded before passing to `VersionedTransaction.from_bytes()`.

#### `trading/portfolio.py`

1. `Portfolio` class tracks positions and trade history.
2. `get_sol_balance() -> float` — real SOL balance from RPC.
3. `get_token_balance(mint_address) -> float` — real SPL token balance using associated token account (ATA) lookup. NOT a stub.
4. `get_portfolio_value() -> PortfolioSummary` — fetches real SOL price from Jupiter, enumerates known token positions.
5. `record_trade(trade: TradeRecord)` — appends to `~/.pod_the_trader/trade_history.json`.
6. `calculate_pnl() -> PnLSummary` — computes actual USD PnL by pairing buy/sell trades, not just counting successes. Fields: `total_pnl_usd`, `win_rate`, `total_trades`, `avg_trade_size`, `largest_win`, `largest_loss`.

**Defects this fixes**:
- Original `get_token_balance` was a stub returning an error string.
- Original `get_portfolio_value` used hardcoded `$1.00` SOL price.
- Original `calculate_pnl` never computed actual dollar PnL.

---

### 4.5 `tools/`

**Purpose**: Register callable tools for the LLM agent in the correct Anthropic format.

#### `tools/registry.py`

1. `ToolRegistry` class with methods: `register(name, description, input_schema, handler)`, `get_tool(name)`, `get_all_definitions() -> list[dict]`.
2. **Tool definitions MUST use Anthropic format**:
   ```python
   {
       "name": "tool_name",
       "description": "What the tool does",
       "input_schema": {
           "type": "object",
           "properties": { ... },
           "required": [ ... ]
       }
   }
   ```
   NOT the OpenAI format (`{"type": "function", "function": {...}}`).
3. `execute(name, args) -> str` — calls the handler, returns JSON string. Catches exceptions and returns `{"error": "..."}`.
4. Tool handlers are `async def handler(args: dict) -> dict`.

**Defects this fixes**:
- Original used OpenAI tool definition format, which would cause every Anthropic API call with tools to fail.

#### `tools/solana_tools.py`

1. `get_solana_balance` — returns SOL balance for an address.
2. `get_spl_token_balance` — returns SPL token balance using proper ATA lookup. NOT a stub.
3. `get_token_info` — token metadata from Jupiter token list API.
4. `get_recent_transactions` — recent transaction signatures with timestamps.
5. All tools registered at module level via `registry.register(...)`.

#### `tools/market_tools.py`

1. `get_market_price` — current USD price via Jupiter Price API v4.
2. `get_token_details` — metadata + price combined.
3. `analyze_market_conditions` — price, 24h change direction (compare current vs. price N minutes ago by fetching twice with cache), volume estimate from recent swap activity. Return a structured analysis, not a trivial price-bucket label.
4. `get_target_token_status` — status of the configured target token (replaces hardcoded `get_squire_status`).

#### `tools/trading_tools.py`

1. `get_swap_quote` — Jupiter quote with human-readable summary.
2. `execute_swap` — full swap execution, records trade to portfolio.
3. `check_swap_feasibility` — liquidity and impact check.
4. `get_token_price` — price lookup via Jupiter.

#### `tools/portfolio_tools.py`

1. `get_portfolio_overview` — full portfolio with real token balances and USD values.
2. `get_token_balance` — specific token balance.
3. `get_trade_history` — recent trades from history file.
4. `calculate_pnl` — real PnL computation.

#### Tool Registration Rule

**All tool modules MUST be explicitly imported in `tools/__init__.py`** so that registration happens at import time. The agent module imports `tools` which triggers all registrations.

**Defects this fixes**:
- Original never imported the tool modules, so no tools were registered. The agent had an empty tool set.

---

### 4.6 `agent/`

**Purpose**: LLM-powered trading agent with correct Anthropic API usage.

#### `agent/core.py`

1. `TradingAgent` class. Constructor takes `config`, `level5_client`, `tool_registry`.
2. `run_turn(user_input: str) -> str` — single conversation turn with tool calling.
3. **Anthropic API call format**:
   ```python
   response = await client.messages.create(
       model=config.get("agent.model"),
       system=system_prompt,          # system is a top-level param, NOT in messages
       messages=conversation_messages, # only "user" and "assistant" roles
       tools=registry.get_all_definitions(),
       max_tokens=2048,
   )
   ```
4. **Response parsing**: iterate `response.content` blocks. Each block is either a `TextBlock` (has `.text`) or a `ToolUseBlock` (has `.id`, `.name`, `.input`). Do NOT assume `response.content[0]` is text. Do NOT access `response.tool_use` (it doesn't exist).
5. **Tool result submission**: after executing a tool, send a follow-up message with role `"user"` containing a `tool_result` content block:
   ```python
   {
       "role": "user",
       "content": [
           {
               "type": "tool_result",
               "tool_use_id": tool_use_block.id,
               "content": json.dumps(result)
           }
       ]
   }
   ```
   Then call `messages.create` again to get the assistant's next response. Loop until `response.stop_reason == "end_turn"` or a max iteration limit is reached.
6. **Tool call loop limit**: max 10 tool calls per turn to prevent runaway loops.
7. `trade_loop()` — autonomous 5-minute cycle. Check Level5 balance, run analysis turn, respect cooldown, handle errors gracefully.
8. Graceful shutdown: catch `KeyboardInterrupt`, `asyncio.CancelledError`. Log final state.

**Defects this fixes**:
- Original put `"system"` role in messages array (invalid for Anthropic API).
- Original assumed `response.content[0].text` exists (crashes on tool-use responses).
- Original accessed non-existent `response.tool_use` attribute.
- Original never sent `tool_result` messages back to the API, breaking the tool-call loop.

#### `agent/memory.py`

1. `ConversationMemory` class managing message history.
2. `add_message(role, content)`, `get_messages(limit=20) -> list`, `clear()`.
3. Persistence: save/load conversation state to `~/.pod_the_trader/conversation.json` on each cycle.
4. Summarization: when messages exceed 30, summarize older messages into a single summary message to keep context window manageable.
5. Trade context: maintain a running summary of recent trades and portfolio state that gets injected into the system prompt each cycle.

**Defects this fixes**:
- Original referenced `core/memory.py` in the README but the file didn't exist. All state was in-memory only.

---

### 4.7 `main.py`

**Purpose**: Wire everything together and run the startup flow.

1. All type hints must use imported types. Import `Optional` from `typing`.
2. Startup sequence:
   - Load and validate config (fail fast if token address is placeholder).
   - Run Level5 auth (interactive or from env).
   - Run wallet setup (interactive or from env).
   - Poll for funding.
   - Register with Level5 or validate existing token.
   - Auto-deposit to Level5 if new registration.
   - Import all tool modules (triggers registration).
   - Start trading agent loop.
3. Each step logs clearly what it's doing and what went wrong if it fails.
4. `KeyboardInterrupt` at any point exits cleanly with a message.
5. No `sys.path` manipulation.

**Defects this fixes**:
- Original was missing `from typing import Optional`, causing immediate crash.
- Original never imported tool modules.

---

## 5. Configuration Defaults (`defaults.yaml`)

```yaml
agent:
  name: "Pod The Trader"
  model: "claude-sonnet-4-6-20250514"
  max_iterations_per_turn: 10
  max_tokens: 2048

level5:
  base_url: "https://api.level5.cloud"
  max_daily_spend_usdc: 10.0
  min_balance_threshold_usdc: 2.0

trading:
  target_token_address: ""  # MUST be set by user — validated at startup
  max_position_size_usdc: 100.0
  max_slippage_bps: 50
  min_trade_size_usdc: 1.0
  max_daily_trades: 20
  cooldown_seconds: 300
  max_price_impact_pct: 5.0

solana:
  rpc_url: "https://api.mainnet-beta.solana.com"
  ws_url: "wss://api.mainnet-beta.solana.com"

jupiter:
  quote_url: "https://quote-api.jup.ag/v6"
  price_url: "https://price.jup.ag/v6"
  swap_url: "https://quote-api.jup.ag/v6"

polling:
  funding_interval_seconds: 10
  funding_timeout_seconds: 3600
  balance_monitor_interval_seconds: 30

logging:
  level: "INFO"
  file: "pod_the_trader.log"
  format: "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
  max_bytes: 52428800  # 50 MB
  backup_count: 5

storage:
  base_dir: "~/.pod_the_trader"
```

---

## 6. Environment Variables (`.env.example`)

```bash
# Required: target token mint address to trade
TARGET_TOKEN_ADDRESS=

# Optional: Solana RPC (defaults to public mainnet)
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com

# Optional: import private key non-interactively (base58 encoded)
SOLANA_PRIVATE_KEY=

# Optional: skip Level5 interactive setup
LEVEL5_API_TOKEN=
```

---

## 7. Error Handling and Logging

1. Use Python's `logging` module throughout. Each module gets its own logger: `logger = logging.getLogger(__name__)`.
2. Configure logging in `main.py` at startup based on config values (level, file, format, rotation).
3. Use `RotatingFileHandler` for file output with the configured max size and backup count.
4. Log levels:
   - `DEBUG`: raw API responses, full transaction details.
   - `INFO`: cycle starts, trade decisions, balance updates, tool calls.
   - `WARNING`: low balance, high price impact, retry attempts, configuration fallbacks.
   - `ERROR`: failed transactions, API errors, unexpected exceptions.
   - `CRITICAL`: config validation failure, unrecoverable state.
5. Never print secrets (private keys, full API tokens). Truncate tokens to first 8 characters in logs.
6. All `async` functions that make network calls must have explicit timeout handling. No indefinite awaits.

---

## 8. Graceful Shutdown

1. Register a signal handler for `SIGINT` and `SIGTERM` in `main.py`.
2. On shutdown signal:
   - Set a `shutdown_event` (`asyncio.Event`).
   - The trading loop checks `shutdown_event` before each cycle.
   - Persist conversation memory to disk.
   - Log final portfolio state and trade count.
   - Close all HTTP clients.
   - Exit with code 0.
3. If a second signal is received during shutdown, force-exit immediately.

---

## 9. Test Suite

### 9.1 Tooling

- Test runner: `pytest` with `pytest-asyncio` for async tests.
- Mocking: `unittest.mock` and `pytest-mock`.
- Coverage: `pytest-cov`, target 90%+ line coverage on non-IO code.
- HTTP mocking: `respx` for mocking `httpx` requests.
- Fixtures: shared in `tests/conftest.py`.

### 9.2 Shared Fixtures (`conftest.py`)

```python
# Fixtures to provide:

@pytest.fixture
def tmp_storage(tmp_path):
    """Temporary storage directory replacing ~/.pod_the_trader."""

@pytest.fixture
def sample_config(tmp_path):
    """Config instance with test-safe defaults (devnet RPC, test token address)."""

@pytest.fixture
def mock_keypair():
    """A deterministic Solana Keypair for testing."""

@pytest.fixture
def mock_level5_client():
    """Level5Client with mocked HTTP responses."""

@pytest.fixture
def mock_rpc_client():
    """Mocked Solana AsyncClient with canned balance/tx responses."""

@pytest.fixture
def mock_jupiter_api(respx_mock):
    """Mocked Jupiter quote/swap/price endpoints."""

@pytest.fixture
def populated_registry():
    """ToolRegistry with all tools registered."""

@pytest.fixture
def sample_trade_history(tmp_path):
    """Pre-populated trade_history.json for PnL tests."""
```

### 9.3 Unit Tests

Each unit test file covers one module. Tests are fast (no network, no disk unless via `tmp_path`).

#### `test_config.py`
- Loads defaults successfully.
- Deep-merges user config over defaults.
- Environment variables override config values.
- Raises `ConfigError` if `target_token_address` is empty or placeholder.
- `get()` returns correct types and defaults for missing keys.

#### `test_wallet_manager.py`
- `generate()` creates a keypair and saves to disk.
- `load()` reads back the saved keypair correctly.
- `load()` returns `None` if no file exists.
- Saved file has `0o600` permissions.
- Import from base58 string produces correct public key.
- Import from base64 string produces correct public key.
- Import from hex string produces correct public key.
- Rejects invalid key material (wrong length, bad encoding).

#### `test_level5_auth.py`
- `save()` writes credentials to disk with `0o600` permissions.
- `load()` reads back credentials correctly.
- `load()` returns `None` if no file exists.
- `delete()` removes the file.
- `has_credentials()` reflects file existence.

#### `test_level5_client.py`
- `register()` sends POST to `/v1/register` and parses response.
- `get_balance()` sends GET and returns float.
- `check_can_afford()` returns `True` when balance is sufficient.
- `check_can_afford()` returns `False` when balance is below threshold.
- `get_anthropic_base_url()` returns correct proxy URL.
- `is_registered()` returns `False` before registration, `True` after.
- HTTP errors raise appropriate exceptions.
- Retries on 5xx responses.

#### `test_poller.py`
- `get_balance()` returns lamports converted to SOL.
- `poll_until_funded()` returns when balance meets threshold.
- `poll_until_funded()` times out after configured limit.
- Balance change callback is invoked.

#### `test_transaction.py`
- `transfer_sol()` constructs a valid transaction (mock RPC).
- Amount conversion: SOL to lamports is correct.
- Destination address is parsed correctly.
- `confirm_transaction()` returns `True` on finalized status.
- `confirm_transaction()` returns `False` on timeout.

#### `test_dex.py`
- `get_quote()` parses Jupiter response into `SwapQuote`.
- `get_quote()` retries on failure.
- `get_quote()` raises after max retries.
- `execute_swap()` returns `TradeExecution` with `success=True` on happy path.
- `execute_swap()` returns `TradeExecution` with `success=False` and error on failure.
- `get_token_price()` returns float from Jupiter price API.
- `check_feasibility()` returns feasible when impact is below threshold.
- `check_feasibility()` returns not feasible when impact exceeds threshold.

#### `test_portfolio.py`
- `get_sol_balance()` returns correct SOL amount.
- `get_token_balance()` computes ATA and returns parsed balance.
- `record_trade()` appends to history file.
- `get_trade_history()` returns last N trades.
- `calculate_pnl()` on empty history returns zeros.
- `calculate_pnl()` correctly computes USD PnL from buy/sell pairs.
- `calculate_pnl()` computes win rate correctly.

#### `test_registry.py`
- `register()` stores a tool by name.
- `get_tool()` returns `None` for unregistered name.
- `get_all_definitions()` returns list in Anthropic format (has `name`, `description`, `input_schema` keys at top level; does NOT have `type: "function"` or nested `function` key).
- `execute()` calls handler and returns JSON.
- `execute()` catches handler exceptions and returns error JSON.

#### `test_tools_solana.py`
- `get_solana_balance` returns correct SOL balance.
- `get_spl_token_balance` returns token amount via ATA lookup.
- `get_recent_transactions` returns transaction list.

#### `test_tools_market.py`
- `get_market_price` returns price dict with `price_usd` field.
- `get_token_details` returns merged metadata + price.
- `analyze_market_conditions` returns structured analysis (not just a string label).
- `get_target_token_status` returns error dict when token not configured.
- `get_target_token_status` returns price when token is configured.

#### `test_tools_trading.py`
- `get_swap_quote` returns quote with summary.
- `execute_swap` calls Jupiter and returns success result.
- `execute_swap` records trade to portfolio.
- `check_swap_feasibility` returns feasibility assessment.
- Missing parameters return error dict.

#### `test_tools_portfolio.py`
- `get_portfolio_overview` returns holdings with real values.
- `get_token_balance` returns balance for given mint.
- `get_trade_history` returns capped trade list.
- `calculate_pnl` returns PnL summary with USD amounts.

#### `test_agent_core.py`
- System prompt is passed as `system` parameter, not in `messages`.
- Messages array only contains `user` and `assistant` roles.
- Text-only response is parsed correctly.
- Tool-use response triggers tool execution and sends `tool_result`.
- Multi-tool response executes all tools sequentially.
- Tool call loop terminates at max iterations.
- Trade count and last-trade timestamp update on successful swap.
- Low Level5 balance triggers warning, not crash.

#### `test_agent_memory.py`
- Messages are stored and retrievable.
- `get_messages(limit=N)` returns last N messages.
- Persistence: save then load roundtrips correctly.
- Summarization: messages over threshold are condensed.
- `clear()` empties all messages.

### 9.4 Integration Tests

These tests wire multiple modules together with mocked external services.

#### `test_startup_flow.py`
- Config loads, wallet loads from fixture, Level5 client initializes with mocked token.
- Tool modules are imported and all expected tools are registered.
- Agent is constructed with real config, mocked Level5, and populated registry.

#### `test_trade_cycle.py`
- Simulate one full trade cycle:
  1. Mock Level5 balance check returns $10.
  2. Mock Anthropic API returns a tool_use response for `get_swap_quote`.
  3. Mock Jupiter returns a quote.
  4. Mock Anthropic API returns a tool_use response for `execute_swap`.
  5. Mock Jupiter returns swap transaction bytes.
  6. Mock Solana RPC confirms transaction.
  7. Mock Anthropic API returns final text response.
- Verify: trade recorded in portfolio, trade count incremented, no errors.

#### `test_tool_dispatch.py`
- Register a mock tool, call `agent.run_turn()` with mocked Anthropic response that requests that tool.
- Verify tool was called with correct args.
- Verify `tool_result` was sent back in the follow-up API call.

### 9.5 End-to-End Smoke Test

#### `test_smoke.py`
- Import `pod_the_trader` package — no import errors.
- Construct `Config` with test defaults — no validation errors (uses a valid test token address).
- Construct `ToolRegistry`, import all tool modules — all expected tools registered (verify by name).
- Construct `TradingAgent` with mocked dependencies — no errors.
- Call `agent.run_turn("What is the current price?")` with fully mocked externals — returns a string response.

---

## 10. Dependency Management (`pyproject.toml`)

Use `pyproject.toml` with a `[project]` table (PEP 621). No `setup.py`.

### Runtime Dependencies
```
anthropic >= 0.30.0
httpx >= 0.27.0
pyyaml >= 6.0
solana >= 0.32.0
solders >= 0.21.0
base58 >= 2.1.0
spl-token >= 0.4.0
python-dotenv >= 1.0.0
```

### Dev Dependencies (in `[project.optional-dependencies]` under `dev`)
```
pytest >= 8.0
pytest-asyncio >= 0.23.0
pytest-cov >= 5.0
pytest-mock >= 3.12
respx >= 0.21.0
```

---

## 11. Conventions and Code Quality

1. **Type hints on all function signatures.** Return types included.
2. **Docstrings on all public classes and methods.** One-liner for simple ones, Google-style for complex.
3. **No bare `except:` clauses.** Always catch specific exceptions. At minimum, catch `Exception` — never silence `KeyboardInterrupt` or `SystemExit`.
4. **No `print()` in library code.** Use `logger.info()` etc. Only `main.py` and interactive setup wizards may use `print()`.
5. **Async consistency.** All I/O-bound operations are `async`. Sync wrappers only at the top-level entry point (`asyncio.run()`).
6. **HTTP client lifecycle.** `httpx.AsyncClient` instances are created once and reused. Use async context managers or explicit close in shutdown.
7. **No mutable class-level defaults.** The original `ToolRegistry` used `_tools: Dict[str, Tool] = {}` as a class variable, meaning all instances shared state. Use instance variables initialized in `__init__`.

---

## 12. Security Considerations

1. Private keys stored in files with `0o600` permissions only.
2. Private keys never logged, never included in error messages.
3. API tokens truncated to 8 characters in all log output.
4. `.gitignore` must include: `*.json` in storage dirs, `.env`, `*.log`, `__pycache__`, `.pytest_cache`.
5. No secrets in `defaults.yaml` — all secrets come from env vars or interactive input.
6. Level5 API token is passed in URL path (per Level5's design). Log sanitization must strip tokens from logged URLs.

---

## 13. Checklist Before Shipping

- [ ] `python -m pod_the_trader` runs without import errors
- [ ] Config validation rejects placeholder token address
- [ ] All tool modules imported at startup, all tools registered
- [ ] Anthropic API calls use correct message format (system as top-level param)
- [ ] Tool definitions use Anthropic format (not OpenAI format)
- [ ] Tool-use response loop sends `tool_result` messages correctly
- [ ] Response content block iteration handles `TextBlock` and `ToolUseBlock`
- [ ] `pytest` passes with 0 failures
- [ ] `pytest --cov` shows 90%+ coverage on non-IO code
- [ ] No `print()` in library modules (only in interactive wizards and main)
- [ ] Logging configured with rotation
- [ ] Graceful shutdown on SIGINT/SIGTERM
- [ ] `.env.example` exists with all documented variables
- [ ] `.gitignore` excludes secrets and build artifacts
- [ ] No `sys.path` hacks
- [ ] Every package directory has `__init__.py`
