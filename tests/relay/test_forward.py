"""Servers on this machine's ports at /port/<n>/ (relay.forward), against real servers (uvicorn on
free ports): the app sees itself addressed directly, absolute paths come back under the prefix,
answers stream, WebSockets go both ways."""

import asyncio
import hashlib
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from relay import config, forward
from relay.app import create_app
from relay.db import Database
from relay.providers import Registry

H = {"x-relay-request": "1"}


def serve(make, host="127.0.0.1"):
    """`make(port)`: an ASGI app, served on a free port of `host` (in a thread); returns the port and a stop."""
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(make(port), log_level="warning", lifespan="off", ws="wsproto"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    def stop():
        server.should_exit = True
        thread.join(5)
    return port, stop


def app_at_root(port):
    """An app made for the root of a site: absolute paths, redirects, a stream, a socket."""
    async def page(request):
        return HTMLResponse("<script src='/assets/app.js'></script><p>the app</p>")

    async def seen(request):
        return JSONResponse({"path": request.url.path, "query": request.url.query,
                             "headers": dict(request.headers.items())})

    async def echo(request):
        data = await request.body()
        return JSONResponse({"length": len(data), "sha": hashlib.sha256(data).hexdigest()})

    async def signin(request):  # (cookies as an app sets them that names their domain after its Host)
        answer = PlainTextResponse("in")
        answer.set_cookie("session", "s1", domain="localhost", httponly=True)
        answer.set_cookie("theme", "dark")
        answer.set_cookie("elsewhere", "e1", domain="example.org")
        return answer

    async def events(request):
        async def stream():
            yield b"data: 1\n\n"
            await asyncio.sleep(1.5)
            yield b"data: 2\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream")

    async def socket_(ws):
        await ws.accept(subprotocol="vite-hmr" if "vite-hmr" in ws.scope.get("subprotocols", []) else None)
        await ws.send_text("hello")
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("text") == "bye":
                await ws.close(4000)
                return
            if message.get("text") is not None:
                await ws.send_text(f"echo {message['text']}")
            else:
                await ws.send_bytes(message["bytes"][::-1])
    return Starlette(routes=[
        Route("/", page), Route("/seen", seen), Route("/echo", echo, methods=["POST"]), Route("/events", events),
        Route("/go", lambda r: RedirectResponse("/login", 302)),
        Route("/abs", lambda r: RedirectResponse(f"http://localhost:{port}/x?y=1", 302)),
        Route("/away", lambda r: RedirectResponse("https://example.org/", 302)),
        Route("/missing", lambda r: PlainTextResponse("no", 404)),
        Route("/signin", signin),
        WebSocketRoute("/", socket_), WebSocketRoute("/ws", socket_),
    ])


def app_under_prefix(redirects):
    """An app that lives under /port/<n>/ itself: Vite's --base (its root redirects there) or Next's
    basePath (a 404 at its root)."""
    def make(port):
        async def page(request):
            return HTMLResponse(f"<p>based {request.url.path}</p>")
        root = (lambda r: RedirectResponse(f"/port/{port}/", 302)) if redirects else \
            (lambda r: PlainTextResponse("not here", 404))
        return Starlette(routes=[Route("/", root), Route(f"/port/{port}/", page), Route(f"/port/{port}/x", page)])
    return make


@pytest.fixture
def upstream():
    port, stop = serve(app_at_root)
    yield port
    stop()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    settings = config.Settings(workspace_roots=[str(tmp_path / "ws")], chat_root=str(tmp_path / "chats"))
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults()
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    with TestClient(app) as client:
        assert client.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H).status_code == 200
        yield client


def test_the_app_sees_itself_addressed_directly(env, upstream):
    env.cookies.set("theme", "dark")
    seen = env.get(f"/port/{upstream}/seen?a=1", headers={
        "origin": "http://testserver", "referer": f"http://testserver/port/{upstream}/page?b=2"}).json()
    assert seen["path"] == "/seen" and seen["query"] == "a=1"
    headers = seen["headers"]
    assert headers["host"] == f"localhost:{upstream}" and headers["origin"] == f"http://localhost:{upstream}"
    assert headers["referer"] == f"http://localhost:{upstream}/page?b=2"
    assert headers["cookie"] == "theme=dark"  # (not Relay's session)
    assert headers["x-forwarded-prefix"] == f"/port/{upstream}" and headers["x-forwarded-proto"] == "http"
    # Another site's Origin isn't made the app's own
    other = env.get(f"/port/{upstream}/seen", headers={"origin": "https://elsewhere.example"}).json()
    assert other["headers"]["origin"] == "https://elsewhere.example"


def test_pages_their_redirects_and_relays_own_headers(env, upstream):
    page = env.get(f"/port/{upstream}/")
    assert page.status_code == 200 and "the app" in page.text
    assert "content-security-policy" not in page.headers and "x-frame-options" not in page.headers
    assert page.headers["referrer-policy"] == "same-origin"
    assert f"relay_port={upstream}" in page.headers["set-cookie"]
    assert "set-cookie" not in env.get(f"/port/{upstream}/seen").headers  # (only for pages)
    # Its cookies: one it set for localhost (where Relay told it it is) is the browser's host's
    cookies = env.get(f"/port/{upstream}/signin").headers.get_list("set-cookie")
    assert [c.split(";")[0] for c in cookies] == ["session=s1", "theme=dark", "elsewhere=e1"]
    assert "domain" not in cookies[0].lower() and "httponly" in cookies[0].lower()
    assert "domain=example.org" in cookies[2].lower()
    assert env.get(f"/port/{upstream}", follow_redirects=False).headers["location"] == f"/port/{upstream}/"
    assert env.get(f"/port/{upstream}/go", follow_redirects=False).headers["location"] == f"/port/{upstream}/login"
    assert env.get(f"/port/{upstream}/abs", follow_redirects=False).headers["location"] == f"/port/{upstream}/x?y=1"
    assert env.get(f"/port/{upstream}/away", follow_redirects=False).headers["location"] == "https://example.org/"
    # A body goes up whole
    data = bytes(range(256)) * 1000
    assert env.post(f"/port/{upstream}/echo", content=data).json() == {
        "length": len(data), "sha": hashlib.sha256(data).hexdigest()}


def test_absolute_paths_of_a_forwarded_page_come_back_under_its_prefix(env, upstream):
    asked = env.post("/api/thing?x=1", headers={"referer": f"http://testserver/port/{upstream}/page"},
                     follow_redirects=False)
    assert asked.status_code == 307 and asked.headers["location"] == f"/port/{upstream}/api/thing?x=1"
    script = env.get("/assets/app.js", headers={"referer": f"https://relay.example/port/{upstream}/"},
                     follow_redirects=False)
    assert script.headers["location"] == f"/port/{upstream}/assets/app.js"
    # Relay's own pages send no Referer: Relay's routes are Relay's
    assert env.get("/api/state").json()["server"]["port"] == 8420


def test_refusals_relay_itself_and_nothing_there(env, upstream):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        empty = sock.getsockname()[1]
    nothing = env.get(f"/port/{empty}/")
    assert nothing.status_code == 502 and "Nothing answers" in nothing.text
    # Relay's page, whose stylesheet comes from Relay: no referrer to send it to the port (_elsewhere)
    assert 'href="/static/css/app.css"' in nothing.text and nothing.headers["referrer-policy"] == "no-referrer"
    assert env.get("/port/8420/c/abc?x=1", follow_redirects=False).headers["location"] == "/c/abc?x=1"
    assert env.get("/port/70000/").status_code == 404
    # Another site's page (or another port's: the same site) can't use it; a link from there still opens
    assert env.post(f"/port/{upstream}/echo", content=b"x", headers={"sec-fetch-site": "same-site",
                                                                     "sec-fetch-mode": "no-cors"}).status_code == 403
    assert env.get(f"/port/{upstream}/seen", headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "cors"}
                   ).status_code == 403
    assert env.get(f"/port/{upstream}/", headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}
                   ).status_code == 200
    env.cookies.clear()
    assert env.get(f"/port/{upstream}/", follow_redirects=False).headers["location"] == "/"
    assert env.post(f"/port/{upstream}/echo", content=b"x").status_code == 401
    with pytest.raises(WebSocketDisconnect), env.websocket_connect(f"/port/{upstream}/ws"):
        pass


@pytest.mark.parametrize("redirects", [True, False], ids=["vite-base", "next-basepath"])
def test_an_app_that_lives_under_the_prefix_itself_is_noticed(env, redirects):
    port, stop = serve(app_under_prefix(redirects))
    try:
        page = env.get(f"/port/{port}/")
        assert page.status_code == 200 and page.text == f"<p>based /port/{port}/</p>"
        assert env.app.middleware_stack.app.kept == {port}  # (remembered: asked with the prefix from now on)
        assert env.get(f"/port/{port}/x").text == f"<p>based /port/{port}/x</p>"
    finally:
        stop()
    env.get(f"/port/{port}/")  # nothing answers there now: forgotten
    assert port not in env.app.middleware_stack.app.kept


def test_a_productionbox_public_port_goes_to_its_live_version(env, upstream):
    relay = env.app.state.relay
    user = relay.db.one("SELECT id FROM users")["id"]
    relay.db.execute("INSERT INTO services (id, user_id, name, command, public_port, created) "
                     "VALUES ('svc1', ?, 'notes', 'x', 8555, 1)", (user,))
    relay.production.proxies["svc1"] = SimpleNamespace(target=upstream, close=lambda: None)
    assert env.get("/port/8555/seen").json()["headers"]["host"] == "localhost:8555"
    assert env.get("/port/8555/abs", follow_redirects=False).headers["location"] == "/port/8555/x?y=1"
    relay.production.proxies["svc1"].target = None
    assert env.get("/port/8555/").status_code == 502


def test_websockets_both_ways_and_from_the_root(env, upstream):
    with env.websocket_connect(f"/port/{upstream}/ws", subprotocols=["vite-hmr"]) as ws:
        assert ws.accepted_subprotocol == "vite-hmr"
        assert ws.receive_text() == "hello"
        ws.send_text("hi")
        assert ws.receive_text() == "echo hi"
        ws.send_bytes(b"abc")
        assert ws.receive_bytes() == b"cba"
        ws.send_text("bye")
        assert ws.receive()["code"] == 4000
    # A socket at the root (Vite's live reloading, no base): to the port of the last page forwarded
    env.get(f"/port/{upstream}/")
    with env.websocket_connect("/?token=t", subprotocols=["vite-hmr"]) as ws:
        assert ws.receive_text() == "hello"
        ws.send_text("again")
        assert ws.receive_text() == "echo again"
        ws.send_text("bye")  # (the app ends it: the test client doesn't wait for a forwarding to wind down)
        assert ws.receive()["code"] == 4000
    # Relay's own pages (the forwarded app's too) may; another site's, with the user's cookie, may not
    with env.websocket_connect(f"/port/{upstream}/ws", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_text() == "hello"
        ws.send_text("bye")
        assert ws.receive()["code"] == 4000
    with pytest.raises(WebSocketDisconnect), env.websocket_connect(f"/port/{upstream}/ws",
                                                                   headers={"origin": "https://evil.example"}):
        pass


def test_answers_stream():
    """Server-sent events: the first arrives before the stream ends."""
    port, stop = serve(app_at_root)
    relay = SimpleNamespace(login_required=False, settings=SimpleNamespace(port=8420),
                            db=SimpleNamespace(one=lambda *a: None), production=SimpleNamespace(proxies={}))
    forwarding = forward.Forwarding(None)

    async def run():
        got = []
        started = time.monotonic()
        scope = {"type": "http", "method": "GET", "path": f"/port/{port}/events", "raw_path": f"/port/{port}/events".encode(),
                 "query_string": b"", "headers": [(b"host", b"relay")], "scheme": "http", "client": ("10.0.0.2", 1),
                 "app": SimpleNamespace(state=SimpleNamespace(relay=relay))}
        asked = asyncio.Event()

        async def receive():
            if not asked.is_set():
                asked.set()
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                got.append((round(time.monotonic() - started, 1), message["body"]))
        await forwarding(scope, receive, send)
        return got
    try:
        got = asyncio.run(run())
    finally:
        stop()
    assert [body for _, body in got] == [b"data: 1\n\n", b"data: 2\n\n"]
    assert got[0][0] < 1 and got[1][0] >= 1.4


@pytest.mark.skipif(not socket.has_ipv6, reason="no IPv6")
def test_a_server_on_ipv6_loopback_only(env):
    try:
        port, stop = serve(app_at_root, "::1")
    except OSError:
        pytest.skip("no IPv6 loopback")
    try:
        assert "the app" in env.get(f"/port/{port}/").text
    finally:
        stop()


def test_relay_under_a_path_of_its_site(tmp_path, monkeypatch, upstream):
    """Settings.base_path: Relay behind a proxy that serves it at /relay/ (and takes that off): its page, its error
    page, the links it makes and its redirects have the path; the paths it's asked for don't."""
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    with pytest.raises(ValueError, match="base_path"):
        config.Settings(base_path="relay")
    settings = config.Settings(workspace_roots=[str(tmp_path / "ws")], chat_root=str(tmp_path / "chats"),
                               base_path="/relay/")
    assert settings.base_path == "/relay"
    app = create_app(settings, Database(tmp_path / "relay.db"), Registry(tmp_path / "providers"))
    with TestClient(app) as client:
        page = client.get("/").text
        assert '<meta name="relay-base" content="/relay">' in page and "@BASE@" not in page
        assert 'src="/relay/static/js/main.js"' in page and 'href="/static/' not in page
        missing = client.get("/nothing-here", headers={"accept": "text/html"})
        assert missing.status_code == 404 and 'href="/relay/static/css/app.css"' in missing.text
        assert 'href="/relay/"' in missing.text
        assert client.post("/api/setup", json={"username": "me", "password": "correct horse"},
                           headers=H).status_code == 200
        assert client.get(f"/port/{upstream}", follow_redirects=False).headers["location"] == \
            f"/relay/port/{upstream}/"
        assert client.get(f"/port/{upstream}/go", follow_redirects=False).headers["location"] == \
            f"/relay/port/{upstream}/login"
        # An absolute path of a forwarded page's (its Referer, as the browser has it, with the path)
        assert client.get("/assets/app.js", follow_redirects=False,
                          headers={"referer": f"http://testserver/relay/port/{upstream}/"}
                          ).headers["location"] == f"/relay/port/{upstream}/assets/app.js"
        assert "/relay/sdk/" in client.get("/sdk/").text
    plain = create_app(config.Settings(workspace_roots=[str(tmp_path / "ws")]), Database(tmp_path / "other.db"),
                       Registry(tmp_path / "providers"))
    with TestClient(plain) as client:
        page = client.get("/").text
        assert '<meta name="relay-base" content="">' in page and 'src="/static/js/main.js"' in page
