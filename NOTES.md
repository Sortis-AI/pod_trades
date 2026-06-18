# NOTES

Project-specific discoveries, adversity, and non-obvious config. Things the next-session me will want.

## UsePod accountless x402 inference (added 0.3.18)

A third provider, `usepod-x402`, pays for inference per-request from the local Solana wallet over the x402 protocol — no account, token, or dashboard. Key facts that aren't obvious from the code alone:

- **Endpoint:** `POST https://api.usepod.ai/proxy/x402/v1/chat/completions`. The literal `x402` path segment replaces the account token; `ProviderConfig.proxy_token = "x402"` makes `Level5Client.get_api_base_url()` build the right URL with no special-casing.
- **Flow** (`pod_the_trader/level5/x402.py`): request → `402` with a `PAYMENT-REQUIRED` header (base64 JSON: `quote_id`, `asset`, `pay_to`, `amount_microunits`, `network`, `mode`) → pay USDC on-chain to `pay_to` → retry the **byte-identical** request with a `PAYMENT-SIGNATURE` header (base64 JSON: `quote_id`, `network`, `asset`, `payer_wallet`, `signature`). The request is bound to a hash of method+path+body, so the retry must differ only by that one header.
- **Why a custom httpx transport:** the OpenAI SDK won't do the 402→pay→retry dance, and the byte-identical-replay rule means we must intercept at the layer that holds the exact serialized request. `X402Transport` (an `httpx.AsyncBaseTransport`) is passed as the SDK's `http_client`; it replays `request.content` verbatim. Do NOT move this into the SDK subclass or a higher layer — you'll lose the exact body.
- **Accountless seams in `Level5Client`:** `is_registered()` is True with no token; `get_balance()` reads the wallet's on-chain USDC via an injected `balance_reader` (wired in `main.py` *after* the Portfolio exists, via `set_balance_reader` — the client is constructed before the Portfolio); `get_dashboard_url()` returns `""`. Registration/funding-wait in `main.py` is skipped because accountless creds have `is_new=False`.

### ⚠️ Unverified against a live endpoint
The on-chain settlement (pay_to treated as **owner** → derive ATA; `TransferChecked`; 6-decimal USDC; no memo) follows the **canonical Coinbase exact-SVM spec**, because UsePod's public docs gave the header/flow shape but not the exact transfer construction. The genuinely unverified points, any of which could mean a payment the server won't credit:
- `pay_to` = owner vs. already-an-ATA (we derive ATA from owner).
- `TransferChecked` vs. `Transfer`, and whether a Memo instruction carrying `quote_id` is required.
- Whether `mode: cap-with-surplus-credit` means one payment buys credit for multiple subsequent calls (we currently pay per 402).

**Before trusting it for sustained spend:** run one live call and confirm the inference returns 200 after payment. The `per_request_cap_usdc` (default $0.50) bounds the loss if a payment isn't credited. If UsePod's `@usepod/sdk` source becomes available, diff our `X402Payer._send_transfer` against it.

### Safety caps
Both editable in the `usepod-x402` config section: `per_request_cap_usdc` (reject oversized quote, abort cycle) and `max_daily_x402_spend_usdc` (pause inference at the UTC-day ceiling). Caps are checked in `X402Payer.check_caps` **before** any funds move.
