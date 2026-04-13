"""Show actual on-chain wallet holdings (SOL + SPL tokens).

Reads the wallet from ~/.pod_the_trader/keypair.json and queries Solana RPC
directly. Also fetches token metadata from Jupiter for decimal info.
"""

import json
import urllib.request
from pathlib import Path

KEYPAIR = Path.home() / ".pod_the_trader" / "keypair.json"
RPC = "https://api.mainnet-beta.solana.com"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def rpc_call(method: str, params: list) -> dict:
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        RPC, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_token_info(mint: str) -> dict | None:
    try:
        url = f"https://lite-api.jup.ag/tokens/v2/search?query={mint}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "pod-the-trader/0.1"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        for token in data:
            if token.get("id") == mint:
                return token
    except Exception:
        return None
    return None


def main() -> None:
    if not KEYPAIR.exists():
        print("No wallet found")
        return

    from solders.keypair import Keypair

    kp = Keypair.from_bytes(bytes(json.loads(KEYPAIR.read_text())))
    addr = str(kp.pubkey())
    print(f"Wallet: {addr}")
    print()

    # SOL balance
    sol_resp = rpc_call("getBalance", [addr])
    lamports = sol_resp.get("result", {}).get("value", 0)
    sol = lamports / 1_000_000_000
    print(f"SOL: {sol:.6f}")
    print()

    # SPL token accounts (both Token and Token-2022 programs)
    print("SPL tokens:")
    seen = set()
    for program in (TOKEN_PROGRAM, TOKEN_2022):
        resp = rpc_call(
            "getTokenAccountsByOwner",
            [
                addr,
                {"programId": program},
                {"encoding": "jsonParsed"},
            ],
        )
        accounts = resp.get("result", {}).get("value", [])
        for acc in accounts:
            info = (
                acc.get("account", {})
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
            )
            mint = info.get("mint", "")
            if mint in seen:
                continue
            seen.add(mint)
            ui_amount = info.get("tokenAmount", {}).get("uiAmount") or 0
            decimals = info.get("tokenAmount", {}).get("decimals", 0)
            raw = info.get("tokenAmount", {}).get("amount", "0")
            if ui_amount == 0:
                continue

            token_info = get_token_info(mint)
            symbol = token_info.get("symbol", "?") if token_info else "?"
            name = token_info.get("name", "") if token_info else ""

            print(f"  {symbol:<10}  {ui_amount:>20,.6f}  decimals={decimals}  raw={raw}")
            print(f"    mint:  {mint}")
            if name:
                print(f"    name:  {name}")
            print()

    if not seen:
        print("  (none)")


if __name__ == "__main__":
    main()
