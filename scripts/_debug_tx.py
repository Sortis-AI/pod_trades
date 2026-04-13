"""Debug: dump the raw structure of the legacy trade's transaction."""

import asyncio

from solana.rpc.async_api import AsyncClient
from solders.signature import Signature

SIG = "4Duit1uL7zhiofJvCASGSaUbg5qNnxpz7xrwty8SxgjNJgj2ExryCi2srVV2YxL6s6Le8ZkphWoTtVLqGwk74kwD"
RPC = "https://api.mainnet-beta.solana.com"


async def main() -> None:
    async with AsyncClient(RPC) as client:
        sig = Signature.from_string(SIG)
        resp = await client.get_transaction(
            sig, encoding="json", max_supported_transaction_version=0
        )
        tx_data = resp.value
        print(f"slot: {tx_data.slot}")
        print(f"block_time: {tx_data.block_time}")

        meta = tx_data.transaction.meta
        print(f"fee: {meta.fee}")
        print()

        print("=== pre_token_balances ===")
        for b in (meta.pre_token_balances or []):
            print(f"  account_index={b.account_index}")
            print(f"  mint={b.mint}")
            print(f"  owner={b.owner}")
            print(f"  ui_token_amount.amount={b.ui_token_amount.amount}")
            print(f"  ui_token_amount.decimals={b.ui_token_amount.decimals}")
            print(f"  ui_token_amount.ui_amount={b.ui_token_amount.ui_amount}")
            print()

        print("=== post_token_balances ===")
        for b in (meta.post_token_balances or []):
            print(f"  account_index={b.account_index}")
            print(f"  mint={b.mint}")
            print(f"  owner={b.owner}")
            print(f"  ui_token_amount.amount={b.ui_token_amount.amount}")
            print(f"  ui_token_amount.decimals={b.ui_token_amount.decimals}")
            print(f"  ui_token_amount.ui_amount={b.ui_token_amount.ui_amount}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
