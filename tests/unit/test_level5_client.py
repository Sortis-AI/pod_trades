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
    """Tests for the /v1/register response shape described in Level5
    SKILL v1.7.2:

        {
          "api_token": "...",
          "deposit_code": "A1B2C3D4E5F6A7B8",
          "status": "pending_deposit",
          "instructions": {
            "contract_address": "BBAdcq...",
            "dashboard_url": "https://level5.cloud/dashboard/<token>"
          }
        }
    """

    @staticmethod
    def _good_response() -> dict:
        return {
            "api_token": "new_token_xyz",
            "deposit_code": "A1B2C3D4E5F6A7B8",
            "status": "pending_deposit",
            "instructions": {
                "contract_address": "BBAdcqUkg68JXNiPQ1HR1wujfZuayyK3eQTQSYAh6FSW",
                "dashboard_url": "https://level5.cloud/dashboard/new_token_xyz",
            },
        }

    @respx.mock
    async def test_register_success(self) -> None:
        respx.post(f"{BASE_URL}/v1/register").mock(
            return_value=httpx.Response(200, json=self._good_response())
        )
        async with Level5Client(base_url=BASE_URL) as client:
            account = await client.register()
        assert account.api_token == "new_token_xyz"
        # deposit_address is the sovereign contract (instructions.contract_address)
        assert account.deposit_address == "BBAdcqUkg68JXNiPQ1HR1wujfZuayyK3eQTQSYAh6FSW"
        assert account.deposit_code == "A1B2C3D4E5F6A7B8"
        assert account.status == "pending_deposit"
        assert account.dashboard_url == "https://level5.cloud/dashboard/new_token_xyz"
        assert account.balance_usdc == 0.0

    @respx.mock
    async def test_register_missing_contract_address_raises_level5_error(
        self,
    ) -> None:
        """Regression test for the exact crash a user hit during first-run
        registration: the response has ``instructions`` but no
        ``contract_address`` inside it. The client must surface a clean
        ``Level5Error`` instead of a bare ``KeyError``.
        """
        body = self._good_response()
        body["instructions"].pop("contract_address")
        respx.post(f"{BASE_URL}/v1/register").mock(return_value=httpx.Response(200, json=body))
        async with Level5Client(base_url=BASE_URL) as client:
            with pytest.raises(Level5Error) as exc:
                await client.register()
        assert "contract_address" in str(exc.value)

    @respx.mock
    async def test_register_missing_instructions_object_raises_level5_error(
        self,
    ) -> None:
        """Whole ``instructions`` object missing — also reported in the wild.
        The user's actual error was:
            Response keys: ['api_token', 'deposit_code', 'instructions', 'status']
        so this covers the inverse case where the object is entirely absent.
        """
        body = self._good_response()
        body.pop("instructions")
        respx.post(f"{BASE_URL}/v1/register").mock(return_value=httpx.Response(200, json=body))
        async with Level5Client(base_url=BASE_URL) as client:
            with pytest.raises(Level5Error) as exc:
                await client.register()
        assert "contract_address" in str(exc.value)

    @respx.mock
    async def test_register_missing_deposit_code_raises_level5_error(self) -> None:
        body = self._good_response()
        body.pop("deposit_code")
        respx.post(f"{BASE_URL}/v1/register").mock(return_value=httpx.Response(200, json=body))
        async with Level5Client(base_url=BASE_URL) as client:
            with pytest.raises(Level5Error) as exc:
                await client.register()
        assert "deposit_code" in str(exc.value)

    @respx.mock
    async def test_register_missing_api_token_raises_level5_error(self) -> None:
        body = self._good_response()
        body.pop("api_token")
        respx.post(f"{BASE_URL}/v1/register").mock(return_value=httpx.Response(200, json=body))
        async with Level5Client(base_url=BASE_URL) as client:
            with pytest.raises(Level5Error) as exc:
                await client.register()
        assert "api_token" in str(exc.value)

    @respx.mock
    async def test_register_empty_contract_address_raises_level5_error(self) -> None:
        body = self._good_response()
        body["instructions"]["contract_address"] = ""
        respx.post(f"{BASE_URL}/v1/register").mock(return_value=httpx.Response(200, json=body))
        async with Level5Client(base_url=BASE_URL) as client:
            with pytest.raises(Level5Error):
                await client.register()

    @respx.mock
    async def test_register_null_contract_address_raises_level5_error(self) -> None:
        body = self._good_response()
        body["instructions"]["contract_address"] = None
        respx.post(f"{BASE_URL}/v1/register").mock(return_value=httpx.Response(200, json=body))
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
    async def test_raises_on_failure_even_with_prior_success(self, client: Level5Client) -> None:
        # A silent cache-fallback would strand the funding wait with a
        # stale is_active=False if a transient error lands between the
        # deposit and the first successful post-deposit poll. Callers
        # are responsible for retry/tolerance.
        respx.get(f"{BASE_URL}/proxy/{TEST_TOKEN}/balance").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "usdc_balance": 10000000,
                        "credit_balance": 0,
                        "is_active": False,
                    },
                ),
                httpx.Response(500),
            ]
        )
        balance1 = await client.get_balance()
        assert balance1 == 10.0
        assert client.last_is_active is False

        with pytest.raises(Level5Error, match="Failed to check balance"):
            await client.get_balance()

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
                    "deposit_code": "CODE123",
                    "status": "pending_deposit",
                    "instructions": {
                        "contract_address": "ContractAddr",
                        "dashboard_url": "https://level5.cloud/dashboard/tok",
                    },
                },
            ),
        ]
        account = await client.register()
        assert account.api_token == "tok"
        assert account.deposit_address == "ContractAddr"
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
