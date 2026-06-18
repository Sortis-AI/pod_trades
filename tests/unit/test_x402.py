"""Tests for the UsePod accountless x402 payment path."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from solders.keypair import Keypair

from pod_the_trader.level5.x402 import (
    SOLANA_MAINNET,
    USDC_MINT,
    PaymentRequired,
    X402CapExceededError,
    X402Error,
    X402Payer,
    X402Transport,
)

PAY_TO = "2wKupLR9q6wXYppw8Gr2NvWxKBUqm4PPJKkQfoxHDBg4"


def _challenge(amount_microunits: int = 1000, quote_id: str = "q1") -> dict:
    return {
        "quote_id": quote_id,
        "asset": USDC_MINT,
        "pay_to": PAY_TO,
        "amount_microunits": amount_microunits,
        "network": SOLANA_MAINNET,
        "mode": "cap-with-surplus-credit",
    }


def _b64_header(d: dict) -> str:
    return base64.b64encode(json.dumps(d).encode()).decode()


class TestPaymentRequiredParsing:
    def test_parses_base64_json(self) -> None:
        pr = PaymentRequired.parse_header(_b64_header(_challenge(2500)))
        assert pr.quote_id == "q1"
        assert pr.pay_to == PAY_TO
        assert pr.amount_microunits == 2500
        assert pr.amount_usdc == pytest.approx(0.0025)

    def test_parses_plain_json_fallback(self) -> None:
        pr = PaymentRequired.parse_header(json.dumps(_challenge(500)))
        assert pr.amount_microunits == 500

    def test_missing_header_raises(self) -> None:
        with pytest.raises(X402Error, match="no PAYMENT-REQUIRED"):
            PaymentRequired.parse_header(None)

    def test_missing_fields_raises(self) -> None:
        bad = _b64_header({"quote_id": "x"})  # no asset/pay_to/amount
        with pytest.raises(X402Error, match="missing/invalid"):
            PaymentRequired.parse_header(bad)


def _payer(per_request: float = 0.50, daily: float = 10.0) -> X402Payer:
    return X402Payer(
        Keypair(),
        rpc_url="https://rpc.example",
        per_request_cap_usdc=per_request,
        max_daily_spend_usdc=daily,
    )


class TestCaps:
    def test_under_caps_passes(self) -> None:
        _payer().check_caps(PaymentRequired(**_challenge(100_000)))  # $0.10

    def test_per_request_cap_blocks(self) -> None:
        # $0.60 > $0.50 cap.
        with pytest.raises(X402CapExceededError, match="per-request cap"):
            _payer().check_caps(PaymentRequired(**_challenge(600_000)))

    def test_daily_cap_blocks(self) -> None:
        p = _payer(per_request=100.0, daily=1.0)
        # First $0.80 is fine; a second $0.80 would breach the $1.00 day cap.
        p._daily.date = p._today()
        p._daily.spent_usdc = 0.80
        with pytest.raises(X402CapExceededError, match="daily spend cap"):
            p.check_caps(PaymentRequired(**_challenge(800_000)))

    def test_daily_counter_resets_next_utc_day(self) -> None:
        p = _payer(per_request=100.0, daily=1.0)
        p._daily.date = "2000-01-01"  # stale day
        p._daily.spent_usdc = 999.0
        # Rolls over to today → counter cleared → charge allowed.
        p.check_caps(PaymentRequired(**_challenge(500_000)))
        assert p.spent_today_usdc == 0.0


class TestSettleRecordsSpend:
    async def test_settle_pays_and_records(self, monkeypatch: pytest.MonkeyPatch) -> None:
        p = _payer()

        async def fake_send(pr):
            return "SIGabc123"

        monkeypatch.setattr(p, "_send_transfer", fake_send)
        payload = await p.settle(PaymentRequired(**_challenge(250_000)))  # $0.25
        assert payload["signature"] == "SIGabc123"
        assert payload["quote_id"] == "q1"
        assert payload["payer_wallet"]  # the wallet pubkey
        assert p.spent_today_usdc == pytest.approx(0.25)

    async def test_settle_over_cap_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        p = _payer()
        called = {"n": 0}

        async def fake_send(pr):
            called["n"] += 1
            return "SIG"

        monkeypatch.setattr(p, "_send_transfer", fake_send)
        with pytest.raises(X402CapExceededError):
            await p.settle(PaymentRequired(**_challenge(600_000)))  # $0.60 > cap
        assert called["n"] == 0  # no funds moved
        assert p.spent_today_usdc == 0.0


class _FakeInner(httpx.AsyncBaseTransport):
    """Queued-response inner transport that records the requests it sees."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses[len(self.requests) - 1]


class _StubPayer:
    def __init__(self) -> None:
        self.calls: list[PaymentRequired] = []

    async def settle(self, pr: PaymentRequired) -> dict[str, str]:
        self.calls.append(pr)
        return {
            "quote_id": pr.quote_id,
            "network": pr.network,
            "asset": pr.asset,
            "payer_wallet": "WALLET",
            "signature": "SIG",
        }


class TestX402Transport:
    async def test_non_402_passes_through_without_payment(self) -> None:
        inner = _FakeInner([httpx.Response(200, json={"ok": True})])
        payer = _StubPayer()
        transport = X402Transport(payer, inner=inner)
        req = httpx.Request("POST", "https://api.usepod.ai/proxy/x402/v1/chat/completions")
        resp = await transport.handle_async_request(req)
        assert resp.status_code == 200
        assert payer.calls == []  # no payment for a non-402
        assert len(inner.requests) == 1

    async def test_402_pays_then_replays_identically(self) -> None:
        challenge = _b64_header(_challenge(250_000))
        inner = _FakeInner(
            [
                httpx.Response(402, headers={"PAYMENT-REQUIRED": challenge}, content=b"pay up"),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        payer = _StubPayer()
        transport = X402Transport(payer, inner=inner)
        body = b'{"model":"x","messages":[]}'
        req = httpx.Request(
            "POST",
            "https://api.usepod.ai/proxy/x402/v1/chat/completions",
            headers={"content-type": "application/json"},
            content=body,
        )
        resp = await transport.handle_async_request(req)

        assert resp.status_code == 200
        assert len(payer.calls) == 1  # paid exactly once
        assert payer.calls[0].amount_microunits == 250_000

        # The replay is byte-identical except for the added header.
        first, second = inner.requests
        assert second.method == first.method == "POST"
        assert str(second.url) == str(first.url)
        assert second.content == body
        assert "PAYMENT-SIGNATURE" in second.headers
        decoded = json.loads(base64.b64decode(second.headers["PAYMENT-SIGNATURE"]))
        assert decoded["signature"] == "SIG"
        assert decoded["quote_id"] == "q1"

    async def test_cap_exceeded_propagates_and_no_retry(self) -> None:
        challenge = _b64_header(_challenge(900_000))
        inner = _FakeInner(
            [httpx.Response(402, headers={"PAYMENT-REQUIRED": challenge}, content=b"x")]
        )
        # Real payer with a low cap so settle raises.
        transport = X402Transport(_payer(per_request=0.10), inner=inner)
        req = httpx.Request("POST", "https://api.usepod.ai/proxy/x402/v1/chat/completions")
        with pytest.raises(X402CapExceededError):
            await transport.handle_async_request(req)
        # Only the initial request was sent — no paid retry.
        assert len(inner.requests) == 1
