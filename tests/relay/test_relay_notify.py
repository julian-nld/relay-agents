import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from relay.config import Settings
from relay.notify import Finished, Notifier, message


class Catcher(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.received.append((self.path, dict(self.headers), body))
        self.send_response(500 if self.path == "/broken" else 200)
        self.end_headers()


@pytest.fixture
def catcher():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Catcher)
    server.received = []
    server.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def wait_for(catcher, count):
    deadline = time.time() + 5
    while len(catcher.received) < count and time.time() < deadline:
        time.sleep(0.05)
    return catcher.received


def finished(**kw):
    base = {"conversation_id": "abc", "title": "Fix the tests", "mode": "code", "status": "succeeded",
            "seconds": 120, "text": "All tests pass now.", "changes": {"files": 2, "additions": 5, "deletions": 1}}
    return Finished(**{**base, **kw})


def test_ntfy_and_webhook_get_the_run(catcher):
    settings = Settings(ntfy_url=catcher.url + "/topic", ntfy_token="tk", webhook_url=catcher.url + "/hook",
                        public_url="http://pi.local:8420/")
    Notifier(settings).run_finished(finished())
    received = {path: (headers, body) for path, headers, body in wait_for(catcher, 2)}
    headers, body = received["/topic"]
    assert headers["Title"] == "Done: Fix the tests" and headers["Tags"] == "white_check_mark"
    assert headers["Markdown"] == "yes"  # (an agent's answer, as it's meant: not its **asterisks**)
    assert headers["Click"] == "http://pi.local:8420/c/abc"
    assert headers["Authorization"] == "Bearer tk"
    assert body.decode() == "2 file(s) changed +5 −1. All tests pass now."
    hook = json.loads(received["/hook"][1])
    assert hook["event"] == "run.finished" and hook["status"] == "succeeded"
    assert hook["conversation"] == {"id": "abc", "title": "Fix the tests", "mode": "code",
                                    "url": "http://pi.local:8420/c/abc"}


def test_quick_and_interrupted_runs_are_quiet(catcher):
    notifier = Notifier(Settings(ntfy_url=catcher.url + "/t", notify_min_seconds=30))
    notifier.run_finished(finished(seconds=5))
    notifier.run_finished(finished(status="interrupted"))
    time.sleep(0.3)
    assert catcher.received == []


def test_failures_and_non_latin_titles(catcher):
    Notifier(Settings(ntfy_url=catcher.url + "/t")).run_finished(
        finished(status="failed", title="Build 🚀 app", error="npm exited with 1"))
    (_, headers, body), = wait_for(catcher, 1)
    assert headers["Title"].startswith("=?UTF-8?B?") and headers["Priority"] == "high"
    assert body.decode() == "npm exited with 1"


def test_test_reports_errors(catcher):
    assert Notifier(Settings(ntfy_url=catcher.url + "/ok")).test() == []
    errors = Notifier(Settings(webhook_url=catcher.url + "/broken")).test()
    assert len(errors) == 1 and errors[0].startswith("webhook:")


def test_message_is_short():
    assert len(message(finished(text="x " * 500, changes=None))) <= 300


def test_categories_decide_what_is_pushed_and_how_loud(catcher):
    settings = Settings(ntfy_url=catcher.url + "/t", webhook_url=catcher.url + "/hook")
    notifier = Notifier(settings)
    # By default the Governor's and its agents' runs aren't pushed; the user's own are
    notifier.run_finished(finished(agentos=True))
    notifier.run_finished(finished(agentos=True, status="failed", error="no"))
    time.sleep(0.3)
    assert catcher.received == []
    notifier.run_finished(finished())
    received = {path: (headers, body) for path, headers, body in wait_for(catcher, 2)}
    assert received["/t"][0]["Priority"] == "default"
    assert json.loads(received["/hook"][1])["category"] == "run.done"
    # Chosen: AgentOS's failures pushed, loud; the user's own done quiet; reports off
    catcher.received.clear()
    settings.notify_categories = {"agents.failed": {"push": True, "priority": "urgent"},
                                  "run.done": {"push": True, "priority": "low"},
                                  "report": {"push": False, "priority": "default"}}
    notifier.run_finished(finished(agentos=True, status="failed", error="no"))
    (_, headers, _), (_, _, body) = sorted(wait_for(catcher, 2), key=lambda r: r[0] != "/t")
    assert headers["Priority"] == "urgent" and json.loads(body)["category"] == "agents.failed"
    catcher.received.clear()
    notifier.report("Did the thing")
    notifier.run_finished(finished())
    (_, headers, _), _ = sorted(wait_for(catcher, 2), key=lambda r: r[0] != "/t")
    assert headers["Priority"] == "low" and headers["Title"] == "Done: Fix the tests"  # (no report)
    time.sleep(0.3)
    assert len(catcher.received) == 2


def test_waiting_for_you_and_interventions_follow_their_category(catcher):
    settings = Settings(ntfy_url=catcher.url + "/t", notify_categories={"intervention": {"push": True, "priority": "urgent"}})
    notifier = Notifier(settings)
    notifier.intervention("The service notes is failing.")
    (_, headers, _), = wait_for(catcher, 1)
    assert headers["Priority"] == "urgent"
    catcher.received.clear()
    settings.notify_categories["approval"] = {"push": False, "priority": "high"}
    notifier.approval_needed("abc", "Fix the tests", "rm build/")
    time.sleep(0.3)
    assert catcher.received == []


def test_vpn_keeps_the_older_setting_until_its_category_is_chosen(catcher):
    settings = Settings(ntfy_url=catcher.url + "/t", notify_vpn=False)
    notifier = Notifier(settings)
    notifier.vpn("home", "It fell back")
    time.sleep(0.3)
    assert catcher.received == [] and not notifier.pushes("vpn")
    settings.notify_categories = {"vpn": {"push": True, "priority": "urgent"}}
    notifier.vpn("home", "It fell back")
    notifier.vpn("home", "Connected again", recovered=True)
    priorities = sorted(h["Priority"] for _, h, _ in wait_for(catcher, 2))
    assert priorities == ["default", "urgent"]  # (connected again: no louder than default)


def test_what_a_script_says(catcher):
    """notify() in a script (Notifier.script): pushed at once, with its title, priority and link; when it can't
    be, why (not set up, turned off, a push service that didn't take it)."""
    settings = Settings(ntfy_url=catcher.url + "/topic", webhook_url=catcher.url + "/hook")
    assert Notifier(settings).script("GPU deal: 300 euro", "Marktplaats", "high", "https://example.com/ad") is None
    received = {path: (headers, body) for path, headers, body in catcher.received}
    headers, body = received["/topic"]
    assert (headers["Title"], headers["Priority"], headers["Click"], body.decode()) == (
        "Marktplaats", "high", "https://example.com/ad", "GPU deal: 300 euro")
    assert json.loads(received["/hook"][1]) == {"event": "script", "category": "script", "title": "Marktplaats",
                                                 "message": "GPU deal: 300 euro", "url": "https://example.com/ad",
                                                 "priority": "high"}
    assert Notifier(Settings(ntfy_url=catcher.url + "/t")).script("Done") is None
    assert catcher.received[-1][1]["Title"] == "From your script" and catcher.received[-1][1]["Priority"] == "default"
    off = Settings(ntfy_url=catcher.url + "/t", notify_categories={"script": {"push": False}})
    assert "off" in Notifier(off).script("Done") and "set up" in Notifier(Settings()).script("Done")
    assert Notifier(Settings(ntfy_url=catcher.url + "/broken")).script("Done").startswith("ntfy: ")


def test_a_discord_webhook_gets_a_message_it_takes(catcher, monkeypatch):
    """Found for real: Discord turned Relay away (403, "error code: 1010": its Cloudflare, for Python's own
    User-Agent), and takes only a message of its own form (content), not Relay's JSON. Every push says it's Relay;
    a Discord webhook gets a message, pinging nobody; any other webhook Relay's JSON, as before."""
    import re

    from relay import notify
    real = notify.DISCORD
    for address in ("https://discord.com/api/webhooks/1/x", "https://discordapp.com/api/webhooks/1/x",
                    "https://canary.discord.com/api/v10/webhooks/1/x"):
        assert real.match(address), address
    assert not real.match("https://example.com/api/webhooks/1/x") and not real.match("https://ntfy.sh/discord.com")
    monkeypatch.setattr(notify, "DISCORD", re.compile(re.escape(catcher.url) + "/api/webhooks/"))  # (the catcher)
    discord = Settings(webhook_url=catcher.url + "/api/webhooks/1/token", public_url="http://pi.local:8420")
    Notifier(discord).run_finished(finished())
    _, headers, body = wait_for(catcher, 1)[0]
    assert headers["User-Agent"].startswith("Relay/") and json.loads(body) == {
        "content": "**Done: Fix the tests**\n2 file(s) changed +5 −1. All tests pass now.\nhttp://pi.local:8420/c/abc",
        "username": "Relay", "allowed_mentions": {"parse": []}}
    assert Notifier(discord).script("@everyone: a GPU for 300 euro", "Deals") is None
    assert json.loads(catcher.received[-1][2])["content"] == "**Deals**\n@everyone: a GPU for 300 euro"
    assert Notifier(discord).test() == []
    assert json.loads(catcher.received[-1][2])["content"].startswith("**Done: Test notification**\nNotifications")
    assert Notifier(Settings(webhook_url=catcher.url + "/hook", ntfy_url=catcher.url + "/t")).script("Done") is None
    assert json.loads(next(b for p, h, b in catcher.received if p == "/hook"))["event"] == "script"
    assert all(h["User-Agent"].startswith("Relay/") for p, h, b in catcher.received)


def test_why_a_push_didnt_go_in_words(catcher):
    """Found by a simulated user: "webhook: Internal Server Error", "webhook: [Errno 111] Connection refused"."""
    import socket
    assert Notifier(Settings(webhook_url=catcher.url + "/broken")).test() == [
        "webhook: it answered 500 Internal Server Error"]
    with socket.socket() as probe:  # (a port nothing listens on)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert Notifier(Settings(webhook_url=f"http://127.0.0.1:{port}/hook")).test() == [
        "webhook: nothing answers at that address (connection refused)"]

