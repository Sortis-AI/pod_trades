"""Level5 API client with retry logic and log sanitization."""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r"(/proxy/|/dashboard/)([a-zA-Z0-9_-]{9,})(/|$)")

# Balance is in microunits (6 decimals)
_USDC_DECIMALS = 1_000_000


def _sanitize_url(url: str) -> str:
    """Truncate API tokens in URLs for safe logging."""
    return _TOKEN_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)[:8]}...{m.group(3)}", url)


@dataclass
class Level5Account:
    """Result of Level5 registration."""

    api_token: str
    deposit_address: str
    balance_usdc: float


class Level5Error(Exception):
    """Raised on Level5 API failures."""


class Level5Client:
    """Async client for the Level5 billing proxy.

    Level5 proxies LLM API calls and bills per-token in USDC.
    Balance is returned via the X-Balance-Remaining header on every
    proxied response (in microunits, 6 decimals).

    Use as an async context manager:
        async with Level5Client(token) as client:
            balance = await client.get_balance()
    """

    def __init__(
        self,
        api_token: str | None = None,
        deposit_address: str | None = None,
        base_url: str = "https://api.level5.cloud",
    ) -> None:
        self._api_token = api_token
        self._deposit_address = deposit_address
        self._base_url = base_url.rstrip("/")
        self._http: httpx.AsyncClient | None = None
        self._last_balance_usdc: float | None = None

    async def __aenter__(self) -> "Level5Client":
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=30, write=30, pool=30),
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise Level5Error("Level5Client must be used as an async context manager")
        return self._http

    async def register(self) -> Level5Account:
        """Register a new Level5 account."""
        data = await self._request("POST", "/v1/register")
        account = Level5Account(
            api_token=data["api_token"],
            deposit_address=data["deposit_address"],
            balance_usdc=float(data.get("balance_usdc", 0.0)),
        )
        self._api_token = account.api_token
        self._deposit_address = account.deposit_address
        logger.info(
            "Registered with Level5. Deposit address: %s",
            account.deposit_address,
        )
        return account

    async def get_balance(self) -> float:
        """Get current USDC balance via the dedicated balance endpoint."""
        url = f"{self._base_url}/proxy/{self._api_token}/balance"
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()
            # usdc_balance and credit_balance are in microunits (6 decimals)
            usdc = int(data.get("usdc_balance", 0))
            credit = int(data.get("credit_balance", 0))
            balance = (usdc + credit) / _USDC_DECIMALS
            self._last_balance_usdc = balance
            return balance
        except Exception as e:
            # Also try the X-Balance-Remaining header
            if hasattr(e, "response"):
                header_balance = self.update_balance_from_headers(e.response.headers)
                if header_balance is not None:
                    return header_balance
            if self._last_balance_usdc is not None:
                logger.debug(
                    "Balance check failed, using cached: $%.4f",
                    self._last_balance_usdc,
                )
                return self._last_balance_usdc
            raise Level5Error(f"Failed to check balance: {e}") from e

    def update_balance_from_headers(self, headers: httpx.Headers) -> float | None:
        """Extract and cache balance from response headers."""
        raw = headers.get("x-balance-remaining")
        if raw is not None:
            try:
                balance = int(raw) / _USDC_DECIMALS
                self._last_balance_usdc = balance
                return balance
            except (ValueError, TypeError):
                pass
        return None

    async def check_can_afford(self, cost: float) -> bool:
        """Check if the current balance covers the given cost."""
        balance = await self.get_balance()
        return balance >= cost

    def get_api_base_url(self) -> str:
        """Return the Level5 proxy base URL for LLM API calls.

        Includes /v1 so the OpenAI SDK appends /chat/completions correctly:
        {base}/proxy/{token}/v1/chat/completions
        """
        return f"{self._base_url}/proxy/{self._api_token}/v1"

    def get_dashboard_url(self) -> str:
        """Return the Level5 dashboard URL for this account."""
        return f"https://level5.cloud/dashboard/{self._api_token}"

    def is_registered(self) -> bool:
        """Check if we have a valid API token."""
        return bool(self._api_token)

    @property
    def last_known_balance(self) -> float | None:
        """Return the last cached balance, or None if never checked."""
        return self._last_balance_usdc

    def _parse_balance_header(self, response: httpx.Response) -> float:
        """Parse X-Balance-Remaining from a response."""
        raw = response.headers.get("x-balance-remaining")
        if raw is None:
            raise Level5Error("No X-Balance-Remaining header in response")
        balance = int(raw) / _USDC_DECIMALS
        self._last_balance_usdc = balance
        return balance

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict | None = None,
        max_retries: int = 3,
    ) -> dict:
        """Make an HTTP request with retry on 5xx and transport errors."""
        url = f"{self._base_url}{path}"
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                response = await self._client.request(method, url, json=json_data)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    last_error = e
                    wait = 2**attempt
                    logger.warning(
                        "Level5 %s %s returned %d, retrying in %ds (%d/%d)",
                        method,
                        _sanitize_url(url),
                        e.response.status_code,
                        wait,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise Level5Error(
                    f"Level5 API error: {e.response.status_code} on {_sanitize_url(url)}"
                ) from e
            except httpx.TransportError as e:
                last_error = e
                wait = 2**attempt
                logger.warning(
                    "Level5 transport error on %s, retrying in %ds (%d/%d)",
                    _sanitize_url(url),
                    wait,
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(wait)

        raise Level5Error(
            f"Level5 request failed after {max_retries} attempts: {last_error}"
        ) from last_error
