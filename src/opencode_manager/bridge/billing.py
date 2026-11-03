"""Who pays for a run: the "billing" field of usage events.

    "api"           pay per use: the cost is what the provider charges
    "subscription"  covered by a flat-rate plan (Claude Pro/Max, ChatGPT Plus/Pro);
                    it counts toward the plan's usage limits, and a cost is at
                    most what the tokens would have cost at API prices
    "free"          a local model, or one offered at no charge
    None            unknown
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

API, SUBSCRIPTION, FREE = "api", "subscription", "free"


def is_local_url(url: str | None) -> bool:
    """Whether a URL points at this machine or the local network."""
    host = (urlsplit(url or "").hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith((".local", ".lan", ".home", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host  # a bare host name on the LAN
    return address.is_private or address.is_loopback or address.is_link_local


def local_host(url: str | None) -> str | None:
    """The host of a server on this machine or the local network, e.g. an Ollama
    server; None for anything else."""
    return (urlsplit(url or "").hostname or "").lower() if is_local_url(url) else None


def claude_billing(api_key_source: str | None) -> str | None:
    """From the apiKeySource in Claude Code's init message: "none" means a
    claude.ai login (a subscription), anything else an API key."""
    if not api_key_source:
        return None
    return SUBSCRIPTION if api_key_source == "none" else API


def opencode_billing(provider: dict[str, Any] | None, integration: dict[str, Any] | None,
                     model: dict[str, Any] | None) -> str | None:
    """From opencode's descriptions of a provider, its integration and the model."""
    if provider is None:
        return None
    connections = (integration or {}).get("connections") or []
    if any(c.get("method") == "oauth" for c in connections if isinstance(c, dict)):
        return SUBSCRIPTION  # signed in with an account, e.g. ChatGPT Plus/Pro
    prices = [p for p in (model or {}).get("cost") or [] if isinstance(p, dict)]
    if prices and not any(_priced(p) for p in prices):
        return FREE
    if is_local_url((provider.get("settings") or {}).get("baseURL")):
        return FREE
    return API


def _priced(price: dict[str, Any]) -> bool:
    return any(_priced(v) if isinstance(v, dict) else (isinstance(v, (int, float)) and v > 0)
               for v in price.values())
