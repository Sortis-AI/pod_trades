"""Low-level Solana transaction building and submission."""

import logging
import time

from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction, VersionedTransaction

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000


class TransactionError(Exception):
    """Raised on transaction build or submit failure."""


class TransactionBuilder:
    """Builds and submits Solana transactions."""

    def __init__(self, rpc_url: str) -> None:
        self._rpc_url = rpc_url

    async def transfer_sol(
        self,
        keypair: Keypair,
        to_address: str,
        amount_sol: float,
    ) -> str:
        """Transfer SOL using a legacy transaction. Returns the signature string."""
        lamports = int(amount_sol * LAMPORTS_PER_SOL)
        to_pubkey = Pubkey.from_string(to_address)

        ix = transfer(
            TransferParams(
                from_pubkey=keypair.pubkey(),
                to_pubkey=to_pubkey,
                lamports=lamports,
            )
        )

        async with AsyncClient(self._rpc_url) as client:
            blockhash_resp = await client.get_latest_blockhash()
            blockhash = blockhash_resp.value.blockhash

            tx = Transaction.new_signed_with_payer(
                [ix],
                keypair.pubkey(),
                [keypair],
                blockhash,
            )

            result = await client.send_transaction(tx)
            sig = str(result.value)
            logger.info(
                "SOL transfer sent: %.6f SOL to %s (sig: %s)",
                amount_sol,
                to_address,
                sig,
            )
            return sig

    async def confirm_transaction(
        self,
        signature: str,
        timeout: float = 60.0,
    ) -> bool:
        """Wait for a transaction to be confirmed. Returns True if confirmed."""
        async with AsyncClient(self._rpc_url) as client:
            start = time.monotonic()
            from solders.signature import Signature

            sig = Signature.from_string(signature)

            while time.monotonic() - start < timeout:
                resp = await client.get_signature_statuses([sig])
                statuses = resp.value
                if statuses and statuses[0] is not None:
                    if statuses[0].err is None:
                        logger.info("Transaction confirmed: %s", signature)
                        return True
                    else:
                        logger.error(
                            "Transaction failed: %s, error: %s",
                            signature,
                            statuses[0].err,
                        )
                        return False

                import asyncio

                await asyncio.sleep(2)

        logger.warning("Transaction confirmation timed out: %s", signature)
        return False

    async def send_versioned_transaction(
        self,
        tx: VersionedTransaction,
    ) -> str:
        """Send a pre-signed versioned transaction. Returns signature string."""
        async with AsyncClient(self._rpc_url) as client:
            result = await client.send_raw_transaction(bytes(tx))
            sig = str(result.value)
            logger.info("Versioned transaction sent: %s", sig)
            return sig

    async def fetch_swap_details(
        self,
        signature: str,
        owner_address: str,
        output_mint: str,
    ) -> dict:
        """Fetch a confirmed swap and extract gas + actual output amount.

        Returns a dict with: gas_lamports, actual_out_raw, block_slot,
        block_time. Returns zeros / empty if anything fails — enrichment is
        best-effort and the trade is still recorded with quoted values.
        """
        result: dict = {
            "gas_lamports": 0,
            "actual_out_raw": 0,
            "block_slot": 0,
            "block_time": 0,
        }
        try:
            from solders.signature import Signature

            sig = Signature.from_string(signature)
            async with AsyncClient(self._rpc_url) as client:
                resp = await client.get_transaction(
                    sig,
                    encoding="json",
                    max_supported_transaction_version=0,
                )
            tx_data = resp.value
            if tx_data is None:
                return result

            result["block_slot"] = getattr(tx_data, "slot", 0) or 0
            result["block_time"] = getattr(tx_data, "block_time", 0) or 0

            meta = getattr(tx_data.transaction, "meta", None)
            if meta is None:
                return result

            result["gas_lamports"] = int(getattr(meta, "fee", 0) or 0)

            # Compute actual output by diffing token balances of the user's
            # ATA for the output mint, pre vs post.
            pre = getattr(meta, "pre_token_balances", None) or []
            post = getattr(meta, "post_token_balances", None) or []

            def _balance_for(entries) -> int:
                for e in entries:
                    # mint/owner come back as Pubkey objects, not strings —
                    # must wrap in str() to compare against string mints.
                    if (
                        str(getattr(e, "mint", "")) == output_mint
                        and str(getattr(e, "owner", "")) == owner_address
                    ):
                        ui = getattr(e, "ui_token_amount", None)
                        if ui is not None:
                            try:
                                return int(getattr(ui, "amount", 0))
                            except (TypeError, ValueError):
                                return 0
                return 0

            pre_amount = _balance_for(pre)
            post_amount = _balance_for(post)
            result["actual_out_raw"] = max(0, post_amount - pre_amount)
        except Exception as e:
            logger.warning("Failed to enrich swap details for %s: %s", signature[:16], e)
        return result
