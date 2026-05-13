"""LLM-proxy provider abstraction.

pod-the-trader supports two providers, both with byte-identical API
shapes (UsePod is a Rust port of Level5):

- Level5 (level5.cloud) — original, has USDC + promotional-credits
  dual-ledger billing, dashboard at ``/dashboard/{token}`` path style.
- UsePod (usepod.ai) — same /v1/register, /proxy/{token}/v1/* routes,
  no credits (always 0 in the balance response), dashboard at
  ``/dashboard?token={token}`` query style.

The concrete clients (Level5Client / Level5Auth) consult a
ProviderConfig for domain defaults, the dashboard URL template, and
whether to surface credits in the TUI. Adding a third provider with
the same wire shape only requires extending ``PROVIDERS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Provider(StrEnum):
    """The set of supported LLM-proxy providers."""

    LEVEL5 = "level5"
    USEPOD = "usepod"


@dataclass(frozen=True)
class ProviderConfig:
    """Per-provider behavior carried alongside the (otherwise
    interchangeable) Level5-compatible API surface.

    ``dashboard_url_template`` is a ``str.format``-style template with
    ``{domain}`` and ``{token}`` placeholders; it's used by
    ``Level5Client.get_dashboard_url`` for tokens entered manually,
    where there's no register response to read the URL from.
    """

    key: str
    display_name: str
    default_domain: str
    dashboard_url_template: str
    has_credits: bool


PROVIDERS: dict[str, ProviderConfig] = {
    Provider.LEVEL5.value: ProviderConfig(
        key=Provider.LEVEL5.value,
        display_name="Level5",
        default_domain="level5.cloud",
        dashboard_url_template="https://{domain}/dashboard/{token}",
        has_credits=True,
    ),
    Provider.USEPOD.value: ProviderConfig(
        key=Provider.USEPOD.value,
        display_name="UsePod",
        default_domain="usepod.ai",
        # Query-param form — confirmed against the usepod.ai source
        # (services/api/src/handler/public.rs).
        dashboard_url_template="https://{domain}/dashboard?token={token}",
        has_credits=False,
    ),
}


def resolve_provider(key: str | None) -> ProviderConfig:
    """Return the ProviderConfig for ``key``, defaulting to Level5.

    Case-insensitive. ``None`` or an empty string returns the Level5
    config so legacy invocations (and credential files predating the
    provider field) keep working unchanged. Unknown values raise
    ``ValueError``.
    """
    if not key:
        return PROVIDERS[Provider.LEVEL5.value]
    normalized = key.strip().lower()
    if normalized not in PROVIDERS:
        raise ValueError(f"Unknown provider {key!r}. Supported: {sorted(PROVIDERS)}")
    return PROVIDERS[normalized]
