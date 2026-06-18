# NOTES

Project-specific discoveries, adversity, and non-obvious config. Things the next-session me will want.

## UsePod accountless x402 inference (added 0.3.18)

A third provider, `usepod-x402`, pays for inference per-request from the local Solana wallet over the x402 protocol — no account, token, or dashboard. Key facts that aren't obvious from the code alone:

- **Endpoint:** `POST https://api.usepod.ai/proxy/x402/v1/chat/completions`. The literal `x402` path segment replaces the account token; `ProviderConfig.proxy_token = "x402"` makes `Level5Client.get_api_base_url()` build the right URL with no special-casing.
- **Flow** (`pod_the_trader/level5/x402.py`): request → `402` with a `PAYMENT-REQUIRED` header (base64 JSON: `quote_id`, `asset`, `pay_to`, `amount_microunits`, `network`, `mode`) → pay USDC on-chain to `pay_to` → retry the **byte-identical** request with a `PAYMENT-SIGNATURE` header (base64 JSON: `quote_id`, `network`, `asset`, `payer_wallet`, `signature`). The request is bound to a hash of method+path+body, so the retry must differ only by that one header.
- **Why a custom httpx transport:** the OpenAI SDK won't do the 402→pay→retry dance, and the byte-identical-replay rule means we must intercept at the layer that holds the exact serialized request. `X402Transport` (an `httpx.AsyncBaseTransport`) is passed as the SDK's `http_client`; it replays `request.content` verbatim. Do NOT move this into the SDK subclass or a higher layer — you'll lose the exact body.
- **Accountless seams in `Level5Client`:** `is_registered()` is True with no token; `get_balance()` reads the wallet's on-chain USDC via an injected `balance_reader` (wired in `main.py` *after* the Portfolio exists, via `set_balance_reader` — the client is constructed before the Portfolio); `get_dashboard_url()` returns `""`. Registration/funding-wait in `main.py` is skipped because accountless creds have `is_new=False`.

### Settlement construction — verified against the docs.usepod.ai x402 example
Checked our `X402Payer._send_transfer` against the published example. All match:
- `pay_to` is a **wallet owner** → derive its ATA. Doc: `ata(Pubkey.from_string(rail["pay_to"]), USDC)`. We do the same. (NOT a direct token account.)
- **`transfer_checked`** (SPL TransferChecked), 6-decimal USDC, **no Memo** instruction.
- `PAYMENT-SIGNATURE` = base64 of `{quote_id, network, asset, payer_wallet, signature}` — exactly what `settle()` builds. Note `asset` is the **symbol** string (`"USDC"`), not the mint; we echo whatever the 402 sent, and the transfer itself uses the hardcoded USDC mint, so this is robust.

### `cap-with-surplus-credit` — one payment funds many calls
The gateway charges actual token usage and credits the unused remainder (`cap − actual`) to a **wallet balance keyed to `payer_wallet`**, applied to future x402 requests from the same wallet. So:
- We pay only when a `402` actually arrives. Once a payment establishes credit, subsequent calls are covered and return 200 with no 402 — the pay-on-402 transport handles this naturally and pays infrequently.
- `amount_microunits` on a 402 is likely the **cap pre-authorization**, not the tiny per-call cost. The full cap amount leaves the wallet on-chain; the surplus is recoverable only as future *inference credit* (not a refund). So capping the transfer is still the right wallet-safety control — but **`per_request_cap_usdc` must be ≥ the gateway's cap quote**, or every first payment is rejected and the bot never gets inference. If x402 calls always abort on the per-request cap, raise it to cover one cap quote.

### Safety caps
Both editable in the `usepod-x402` config section: `per_request_cap_usdc` (reject oversized quote, abort cycle) and `max_daily_x402_spend_usdc` (pause inference at the UTC-day ceiling). Caps are checked in `X402Payer.check_caps` **before** any funds move.
