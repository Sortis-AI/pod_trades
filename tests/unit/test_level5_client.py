"""Tests for pod_the_trader.level5.client."""

import httpx
import pytest
import respx

from pod_the_trader.level5.client import (
    Level5Client,
    Level5Error,
    _sanitize_url,
)

BASE_URL = "https://api.level5.cloud"
TEST_TOKEN = "test_token_abcdef123456"


@pytest.fixture()
async def client():
    async with Level5Client(
        api_token=TEST_TOKEN,
        deposit_address="DepAddr123",
        base_url=BASE_URL,
    ) as c:
        yield c


class TestRegister:
    @respx.mock
    async def test_register_success(self) -> None:
        respx.post(f"{BASE_URL}/v1/register").mock(
            return_value=httpx.Response(
                200,
                json={
                    "api_token": "new_token_xyz",
                    "deposit_address": "NewDepAddr",
                    "balance_usdc": 0.0,
                },
            )
        )
        async with Level5Client(base_url=BASE_URL) as client:
            account = await client.register()
        assert account.api_token == "new_token_xyz"
        assert account.deposit_address == "NewDepAddr"
        assert account.balance_usdc == 0.0

    @respx.mock
    async def test_register_missing_deposit_address_raises_level5_error(self) -> None:
        """Regression test: Level5's /v1/register response has been seen in
        production without a ``deposit_address`` key. The original code did
        ``data["deposit_address"]`` and crashed the whole startup flow with
        an unhandled ``KeyError``. The client must detect the missing field
        and raise a ``Level5Error`` with a clear message instead.
        """
        respx.post(f"{BASE_URL}/v1/register").mock(
            return_value=httpx.Response(
                200,
                json={
                    "api_token": "new_token_xyz",
                    # deposit_address intentionally absent
                    "balance_usdc": 0.0,
                },
            )
        )
        async with Level5Client(base_url=BASE_URL) as client:
            with pytest.raises(Level5Error) as exc:
                await client.register()
        msg = str(exc.value).lower()
        assert "deposit_address" in msg or "deposit address" in msg

    @respx.mock
    async def test_register_missing_api_token_raises_level5_error(self) -> None:
        """Symmetric case: no api_token in the response."""
        respx.post(f"{BASE_URL}/v1/register").mock(
            return_value=httpx.Response(
                200,
                json={
                    "deposit_address": "NewDepAddr",
                    "balance_usdc": 0.0,
                },
            )
        )
        async with Level5Client(base_url=BASE_URL) as client:
            with pytest.raises(Level5Error) as exc:
                await client.register()
        assert "api_token" in str(exc.value).lower()

    @respx.mock
    async def test_register_empty_deposit_address_raises_level5_error(self) -> None:
        """Empty string is as unusable as missing — the wallet can't be
        funded without a real deposit address, so the whole flow should
        halt with a clear error.
        """
        respx.post(f"{BASE_URL}/v1/register").mock(
            return_value=httpx.Response(
                200,
                json={
                    "api_token": "new_token_xyz",
                    "deposit_address": "",
                    "balance_usdc": 0.0,
                },
            )
        )
        async with Level5Client(base_url=BASE_URL) as client:
            with pytest.raises(Level5Error):
                await client.register()

    @respx.mock
    async def test_register_null_deposit_address_raises_level5_error(self) -> None:
        """JSON null → Python None. Same treatment as missing/empty."""
        respx.post(f"{BASE_URL}/v1/register").mock(
            return_value=httpx.Response(
                200,
                json={
                    "api_token": "new_token_xyz",
                    "deposit_address": None,
                    "balance_usdc": 0.0,
                },
            )
        )
        async with Level5Client(base_url=BASE_URL) as client:
            with pytest.raises(Level5Error):
                await client.register()


class TestGetBalance:
    @respx.mock
    async def test_returns_balance_from_endpoint(self, client: Level5Client) -> None:
        # usdc_balance + credit_balance in microunits
        respx.get(f"{BASE_URL}/proxy/{TEST_TOKEN}/balance").mock(
            return_value=httpx.Response(
                200,
                json={
                    "usdc_balance": 5750000,
                    "credit_balance": 0,
                    "is_active": True,
                },
            )
        )
        balance = await client.get_balance()
        assert balance == 5.75

    @respx.mock
    async def test_includes_credits_in_balance(self, client: Level5Client) -> None:
        respx.get(f"{BASE_URL}/proxy/{TEST_TOKEN}/balance").mock(
            return_value=httpx.Response(
                200,
                json={
                    "usdc_balance": 845045,
                    "credit_balance": 200000,
                    "is_active": True,
                },
            )
        )
        balance = await client.get_balance()
        assert balance == pytest.approx(1.045045)

    @respx.mock
    async def test_uses_cached_on_failure(self, client: Level5Client) -> None:
        respx.get(f"{BASE_URL}/proxy/{TEST_TOKEN}/balance").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "usdc_balance": 10000000,
                        "credit_balance": 0,
                        "is_active": True,
                    },
                ),
                httpx.Response(500),
            ]
        )
        balance1 = await client.get_balance()
        assert balance1 == 10.0

        balance2 = await client.get_balance()
        assert balance2 == 10.0  # Cached

    @respx.mock
    async def test_raises_without_cache(self, client: Level5Client) -> None:
        respx.get(f"{BASE_URL}/proxy/{TEST_TOKEN}/balance").mock(return_value=httpx.Response(500))
        with pytest.raises(Level5Error, match="Failed to check balance"):
            await client.get_balance()

    @respx.mock
    async def test_check_can_afford_true(self, client: Level5Client) -> None:
        respx.get(f"{BASE_URL}/proxy/{TEST_TOKEN}/balance").mock(
            return_value=httpx.Response(
                200,
                json={
                    "usdc_balance": 10000000,
                    "credit_balance": 0,
                    "is_active": True,
                },
            )
        )
        assert await client.check_can_afford(5.0) is True

    @respx.mock
    async def test_check_can_afford_false(self, client: Level5Client) -> None:
        respx.get(f"{BASE_URL}/proxy/{TEST_TOKEN}/balance").mock(
            return_value=httpx.Response(
                200,
                json={
                    "usdc_balance": 1000000,
                    "credit_balance": 0,
                    "is_active": True,
                },
            )
        )
        assert await client.check_can_afford(5.0) is False


class TestBalanceFromHeaders:
    def test_parse_header(self) -> None:
        client = Level5Client(api_token="tok")
        headers = httpx.Headers({"x-balance-remaining": "3500000"})
        balance = client.update_balance_from_headers(headers)
        assert balance == 3.5
        assert client.last_known_balance == 3.5

    def test_missing_header(self) -> None:
        client = Level5Client(api_token="tok")
        headers = httpx.Headers({})
        assert client.update_balance_from_headers(headers) is None

    def test_invalid_header(self) -> None:
        client = Level5Client(api_token="tok")
        headers = httpx.Headers({"x-balance-remaining": "not_a_number"})
        assert client.update_balance_from_headers(headers) is None


class TestProxyUrl:
    def test_get_api_base_url(self) -> None:
        client = Level5Client(api_token="mytoken", base_url="https://api.level5.cloud")
        url = client.get_api_base_url()
        assert url == "https://api.level5.cloud/proxy/mytoken/v1"

    def test_get_dashboard_url(self) -> None:
        client = Level5Client(api_token="mytoken")
        assert client.get_dashboard_url() == "https://level5.cloud/dashboard/mytoken"

    def test_is_registered(self) -> None:
        assert Level5Client(api_token="tok").is_registered() is True
        assert Level5Client(api_token=None).is_registered() is False
        assert Level5Client(api_token="").is_registered() is False


class TestRetry:
    @respx.mock
    async def test_retries_on_5xx(self, client: Level5Client) -> None:
        route = respx.post(f"{BASE_URL}/v1/register")
        route.side_effect = [
            httpx.Response(500, json={"error": "internal"}),
            httpx.Response(
                200,
                json={
                    "api_token": "tok",
                    "deposit_address": "addr",
                    "balance_usdc": 0,
                },
            ),
        ]
        account = await client.register()
        assert account.api_token == "tok"
        assert route.call_count == 2

    @respx.mock
    async def test_raises_on_4xx(self, client: Level5Client) -> None:
        respx.post(f"{BASE_URL}/v1/register").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        with pytest.raises(Level5Error, match="404"):
            await client.register()

    @respx.mock
    async def test_raises_after_max_retries(self, client: Level5Client) -> None:
        respx.post(f"{BASE_URL}/v1/register").mock(
            return_value=httpx.Response(503, json={"error": "unavailable"})
        )
        with pytest.raises(Level5Error, match="failed after"):
            await client.register()


class TestContextManager:
    async def test_raises_without_context_manager(self) -> None:
        client = Level5Client(api_token="tok")
        with pytest.raises(Level5Error, match="async context manager"):
            await client.get_balance()


class TestSanitizeUrl:
    def test_truncates_token_in_proxy_url(self) -> None:
        url = "https://api.level5.cloud/proxy/abcdef123456789/v1/chat/completions"
        sanitized = _sanitize_url(url)
        assert "abcdef123456789" not in sanitized
        assert "abcdef12..." in sanitized

    def test_no_token_passes_through(self) -> None:
        url = "https://api.level5.cloud/v1/register"
        assert _sanitize_url(url) == url
