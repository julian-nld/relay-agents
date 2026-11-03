"""Servers on this machine's ports, at a sub-URL of Relay: /port/<n>/….

The user may reach Relay only through a domain (a reverse proxy in front of it), not this machine's
ports, while the servers agents start listen on them (WorkBoxes share the machine's network). So
Relay forwards /port/<n>/… to port n here, for someone signed in to Relay: HTTP (streamed:
server-sent events, downloads) and WebSockets. A ProductionBox's public port goes to its live
version; Relay's own port to Relay itself.

The app sees itself addressed directly (Host, Origin and Referer of http://localhost:<n>, which dev
servers check), without Relay's session cookie, and the path without /port/<n>: unless it lives
under that prefix itself (Vite --base, Next basePath). Its root then answers with a redirect into
the prefix, or a 404, and Relay asks again with the prefix kept, and remembers that for the port
until nothing answers there.

An app made for the root of a site uses absolute paths (/assets/…, /@vite/client, fetch("/api/…")).
A request for one from a page under /port/<n>/ names that page in its Referer (Relay's own pages
send none: no-referrer) and is sent back there (a 307, method and body kept); a module's imports
follow, its own address being under the prefix then. A WebSocket names no page, so one Relay
doesn't serve goes to the port of the last page forwarded (a cookie): dev servers' live reloading.

Such an app shares Relay's origin, so its scripts could use Relay's API as the user: accepted (the
user's own agents' apps, behind the user's sign-in); Relay's session cookie isn't passed on.

A new provider's trial Relay (relay.creations) is at /trial/<id>/, for its user alone: a Relay of its own on a
port of this machine's, which knows that path as its own (its base_path). It gets its own session instead of
Relay's (its user signed in there as they are here), and sets none in the browser.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import subprocess
import urllib.parse
from typing import TYPE_CHECKING, Any

import h11
from starlette.requests import HTTPConnection
from wsproto import ConnectionType, WSConnection
from wsproto.events import (
    AcceptConnection,
    BytesMessage,
    CloseConnection,
    Ping,
    RejectConnection,
    Request,
    TextMessage,
)
from wsproto.utilities import LocalProtocolError, RemoteProtocolError

from relay.web import COOKIE, CSP, error_page, from_relay

if TYPE_CHECKING:
    from relay.app import Relay

PATH = re.compile(r"^/port/(\d{1,5})(/.*)?$", re.DOTALL)
TRIAL = re.compile(r"^/trial/([0-9a-f]{12})(/.*)?$", re.DOTALL)
CONNECT_TIME = 5
ANSWER_TIME = 120  # seconds for an answer to begin (a dev server compiles on its first request)
CHUNK = 65536
PORT_COOKIE = "relay_port"  # the port of the last page forwarded (for WebSockets: see above)
# Hop-by-hop, or answered by Relay's own server (and its proxy, the user's)
DROPPED = {b"connection", b"keep-alive", b"proxy-connection", b"te", b"trailer", b"transfer-encoding", b"upgrade",
           b"expect", b"forwarded"}
LOOPBACK = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "::"}
REDIRECTS = {301, 302, 303, 307, 308}
# A cookie an app set for the host Relay told it it's at (localhost): kept by the browser for the host it asked
# (no Domain), as an app that names its cookies' domain after its Host otherwise loses them
LOCALHOST_DOMAIN = re.compile(rb";\s*domain=\.?localhost(?=\s*;|\s*$)", re.IGNORECASE)
CLOSE_CODES = {1005, 1006, 1015}  # (reserved: not sent in a close frame)


@functools.cache
def addresses() -> tuple[str, ...]:
    """This machine's own IPv4 addresses (not loopback's or the containers' networks): a link to one
    of them with a port is to a server here."""
    try:
        done = subprocess.run(["ip", "-j", "-4", "addr"], capture_output=True, text=True, timeout=10)
        links = json.loads(done.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ()
    return tuple(address["local"] for link in links
                 if not link.get("ifname", "").startswith(("lo", "podman", "veth", "cni", "docker", "tun", "wg"))
                 for address in link.get("addr_info") or [] if address.get("family") == "inet")


class Forwarding:
    """ASGI middleware, the outermost: /port/<n>/… (and what comes back to it) forwarded; the rest is
    Relay's."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.kept: set[int] = set()  # ports whose app lives under /port/<n>/ itself

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        trial = TRIAL.match(scope["path"])
        if trial is not None:
            await self._trial(scope, receive, send, trial.group(1), trial.group(2))
            return
        found = PATH.match(scope["path"])
        if found is None:
            mounted = scope["app"].state.relay.plugins.forwarded(scope["path"])
            if mounted is not None:  # (a plugin's web app at a path of Relay's: plugin.forward)
                await self._mounted(scope, receive, send, mounted)
            else:
                await self._elsewhere(scope, receive, send)
            return
        relay: Relay = scope["app"].state.relay
        port, rest = int(found.group(1)), found.group(2)
        raw = scope.get("raw_path") or scope["path"].encode()
        tail = raw[len(f"/port/{found.group(1)}"):]  # (/… of the path as the browser wrote it)
        query = scope.get("query_string") or b""
        ask = f"?{query.decode('latin-1')}" if query else ""
        if not 0 < port < 65536:
            await _page(scope, send, 404, "No such port", "A port is a number from 1 to 65535.")
        elif rest is None:
            await _redirect(scope, send, 307, f"/port/{port}/{ask}")
        elif not _signed_in(relay, scope):
            if scope["type"] == "http" and scope["method"] in ("GET", "HEAD"):
                await _redirect(scope, send, 303, "/")
            else:
                await _page(scope, send, 401, "Not signed in", "Sign in to Relay first.")
        elif scope["type"] == "http" and _foreign(scope):
            await _page(scope, send, 403, "Not from another site",
                        "A page elsewhere (another site, or another port of this machine) can't use a server here "
                        "through Relay. Open the link itself instead.")
        elif port == relay.settings.port:  # (Relay itself)
            await _redirect(scope, send, 307, f"{tail.decode('latin-1')}{ask}")
        else:
            upstream = _upstream(relay, port)
            if upstream is None:
                await _page(scope, send, 502, "Nothing live", f"The service on port {port} has no version live.")
            elif scope["type"] == "http":
                await self._http(scope, receive, send, port, upstream, tail)
            else:
                kept = port in self.kept
                await self._websocket(scope, receive, send, port, upstream, raw if kept else tail, kept)

    async def _elsewhere(self, scope: dict, receive: Any, send: Any) -> None:
        """Not under /port/: Relay's, unless it's an absolute path of a forwarded page's (see above)."""
        connection = HTTPConnection(scope)
        if scope["type"] == "http":
            page = PATH.match(_relays(scope, urllib.parse.urlsplit(connection.headers.get("referer", "")).path))
            if page is not None and page.group(2) is not None:
                raw = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
                query = scope.get("query_string") or b""
                await _redirect(scope, send, 307, f"/port/{page.group(1)}{raw}"
                                + (f"?{query.decode('latin-1')}" if query else ""))
                return
        elif not scope["path"].startswith("/api/"):  # (Relay's own sockets are there)
            port = connection.cookies.get(PORT_COOKIE, "")
            relay: Relay = scope["app"].state.relay
            if port.isdigit() and 0 < int(port) < 65536 and int(port) != relay.settings.port \
                    and _signed_in(relay, scope):
                upstream = _upstream(relay, int(port))
                if upstream is not None:
                    raw = scope.get("raw_path") or scope["path"].encode()
                    await self._websocket(scope, receive, send, int(port), upstream, raw, True)
                    return
        await self.app(scope, receive, send)

    async def _trial(self, scope: dict, receive: Any, send: Any, creation_id: str, rest: str | None) -> None:
        """A new provider's trial Relay (relay.creations), signed in as its user."""
        relay: Relay = scope["app"].state.relay
        prefix = f"/trial/{creation_id}"
        user = _user(relay, scope)
        if rest is None:
            await _redirect(scope, send, 307, f"{prefix}/")
        elif user is None:
            if scope["type"] == "http" and scope["method"] in ("GET", "HEAD"):
                await _redirect(scope, send, 303, "/")
            else:
                await _page(scope, send, 401, "Not signed in", "Sign in to Relay first.")
        elif scope["type"] == "http" and _foreign(scope):
            await _page(scope, send, 403, "Not from another site", "Open the trial Relay from its page in Relay.")
        elif (trial := relay.creations.trial(creation_id)) is None or trial["user"] != user["id"]:
            await _page(scope, send, 404, "No trial Relay here", "It isn't running: its page in Relay (Settings → "
                        "Add provider, the new provider) starts it again, while it's being tried.")
        elif scope["type"] == "http" and rest in ("/api/logout", "/api/account/password"):
            # (its session is Relay's to keep: signing out there would leave the trial without one)
            await _json(send, 200 if rest == "/api/logout" else 403,
                        {"ok": True} if rest == "/api/logout" else {"error": "A trial Relay's account is Relay's"})
        else:
            raw = scope.get("raw_path") or scope["path"].encode()
            tail = raw[len(prefix):]
            headers = _trial_headers(scope, trial["port"], trial["token"], _base(scope) + prefix)
            if scope["type"] == "http":
                await self._trial_http(scope, receive, send, trial["port"], tail, headers, _base(scope) + prefix)
            else:
                await self._websocket(scope, receive, send, trial["port"], trial["port"], tail, False, headers)

    async def _trial_http(self, scope: dict, receive: Any, send: Any, port: int, tail: bytes,
                          sent: list[tuple[bytes, bytes]], prefix: str) -> None:
        answer = await _Answer.ask(scope, receive, port, port, tail, False, sent)
        if answer is None or answer.status is None:
            if answer is not None:
                answer.close()
            await _page(scope, send, 502, "The trial Relay doesn't answer",
                        "Its page in Relay starts it again, while it's being tried.")
            return
        headers = []
        for name, value in answer.headers:
            if name in DROPPED or name in (b"date", b"server", b"set-cookie"):
                continue  # (its cookies are its session's: Relay keeps that, the browser has Relay's)
            if name == b"location":
                value = _trial_location(value.decode("latin-1"), port, prefix).encode("latin-1")
            headers.append((name, value))
        await send({"type": "http.response.start", "status": answer.status, "headers": headers})
        gone = asyncio.ensure_future(_gone(receive))
        gone.add_done_callback(lambda _: answer.close())
        try:
            async for chunk in answer.body():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        except (h11.ProtocolError, OSError):
            pass
        finally:
            gone.cancel()
            answer.close()
        try:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except OSError:
            pass

    async def _http(self, scope: dict, receive: Any, send: Any, port: int, upstream: int, tail: bytes) -> None:
        kept = port in self.kept
        prefix = f"/port/{port}".encode()
        answer = await _Answer.ask(scope, receive, upstream, port, prefix + tail if kept else tail, kept)
        if answer is not None and not kept and scope["method"] in ("GET", "HEAD") \
                and _wants_prefix(answer, port, tail == b"/"):
            answer.close()
            answer = await _Answer.ask(scope, None, upstream, port, prefix + tail, True)
            kept = answer is not None and answer.status != 404
            if kept:
                self.kept.add(port)
        if answer is None:
            self.kept.discard(port)  # (a new server there may live elsewhere)
            await _page(scope, send, 502, "Nothing answers",
                        f"Nothing answers on port {port} on this server. Is the server running? (`relay serve "
                        "--list` in its WorkBox shows the ones an agent started. Behind a VPN tunnel only the "
                        "tunnel's passed-through ports can be reached.)")
            return
        if answer.status is None:
            await _page(scope, send, 504, "No answer", f"The server on port {port} didn't answer in time.")
            return
        await self._answer(scope, receive, send, answer, lambda value: _location(value, port, upstream, _base(scope)),
                           port)

    async def _mounted(self, scope: dict, receive: Any, send: Any, port: int) -> None:
        """A plugin's web app (plugin.forward): its path as it is, for someone signed in, as /port/ is."""
        relay: Relay = scope["app"].state.relay
        if not _signed_in(relay, scope):
            if scope["type"] == "http" and scope["method"] in ("GET", "HEAD"):
                await _redirect(scope, send, 303, "/")
            else:
                await _page(scope, send, 401, "Not signed in", "Sign in to Relay first.")
            return
        if scope["type"] == "http" and _foreign(scope):
            await _page(scope, send, 403, "Not from another site",
                        "A page elsewhere can't use a plugin's app here through Relay. Open the link itself instead.")
            return
        raw = scope.get("raw_path") or scope["path"].encode()
        headers = _mounted_headers(scope, port)
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send, port, port, raw, True, headers)
            return
        answer = await _Answer.ask(scope, receive, port, port, raw, True, headers)
        if answer is None:
            await _page(scope, send, 502, "Nothing answers", f"The plugin's app on port {port} doesn't answer: "
                        "is it running? Its page in Relay starts it.")
            return
        if answer.status is None:
            await _page(scope, send, 504, "No answer", f"The plugin's app on port {port} didn't answer in time.")
            return
        await self._answer(scope, receive, send, answer, lambda value: _mounted_location(value, port, _base(scope)))

    async def _answer(self, scope: dict, receive: Any, send: Any, answer: _Answer, relocate: Any,
                      port: int | None = None) -> None:
        """The app's answer passed on as it comes: its redirects to where the browser finds it (`relocate`), and,
        for a page under /port/<port>/, the cookie that sends its WebSockets there."""
        headers = []
        page = False
        for name, value in answer.headers:
            if name in DROPPED or name in (b"date", b"server", b"referrer-policy"):
                continue  # (Relay's own server adds Date and Server; Referrer-Policy: the fallbacks need it)
            if name == b"location":
                value = relocate(value.decode("latin-1")).encode("latin-1")
            elif name == b"set-cookie":
                value = LOCALHOST_DOMAIN.sub(b"", value)
            elif name == b"content-type":
                page = value.lower().startswith(b"text/html")
            headers.append((name, value))
        headers.append((b"referrer-policy", b"same-origin"))
        if page and port is not None:
            headers.append((b"set-cookie", f"{PORT_COOKIE}={port}; Path=/; SameSite=Strict; HttpOnly".encode()))
        await send({"type": "http.response.start", "status": answer.status, "headers": headers})
        gone = asyncio.ensure_future(_gone(receive))  # (the browser left: so does the way to the app)
        gone.add_done_callback(lambda _: answer.close())
        try:
            async for chunk in answer.body():
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        except (h11.ProtocolError, OSError):
            pass
        finally:
            gone.cancel()
            answer.close()
        try:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except OSError:
            pass

    async def _websocket(self, scope: dict, receive: Any, send: Any, port: int, upstream: int, target: bytes,
                         kept: bool, sent: list[tuple[bytes, bytes]] | None = None) -> None:
        if (await receive())["type"] != "websocket.connect":
            return
        relay: Relay = scope["app"].state.relay
        if not from_relay(HTTPConnection(scope).headers, relay.settings.public_url):
            await send({"type": "websocket.close", "code": 1008})  # (another site's page, with the user's cookie)
            return
        opened = await _connect(upstream)
        if opened is None:
            self.kept.discard(port)
            await send({"type": "websocket.close", "code": 1011})
            return
        reader, writer = opened
        ws = WSConnection(ConnectionType.CLIENT)
        query = scope.get("query_string") or b""
        headers = [(name, value) for name, value in (sent or _request_headers(scope, port, kept))
                   if name not in (b"host", b"content-length") and not name.startswith(b"sec-websocket-")]
        try:
            writer.write(ws.send(Request(host=f"localhost:{port}", extra_headers=headers,
                                         target=(target + (b"?" + query if query else b"")).decode("latin-1"),
                                         subprotocols=list(scope.get("subprotocols") or []))))
            accepted = await asyncio.wait_for(_handshake(ws, reader), ANSWER_TIME)
        except (OSError, asyncio.TimeoutError, LocalProtocolError, RemoteProtocolError):
            accepted = None
        if not isinstance(accepted, AcceptConnection):
            writer.close()
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({"type": "websocket.accept", "subprotocol": accepted.subprotocol})

        async def up() -> None:  # the browser's to the app
            while True:
                message = await receive()
                if message["type"] == "websocket.disconnect":
                    code = message.get("code") or 1000
                    writer.write(ws.send(CloseConnection(code=1000 if code in CLOSE_CODES else code)))
                    return
                if message["type"] == "websocket.receive":
                    if message.get("text") is not None:
                        writer.write(ws.send(TextMessage(data=message["text"])))
                    elif message.get("bytes") is not None:
                        writer.write(ws.send(BytesMessage(data=message["bytes"])))
                    await writer.drain()

        async def down() -> None:  # the app's to the browser
            parts: list = []
            while True:
                for event in ws.events():
                    if isinstance(event, (TextMessage, BytesMessage)):
                        parts.append(event.data)
                        if event.message_finished:
                            text = isinstance(event, TextMessage)
                            whole = ("" if text else b"").join(parts)
                            parts = []
                            await send({"type": "websocket.send", "text" if text else "bytes": whole})
                    elif isinstance(event, Ping):
                        writer.write(ws.send(event.response()))
                    elif isinstance(event, CloseConnection):
                        writer.write(ws.send(event.response()))
                        code = 1000 if event.code in CLOSE_CODES else event.code
                        await send({"type": "websocket.close", "code": code, "reason": event.reason or ""})
                        return
                chunk = await reader.read(CHUNK)
                if not chunk:
                    await send({"type": "websocket.close", "code": 1001})
                    return
                ws.receive_data(chunk)

        ways = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
        try:
            await asyncio.wait(ways, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for way in ways:
                way.cancel()
            await asyncio.gather(*ways, return_exceptions=True)  # (the browser or the app gone meanwhile: over)
            writer.close()


class _Answer:
    """One request to the app (its own connection) and its answer: status None when none began in time."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.h11 = h11.Connection(h11.CLIENT, max_incomplete_event_size=256 * 1024)
        self.status: int | None = None
        self.headers: list[tuple[bytes, bytes]] = []

    @classmethod
    async def ask(cls, scope: dict, receive: Any, upstream: int, port: int, target: bytes,
                  kept: bool, sent: list[tuple[bytes, bytes]] | None = None) -> _Answer | None:
        """None when nothing answers on the port. `receive` None: again, without a body (GET, HEAD). `sent`: the
        headers to send, when they're not the app's (_request_headers)."""
        opened = await _connect(upstream)
        if opened is None:
            return None
        self = cls(*opened)
        headers = sent or _request_headers(scope, port, kept)
        names = {name for name, _ in scope["headers"]}
        if receive is None:
            headers = [(name, value) for name, value in headers if name != b"content-length"]
        elif b"content-length" not in names and b"transfer-encoding" in names:
            headers.append((b"transfer-encoding", b"chunked"))
        query = scope.get("query_string") or b""
        try:
            self._send(h11.Request(method=scope["method"], target=target + (b"?" + query if query else b""),
                                   headers=[*headers, (b"connection", b"close")]))
            while receive is not None:
                message = await receive()
                if message["type"] == "http.disconnect":
                    break
                if message.get("body"):
                    self._send(h11.Data(data=message["body"]))
                    await self.writer.drain()
                if not message.get("more_body"):
                    break
            self._send(h11.EndOfMessage())
            await self.writer.drain()
            event = await asyncio.wait_for(self._response(), ANSWER_TIME)
        except asyncio.TimeoutError:
            self.close()
            return self
        except (h11.ProtocolError, OSError):
            self.close()
            return None
        self.status, self.headers = event.status_code, list(event.headers)
        return self

    def _send(self, event: Any) -> None:
        data = self.h11.send(event)
        if data:
            self.writer.write(data)

    async def _next(self) -> Any:
        while True:
            event = self.h11.next_event()
            if event is not h11.NEED_DATA:
                return event
            self.h11.receive_data(await self.reader.read(CHUNK))

    async def _response(self) -> h11.Response:
        while True:
            event = await self._next()
            if isinstance(event, h11.Response):
                return event
            if not isinstance(event, h11.InformationalResponse):  # (100 Continue, 103 Early Hints)
                raise h11.RemoteProtocolError("no answer")

    async def body(self):
        while True:
            event = await self._next()
            if isinstance(event, h11.Data):
                yield bytes(event.data)
            elif isinstance(event, (h11.EndOfMessage, h11.ConnectionClosed)):
                return

    def close(self) -> None:
        self.writer.close()


def _foreign(scope: dict) -> bool:
    """A request another site's page made (another port of this machine's is the same site, but not
    Relay), other than a link followed: /port/ has no CSRF header to guard it, as Relay's API has, so the
    browser's word on where it comes from does. A link from elsewhere still opens."""
    headers = HTTPConnection(scope).headers
    if headers.get("sec-fetch-site") not in ("same-site", "cross-site"):
        return False
    return not (headers.get("sec-fetch-mode") == "navigate" and scope["method"] in ("GET", "HEAD"))


def _signed_in(relay: Relay, scope: dict) -> bool:
    return not relay.login_required or _user(relay, scope) is not None


def _user(relay: Relay, scope: dict) -> dict | None:
    if not relay.login_required:
        return relay.accounts.local_user()
    return relay.accounts.user_for_token(HTTPConnection(scope).cookies.get(COOKIE))


def _upstream(relay: Relay, port: int) -> int | None:
    """The port that answers for `port`: a service's public one is its live version's (None: none live)."""
    service = relay.db.one("SELECT id FROM services WHERE public_port = ?", (port,))
    if service is None:
        return port
    proxy = relay.production.proxies.get(service["id"])
    return proxy.target if proxy is not None else None


async def _connect(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    for host in ("127.0.0.1", "::1"):  # (a server on "localhost" may listen on IPv6 only: Vite)
        try:
            return await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIME)
        except (OSError, asyncio.TimeoutError):
            continue
    return None


def _request_headers(scope: dict, port: int, kept: bool) -> list[tuple[bytes, bytes]]:
    """The browser's headers as the app should see them: addressed directly at localhost:<port>."""
    here = f"localhost:{port}"
    connection = HTTPConnection(scope)
    own = {connection.headers.get("host"), connection.headers.get("x-forwarded-host")} - {None}
    same = connection.headers.get("sec-fetch-site") == "same-origin" \
        or urllib.parse.urlsplit(connection.headers.get("origin", "")).netloc in own
    proto = connection.headers.get("x-forwarded-proto") or {"ws": "http", "wss": "https"}.get(
        scope.get("scheme", "http"), scope.get("scheme", "http"))
    found = [(b"host", here.encode())]
    for name, value in scope["headers"]:
        if name in DROPPED or name == b"host" or name.startswith(b"x-forwarded-"):
            continue
        if name == b"cookie":
            value = "; ".join(part.strip() for part in value.decode("latin-1").split(";")
                              if part.strip() and part.split("=", 1)[0].strip() != COOKIE).encode("latin-1")
            if not value:
                continue
        elif name == b"origin" and same:
            value = f"http://{here}".encode()
        elif name == b"referer":
            parts = urllib.parse.urlsplit(value.decode("latin-1"))
            prefix, page = f"/port/{port}", _relays(scope, parts.path)
            if not (page == prefix or page.startswith(prefix + "/")):
                continue  # (not this app's page)
            path = page if kept else page[len(prefix):] or "/"
            value = urllib.parse.urlunsplit(("http", here, path, parts.query, "")).encode("latin-1")
        found.append((name, value))
    client = scope.get("client")
    if client:
        found.append((b"x-forwarded-for", str(client[0]).encode()))
    found += [(b"x-forwarded-proto", proto.encode()), (b"x-forwarded-prefix", f"/port/{port}".encode())]
    return found


def _mounted_headers(scope: dict, port: int) -> list[tuple[bytes, bytes]]:
    """The browser's headers for a plugin's web app: as for a port's (_request_headers), its own page the
    Referer, and no /port/ prefix said."""
    found = [(name, value) for name, value in _request_headers(scope, port, True)
             if name not in (b"referer", b"x-forwarded-prefix")]
    referer = HTTPConnection(scope).headers.get("referer")
    if referer:
        parts = urllib.parse.urlsplit(referer)
        found.append((b"referer", urllib.parse.urlunsplit(
            ("http", f"localhost:{port}", _relays(scope, parts.path), parts.query, "")).encode("latin-1")))
    return found


def _mounted_location(value: str, port: int, base: str = "") -> str:
    """A plugin's web app's redirect, to where the browser finds it: its own address taken off."""
    parts = urllib.parse.urlsplit(value)
    if parts.scheme or parts.netloc:
        try:
            ours = parts.hostname in LOOPBACK and parts.port == port
        except ValueError:
            ours = False
        if not ours:
            return value  # (elsewhere)
        value = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, parts.fragment))
    return base + value if value.startswith("/") and not value.startswith("//") else value


def _trial_headers(scope: dict, port: int, token: str, prefix: str) -> list[tuple[bytes, bytes]]:
    """The browser's headers as a trial Relay should see them: addressed directly, with its own session (the
    browser's is Relay's), and the path it's at."""
    found = []
    session = f"{COOKIE}={token}"
    for name, value in _request_headers(scope, port, False):
        if name == b"x-forwarded-prefix":
            value = prefix.encode()
        elif name == b"cookie":
            continue
        found.append((name, value))
    return [*found, (b"cookie", session.encode("latin-1"))]


def _trial_location(value: str, port: int, prefix: str) -> str:
    """A trial Relay's redirect, to where the browser finds it: its own paths have its base already; a URL of
    its own address (Starlette's, from the Host it was sent) is put under it."""
    parts = urllib.parse.urlsplit(value)
    if parts.scheme or parts.netloc:
        try:
            ours = parts.hostname in LOOPBACK and parts.port == port
        except ValueError:
            ours = False
        if not ours:
            return value
        value = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, parts.fragment))
    if value.startswith("/") and not value.startswith("//") and value != prefix and not value.startswith(prefix + "/"):
        return prefix + value
    return value


async def _json(send: Any, status: int, data: dict) -> None:
    body = json.dumps(data).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _wants_prefix(answer: _Answer, port: int, root: bool) -> bool:
    """Whether the app lives under /port/<n>/ itself: it sends the browser there, or has nothing at its root."""
    if answer.status in REDIRECTS:
        location = dict(answer.headers).get(b"location", b"").decode("latin-1")
        path = urllib.parse.urlsplit(location).path
        return path == f"/port/{port}" or path.startswith(f"/port/{port}/")
    return answer.status == 404 and root


def _location(value: str, port: int, upstream: int, base: str = "") -> str:
    """A redirect of the app's, to where the browser finds it: under /port/<n>/ (of Relay's base path)."""
    parts = urllib.parse.urlsplit(value)
    if parts.scheme or parts.netloc:
        try:
            ours = parts.hostname in LOOPBACK and parts.port in (port, upstream)
        except ValueError:
            ours = False
        if not ours:
            return value  # (elsewhere)
        value = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, parts.fragment))
    prefix = f"/port/{port}"
    if value.startswith("/") and not value.startswith("//") and value != prefix \
            and not value.startswith((prefix + "/", prefix + "?")):
        return base + prefix + value
    return base + value if value.startswith(prefix) else value


async def _handshake(ws: WSConnection, reader: asyncio.StreamReader) -> AcceptConnection | RejectConnection | None:
    while True:
        chunk = await reader.read(CHUNK)
        ws.receive_data(chunk or None)
        for event in ws.events():
            if isinstance(event, (AcceptConnection, RejectConnection)):
                return event
        if not chunk:
            return None


async def _gone(receive: Any) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


def _base(scope: dict) -> str:
    """The path Relay is served under on its site (Settings.base_path): its paths as the browser has them start
    with it, the ones it's asked for don't (a proxy took it off)."""
    return scope["app"].state.relay.settings.base_path


def _relays(scope: dict, path: str) -> str:
    """A path as the browser has it (a page's Referer), as Relay names it."""
    base = _base(scope)
    return path[len(base):] or "/" if base and (path == base or path.startswith(f"{base}/")) else path


async def _redirect(scope: dict, send: Any, status: int, location: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    if location.startswith("/"):  # (one of Relay's own paths: as the browser asks for it)
        location = _base(scope) + location
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"location", location.encode("latin-1")), (b"content-length", b"0")]})
    await send({"type": "http.response.body", "body": b""})


async def _page(scope: dict, send: Any, status: int, title: str, text: str) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008 if status < 500 else 1011})
        return
    body = error_page(status, title, text, scope.get("path", ""), _base(scope)).encode()
    # (No referrer: its stylesheet and script then come from Relay, not the port: see _elsewhere.)
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/html; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store"), (b"content-security-policy", CSP.encode()),
                            (b"referrer-policy", b"no-referrer"), (b"x-content-type-options", b"nosniff")]})
    await send({"type": "http.response.body", "body": body})
