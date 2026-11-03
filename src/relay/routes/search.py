"""Relay's web search and fetch (relay.search, relay.fetch): search on or off, their route (this
machine's connection or a VPN tunnel's), and a search or fetch to try, in Settings → General."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from starlette.requests import Request

from relay import config
from relay.fetch import FetchError
from relay.search import SearchError, format_results
from relay.web import ApiError, body

if TYPE_CHECKING:
    from relay.app import Relay


async def get_search(request: Request, relay: Relay):
    # (each asks podman, which can be slow when it's busy: both at once)
    searching, fetching = await asyncio.gather(asyncio.to_thread(relay.search.status),
                                               asyncio.to_thread(relay.fetch.running))
    status = {**searching, "fetching": fetching}
    return {**relay.search.config(), "status": status,
            "tunnels": [{"id": t["id"], "name": t["name"]} for t in relay.vpn.config()["tunnels"]]}


async def put_search(request: Request, relay: Relay):
    """On or off, and its route: another route makes SearXNG anew on the next search."""
    data = await body(request)
    old = relay.search.config()
    tunnel = str(data.get("tunnel", old["tunnel"]) or "")
    if tunnel and relay.vpn.tunnel(tunnel) is None:
        raise ApiError("tunnel: one of the VPN tunnels (Settings → Workspaces → VPN), or none")
    relay.settings.search = {"enabled": data.get("enabled", old["enabled"]) is not False, "tunnel": tunnel}
    config.save_settings(relay.settings)
    if tunnel != old["tunnel"] or not relay.settings.search["enabled"]:
        await asyncio.to_thread(relay.search.stop)
    if tunnel != old["tunnel"]:
        await asyncio.to_thread(relay.fetch.stop)  # (the same route)
    return await get_search(request, relay)


async def try_search(request: Request, relay: Relay):
    """A search, or a fetch of an address, from Settings: what an agent would get."""
    query = str((await body(request)).get("query") or "").strip()
    if not query:
        raise ApiError("query: what to search for, or an address to fetch")
    if re.match(r"https?://\S+$", query):
        try:
            text = await asyncio.to_thread(relay.fetch.text, {"url": query})
        except FetchError as exc:
            raise ApiError(str(exc), 409) from exc
        head, _, rest = text.partition("\n\n")
        return {"fetched": head, "text": rest[:3000], "more": len(rest) > 3000}
    try:
        found = await asyncio.to_thread(relay.search.query, query)
    except SearchError as exc:
        raise ApiError(str(exc), 409) from exc
    results = [{"title": r.get("title") or r.get("url"), "url": r.get("url"), "content": r.get("content") or "",
                "engines": r.get("engines") or []} for r in found.get("results") or [] if isinstance(r, dict)
               and r.get("url")]
    return {"results": results[:8], "text": format_results(query, found),
            "unresponsive": [f"{e[0]} ({e[1]})" for e in found.get("unresponsive_engines") or [] if isinstance(e, list)
                             and len(e) > 1]}
