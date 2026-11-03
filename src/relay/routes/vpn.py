"""VPNs for WorkBoxes (relay.vpn): profiles (VPN accounts) and tunnels (which WorkBoxes go through
which exits), in Settings → Workspaces."""

from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.requests import Request

from relay import config
from relay.containers import ContainerError
from relay.vpn import PLACE_KEYS, ROTATE_TO, SECRET_WORDS, servers
from relay.web import SECRET_MASK, ApiError, body, within_roots

if TYPE_CHECKING:
    from relay.app import Relay


def _masked(env: dict[str, str]) -> dict[str, str]:
    return {k: SECRET_MASK if any(w in k.upper() for w in SECRET_WORDS) and v else v for k, v in env.items()}


def _save(relay: Relay, profiles: list[dict], tunnels: list[dict]) -> None:
    image = relay.vpn.config()["image"]
    relay.settings.vpn = {"profiles": profiles, "tunnels": tunnels, **({"image": image} if image else {})}
    config.save_settings(relay.settings)


async def get_vpn(request: Request, relay: Relay):
    found = relay.vpn.config()
    tunnels = []
    for tunnel in found["tunnels"]:
        tunnels.append({**tunnel, "status": await asyncio.to_thread(relay.vpn.status, tunnel)})
    return {"profiles": [{**p, "env": _masked(p["env"])} for p in found["profiles"]], "tunnels": tunnels,
            "relay_url": relay.vpn.relay_url()}


# Profiles: VPN accounts

def _profile(data: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    name = str(data.get("name", (old or {}).get("name", "")) or "").strip()
    if not name:
        raise ApiError("name: what to call this VPN account")
    env = data.get("env", (old or {}).get("env", {}))
    if not isinstance(env, dict) or not all(isinstance(k, str) and k.strip() and isinstance(v, str)
                                            for k, v in env.items()):
        raise ApiError("env: gluetun's settings, NAME=value")
    kept = (old or {}).get("env", {})
    env = {k.strip(): (kept.get(k.strip(), "") if v == SECRET_MASK else v.strip()) for k, v in env.items()}
    env = {k: v for k, v in env.items() if k not in PLACE_KEYS}  # (a place is a tunnel's exit's)
    if not env.get("VPN_SERVICE_PROVIDER"):
        raise ApiError("env: which VPN provider (VPN_SERVICE_PROVIDER)")
    return {"id": (old or {}).get("id") or "p" + secrets.token_hex(3), "name": name, "env": env}


async def add_profile(request: Request, relay: Relay):
    found = relay.vpn.config()
    profile = _profile(await body(request), None)
    _save(relay, [*found["profiles"], profile], found["tunnels"])
    return {**profile, "env": _masked(profile["env"])}


async def update_profile(request: Request, relay: Relay):
    """A changed account: the tunnels at one of its exits connect anew (their WorkBoxes keep their network)."""
    found = relay.vpn.config()
    old = relay.vpn.profile(request.path_params["profile_id"])
    if old is None:
        raise ApiError("No such VPN profile", 404)
    profile = _profile(await body(request), old)
    _save(relay, [profile if p["id"] == old["id"] else p for p in found["profiles"]], found["tunnels"])
    errors = []
    for tunnel in relay.vpn.config()["tunnels"]:
        if any(e["profile"] == old["id"] for e in tunnel["exits"]) and await asyncio.to_thread(relay.vpn.running,
                                                                                               tunnel):
            try:
                await asyncio.to_thread(relay.vpn.ensure, tunnel)
            except ContainerError as exc:
                errors.append(str(exc))
    return {**profile, "env": _masked(profile["env"]), "error": " ".join(errors) or None}


async def delete_profile(request: Request, relay: Relay):
    found = relay.vpn.config()
    profile_id = request.path_params["profile_id"]
    using = [t["name"] for t in found["tunnels"] if any(e["profile"] == profile_id for e in t["exits"])]
    if using:
        raise ApiError(f"Tunnels go through it: {', '.join(using)}. Take it out of their exits first.", 409)
    _save(relay, [p for p in found["profiles"] if p["id"] != profile_id], found["tunnels"])
    return {"ok": True}


# Tunnels: which WorkBoxes go through which exits

def _number(value: Any, what: str, low: int = 0, high: int = 10**6) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"{what}: a number") from exc
    if not low <= number <= high:
        raise ApiError(f"{what}: {low}–{high}")
    return number


def _tunnel(relay: Relay, data: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    found = relay.vpn.config()
    old = old or {}
    tunnel_id = old.get("id") or "t" + secrets.token_hex(3)
    others = [t for t in found["tunnels"] if t["id"] != tunnel_id]
    name = str(data.get("name", old.get("name", "")) or "").strip()
    if not name:
        raise ApiError("name: what to call this tunnel")
    folders = data.get("folders", old.get("folders", []))
    if not isinstance(folders, list) or not all(isinstance(f, str) and f.strip() for f in folders):
        raise ApiError("folders: a list of project folders")
    folders = list(dict.fromkeys(str(Path(f.strip()).expanduser()) for f in folders))
    for folder in folders:
        if not Path(folder).is_absolute() or not within_roots(relay, Path(folder)):
            raise ApiError(f"{folder}: choose a folder in one of the workspace roots")
        taken = next((t["name"] for t in others if folder in t["folders"]), None)
        if taken:
            raise ApiError(f"{folder} goes through the tunnel {taken} already")
    everything = bool(data.get("all", old.get("all", False)))
    if everything and any(t["all"] for t in others):
        raise ApiError("Another tunnel is for all WorkBoxes already")
    ports = data.get("ports", old.get("ports", []))
    if not isinstance(ports, list):
        raise ApiError("ports: a list of numbers")
    ports = sorted({_number(p, "ports", 1, 65535) for p in ports})
    if relay.settings.port in ports:
        raise ApiError("ports: not Relay's own")
    for port in ports:
        taken = next((t["name"] for t in others if port in t["ports"]), None)
        if taken:
            raise ApiError(f"Port {port} is the tunnel {taken}'s already")
    exits = data.get("exits", old.get("exits", []))
    if not isinstance(exits, list) or not exits:
        raise ApiError("exits: at least one (a profile and, if you like, a place)")
    profiles = {p["id"] for p in found["profiles"]}
    checked = []
    for chosen in exits:
        if not isinstance(chosen, dict) or chosen.get("profile") not in profiles:
            raise ApiError("exits: each with one of the VPN profiles")
        place = chosen.get("place") or {}
        if not isinstance(place, dict) or not all(k in PLACE_KEYS and isinstance(v, str) for k, v in place.items()):
            raise ApiError(f"exits: a place is {', '.join(PLACE_KEYS)}")
        checked.append({"profile": chosen["profile"], "place": {k: v.strip() for k, v in place.items() if v.strip()}})
    rotate = {**old.get("rotate", {}), **(data.get("rotate") or {})}
    rotate = {"minutes": _number(rotate.get("minutes", 0), "rotate.minutes", 0, 7 * 24 * 60),
              "to": rotate.get("to") if rotate.get("to") in ROTATE_TO else "server",
              "idle": rotate.get("idle", True) is not False}
    if rotate["minutes"] and rotate["minutes"] < 5:
        raise ApiError("rotate.minutes: at least 5 (a new connection takes a while)")
    fallback = {**old.get("fallback", {}), **(data.get("fallback") or {})}
    fallback = {"after": _number(fallback.get("after", 60), "fallback.after", 0, 3600),
                "back": _number(fallback.get("back", 30), "fallback.back", 0, 7 * 24 * 60)}
    if fallback["after"] and fallback["after"] < 20:
        raise ApiError("fallback.after: at least 20 seconds (connecting takes a few)")
    return {"id": tunnel_id, "name": name, "folders": folders, "all": everything, "ports": ports, "exits": checked,
            "rotate": rotate, "fallback": fallback}


async def _connect(relay: Relay, tunnel: dict[str, Any]) -> str | None:
    """A tunnel some WorkBoxes go through: connected now, at its first exit (made anew when its
    settings changed); what went wrong, if so."""
    if not tunnel["folders"] and not tunnel["all"]:
        return None
    relay.vpn.start_over(tunnel)
    try:
        await asyncio.to_thread(relay.vpn.ensure, tunnel)
    except ContainerError as exc:  # (VpnError, or a box that won't start: a port taken, say; saved anyway)
        return str(exc)
    return None


async def add_tunnel(request: Request, relay: Relay):
    found = relay.vpn.config()
    tunnel = _tunnel(relay, await body(request), None)
    _save(relay, found["profiles"], [*found["tunnels"], tunnel])
    error = await _connect(relay, tunnel)
    return {**tunnel, "status": await asyncio.to_thread(relay.vpn.status, tunnel), "error": error}


async def update_tunnel(request: Request, relay: Relay):
    found = relay.vpn.config()
    old = relay.vpn.tunnel(request.path_params["tunnel_id"])
    if old is None:
        raise ApiError("No such tunnel", 404)
    tunnel = _tunnel(relay, await body(request), old)
    _save(relay, found["profiles"], [tunnel if t["id"] == old["id"] else t for t in found["tunnels"]])
    if not tunnel["folders"] and not tunnel["all"]:  # (nobody's any more: it goes)
        await asyncio.to_thread(relay.vpn.stop, tunnel)
    error = await _connect(relay, tunnel)
    return {**tunnel, "status": await asyncio.to_thread(relay.vpn.status, tunnel), "error": error}


async def delete_tunnel(request: Request, relay: Relay):
    """Its containers go, and its WorkBoxes (on the machine's network, or another tunnel's, when next used)."""
    found = relay.vpn.config()
    tunnel = relay.vpn.tunnel(request.path_params["tunnel_id"])
    if tunnel is None:
        raise ApiError("No such tunnel", 404)
    if relay.search.config()["tunnel"] == tunnel["id"]:
        raise ApiError("Web search and fetch go through it: choose another route first (Settings → General → Web "
                       "search and fetch)", 409)
    _save(relay, found["profiles"], [t for t in found["tunnels"] if t["id"] != tunnel["id"]])
    await asyncio.to_thread(relay.vpn.stop, tunnel)
    relay.vpn.forget(tunnel["id"])  # (what happened to it)
    return {"ok": True}


async def reconnect_tunnel(request: Request, relay: Relay):
    tunnel = relay.vpn.tunnel(request.path_params["tunnel_id"])
    if tunnel is None:
        raise ApiError("No such tunnel", 404)
    try:
        await asyncio.to_thread(relay.vpn.reconnect, tunnel)
    except ContainerError as exc:
        raise ApiError(str(exc), 409) from exc
    return {**tunnel, "status": await asyncio.to_thread(relay.vpn.status, tunnel)}


async def tunnel_log(request: Request, relay: Relay):
    tunnel = relay.vpn.tunnel(request.path_params["tunnel_id"])
    if tunnel is None:
        raise ApiError("No such tunnel", 404)
    return {"log": await asyncio.to_thread(relay.vpn.log, tunnel, 200)}


async def vpn_servers(request: Request, relay: Relay):
    """gluetun's providers and their servers' places (relay.vpn.servers), for the forms."""
    try:
        return await asyncio.to_thread(servers, relay.vpn.image())
    except ContainerError as exc:
        raise ApiError(str(exc), 409) from exc
