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
    """Result of Level5 registration.

    Field mapping onto the real ``/v1/register`` response (SKILL v1.7.2):

    * ``api_token``          ← ``api_token``
    * ``deposit_address``    ← ``instructions.contract_address``
      (sovereign Solana contract where USDC lands)
    * ``deposit_code``       ← ``deposit_code``
      (per-account identifier Level5 uses to route a deposit; the
      operator provides it through the dashboard when funding)
    * ``dashboard_url``      ← ``instructions.dashboard_url``
    * ``status``             ← ``status`` (e.g. ``"pending_deposit"``)
    * ``balance_usdc``       ← 0 at registration time; the get_balance
      endpoint is the source of truth thereafter
    """

    api_token: str
    deposit_address: str
    balance_usdc: float
    deposit_code: str = ""
    dashboard_url: str = ""
    status: str = ""


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
        # Split between deposited USDC and promotional credits — both in
        # USD, populated on every successful get_balance() call.
        self._last_usdc_only: float = 0.0
        self._last_credit_only: float = 0.0
        # ``is_active`` from the balance endpoint — flips to True once the
        # first deposit has been credited or the account has locked SQUIRE.
        # Used by the startup funding wait to decide when to proceed.
        self._last_is_active: bool = False

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
        """Register a new Level5 account.

        Parses the real ``/v1/register`` response shape per SKILL v1.7.2:

        .. code-block:: json

            {
              "api_token": "...",
              "deposit_code": "A1B2C3D4E5F6A7B8",
              "status": "pending_deposit",
              "instructions": {
                "contract_address": "BBAdcq...",
                "dashboard_url": "https://level5.cloud/dashboard/<token>"
              }
            }

        The sovereign contract address (where USDC actually gets
        deposited) lives under ``instructions.contract_address``; it's
        what pod-the-trader has historically called ``deposit_address``.
        The ``deposit_code`` is a per-account identifier Level5 uses to
        match a deposit back to the right account.

        Raises ``Level5Error`` with the observed response keys when
        anything critical is missing, empty, null, or the wrong type.
        """
        data = await self._request("POST", "/v1/register")

        if not isinstance(data, dict):
            raise Level5Error(
                f"Level5 /v1/register returned a non-object response: {type(data).__name__}"
            )

        api_token = data.get("api_token")
        deposit_code = data.get("deposit_code")
        status = data.get("status", "") or ""
        instructions = data.get("instructions") or {}
        if not isinstance(instructions, dict):
            instructions = {}
        contract_address = instructions.get("contract_address")
        dashboard_url = instructions.get("dashboard_url", "") or ""

        def _observed_shape() -> str:
            top = sorted(data.keys())
            nested = sorted(instructions.keys()) if instructions else []
            return f"top-level keys: {top}; instructions keys: {nested}"

        if not api_token or not isinstance(api_token, str):
            raise Level5Error(
                f"Level5 /v1/register response is missing `api_token`. {_observed_shape()}"
            )
        if not contract_address or not isinstance(contract_address, str):
            raise Level5Error(
                "Level5 /v1/register response is missing "
                f"`instructions.contract_address`. {_observed_shape()}"
            )
        if not deposit_code or not isinstance(deposit_code, str):
            raise Level5Error(
                f"Level5 /v1/register response is missing `deposit_code`. {_observed_shape()}"
            )

        account = Level5Account(
            api_token=api_token,
            deposit_address=contract_address,
            deposit_code=deposit_code,
            dashboard_url=dashboard_url,
            status=status,
            balance_usdc=float(data.get("balance_usdc", 0.0)),
        )
        self._api_token = account.api_token
        self._deposit_address = account.deposit_address
        logger.info(
            "Registered with Level5. Contract: %s  deposit_code: %s  status: %s",
            account.deposit_address,
            account.deposit_code,
            account.status or "(none)",
        )
        return account

    async def get_balance(self) -> float:
        """Get current USDC balance via the dedicated balance endpoint.

        Also captures the ``is_active`` flag into
        :attr:`last_is_active`. The flag flips to True once Level5 has
        credited the account (first deposit or first SQUIRE lock), and
        pod-the-trader's startup funding wait uses it as the signal to
        stop polling and start trading.

        Raises ``Level5Error`` on any transport or HTTP failure. Does
        NOT fall back to cached values: callers all wrap this in their
        own retry/tolerance logic (the funding orchestrator retries on
        the configured poll interval; the agent loop logs and continues
        to the next cycle), and a silent cache fallback here would
        strand the funding wait with stale ``is_active=False`` when a
        transient error lands between the deposit and the first
        successful post-deposit poll.
        """
        url = f"{self._base_url}/proxy/{self._api_token}/balance"
        try:
            response = await self._client.get(url)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            raise Level5Error(f"Failed to check balance: {e}") from e

        # usdc_balance and credit_balance are in microunits (6 decimals)
        usdc = int(data.get("usdc_balance", 0))
        credit = int(data.get("credit_balance", 0))
        self._last_usdc_only = usdc / _USDC_DECIMALS
        self._last_credit_only = credit / _USDC_DECIMALS
        self._last_is_active = bool(data.get("is_active", False))
        balance = (usdc + credit) / _USDC_DECIMALS
        self._last_balance_usdc = balance
        return balance

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

    @property
    def last_usdc_balance(self) -> float:
        """Deposited USDC portion of the last balance check."""
        return self._last_usdc_only

    @property
    def last_credit_balance(self) -> float:
        """Promotional credit portion of the last balance check."""
        return self._last_credit_only

    @property
    def last_is_active(self) -> bool:
        """``is_active`` flag from the last balance check.

        True once the account has been activated by a first deposit or
        SQUIRE lock. False before any funding lands.
        """
        return self._last_is_active

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
