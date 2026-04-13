"""Tests for pod_the_trader.trading.transaction."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.signature import Signature as SoldersSig

from pod_the_trader.trading.transaction import LAMPORTS_PER_SOL, TransactionBuilder

# A valid base58-encoded 64-byte signature for testing
FAKE_SIG = str(SoldersSig.default())


@pytest.fixture()
def builder() -> TransactionBuilder:
    return TransactionBuilder(rpc_url="https://api.devnet.solana.com")


@pytest.fixture()
def keypair() -> Keypair:
    return Keypair.from_seed(bytes(range(32)))


class TestTransferSol:
    async def test_constructs_and_sends_transaction(
        self, builder: TransactionBuilder, keypair: Keypair
    ) -> None:
        mock_client = AsyncMock()
        mock_blockhash_resp = MagicMock()
        mock_blockhash_resp.value.blockhash = Hash.default()
        mock_client.get_latest_blockhash = AsyncMock(return_value=mock_blockhash_resp)

        mock_send_resp = MagicMock()
        mock_send_resp.value = FAKE_SIG
        mock_client.send_transaction = AsyncMock(return_value=mock_send_resp)

        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "pod_the_trader.trading.transaction.AsyncClient",
            return_value=mock_client,
        ):
            sig = await builder.transfer_sol(keypair, "11111111111111111111111111111111", 1.5)

        assert sig == FAKE_SIG
        mock_client.send_transaction.assert_called_once()

    def test_amount_conversion(self) -> None:
        assert int(1.5 * LAMPORTS_PER_SOL) == 1_500_000_000
        assert int(0.001 * LAMPORTS_PER_SOL) == 1_000_000


class TestConfirmTransaction:
    async def test_returns_true_on_confirmed(self, builder: TransactionBuilder) -> None:
        mock_client = AsyncMock()
        mock_status = MagicMock()
        mock_status.err = None
        mock_resp = MagicMock()
        mock_resp.value = [mock_status]
        mock_client.get_signature_statuses = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "pod_the_trader.trading.transaction.AsyncClient",
            return_value=mock_client,
        ):
            result = await builder.confirm_transaction(FAKE_SIG)

        assert result is True

    async def test_returns_false_on_error(self, builder: TransactionBuilder) -> None:
        mock_client = AsyncMock()
        mock_status = MagicMock()
        mock_status.err = {"InstructionError": [0, "Custom"]}
        mock_resp = MagicMock()
        mock_resp.value = [mock_status]
        mock_client.get_signature_statuses = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "pod_the_trader.trading.transaction.AsyncClient",
            return_value=mock_client,
        ):
            result = await builder.confirm_transaction(FAKE_SIG)

        assert result is False

    async def test_returns_false_on_timeout(self, builder: TransactionBuilder) -> None:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.value = [None]
        mock_client.get_signature_statuses = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "pod_the_trader.trading.transaction.AsyncClient",
            return_value=mock_client,
        ):
            result = await builder.confirm_transaction(FAKE_SIG, timeout=0.1)

        assert result is False
