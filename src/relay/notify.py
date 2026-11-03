"""Push notifications: ntfy and webhooks (Discord's too), by category (Settings → Notifications).

Browsers only allow system notifications on HTTPS or localhost, and Relay
usually runs on plain HTTP in the local network. A push service reaches a
phone even when Relay isn't open: ntfy (https://ntfy.sh or self-hosted,
with the ntfy app) or any webhook.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from relay import __version__
from relay.config import Settings

TIMEOUT = 10

# What Relay pushes, each to be turned on or off with its ntfy priority (Settings → Notifications; the
# user's choice in Settings.notify_categories). A browser's own choice of what it announces is kept in
# that browser (static/js/notifications.js).
CATEGORIES = (
    {"id": "run.done", "label": "Your chats and tasks: done", "push": True, "priority": "default",
     "hint": "A run that took longer than the time below."},
    {"id": "run.failed", "label": "Your chats and tasks: failed", "push": True, "priority": "high",
     "hint": "A run that failed."},
    {"id": "approval", "label": "Waiting for you", "push": True, "priority": "high",
     "hint": "An agent asks you something, or to be allowed something."},
    {"id": "agents.done", "label": "AgentOS: done", "push": False, "priority": "low",
     "hint": "A run of the Governor's, or of one of its agents."},
    {"id": "agents.failed", "label": "AgentOS: failed", "push": False, "priority": "default",
     "hint": "One of those that failed."},
    {"id": "report", "label": "Reports from the Governor", "push": True, "priority": "default",
     "hint": "What it did or found: nothing for you to do."},
    {"id": "intervention", "label": "Something needs you", "push": True, "priority": "high",
     "hint": "The Governor needs your decision; a service or a provider keeps failing."},
    {"id": "vpn", "label": "VPN tunnel problems", "push": True, "priority": "high",
     "hint": "A tunnel fell back, or none of its exits connects; and once it's connected again."},
    {"id": "script", "label": "From your scripts", "push": True, "priority": "default",
     "hint": "What a script of yours says: notify(…) in Relay's SDK."},
)
CATEGORY = {c["id"]: c for c in CATEGORIES}
INTERVENTION, REPORT = "Intervention needed (AgentOS)", "From the Governor (AgentOS)"  # (their titles)
DISCORD = re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/")
PRIORITIES = ("min", "low", "default", "high", "urgent")  # ntfy's


@dataclass
class Finished:
    conversation_id: str
    title: str
    mode: str
    status: str  # succeeded | failed | interrupted
    seconds: float
    text: str
    error: str | None = None
    changes: dict | None = None
    agentos: bool = False  # the Governor's run, or one of its agents' (a managed chat)

    @property
    def category(self) -> str:
        return ("agents" if self.agentos else "run") + (".done" if self.status == "succeeded" else ".failed")


class Notifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def configured(self) -> bool:
        return bool(self.settings.ntfy_url or self.settings.webhook_url)

    def wants(self, category: str) -> bool:
        """Whether to push this category now: the user wants it, and there's somewhere to push it."""
        return self.configured() and self.pushes(category)

    def pushes(self, category: str) -> bool:
        """Whether the user wants this category pushed: as they chose, else its default."""
        default = self.settings.notify_vpn if category == "vpn" else CATEGORY[category]["push"]  # (an older setting)
        return bool(self.chosen(category).get("push", default))

    def priority(self, category: str) -> str:
        chosen = self.chosen(category).get("priority")
        return chosen if chosen in PRIORITIES else CATEGORY[category]["priority"]

    def chosen(self, category: str) -> dict[str, Any]:
        chosen = (self.settings.notify_categories or {}).get(category)
        return chosen if isinstance(chosen, dict) else {}

    def run_finished(self, finished: Finished) -> None:
        """Notify in the background, for runs that took long enough."""
        if finished.status == "interrupted" or not self.wants(finished.category):
            return
        if finished.seconds < self.settings.notify_min_seconds:
            return
        threading.Thread(target=self._send_all, args=(finished,), daemon=True).start()

    def approval_needed(self, conversation_id: str, title: str, what: str) -> None:
        """Tell the user an agent waits for them to allow something."""
        if not self.wants("approval"):
            return
        threading.Thread(target=self._send_approval, args=(conversation_id, title, what), daemon=True).start()

    def _send_approval(self, conversation_id: str, title: str, what: str) -> None:
        link, heading = self._link(conversation_id), f"Waiting for you: {title}"
        try:
            if self.settings.ntfy_url:
                headers = {"Title": _header(heading), "Tags": "raising_hand",
                           "Priority": self.priority("approval")}
                if link:
                    headers["Click"] = link
                if self.settings.ntfy_token:
                    headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
                _post(self.settings.ntfy_url, f"Allow {what}?"[:300].encode(), headers)
            if self.settings.webhook_url:
                payload = {"event": "run.approval", "category": "approval", "conversation": {
                           "id": conversation_id, "title": title, "url": link}, "message": f"Allow {what}?"[:300]}
                _hook(self.settings.webhook_url, payload, heading, payload["message"], link)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"relay: notification failed ({_why(exc)})", file=sys.stderr, flush=True)

    def intervention(self, text: str, conversation_id: str | None = None) -> None:
        """AgentOS's Governor needs the user (relay.agentos)."""
        if not self.wants("intervention"):
            return
        threading.Thread(target=self._send_intervention, args=(text, conversation_id), daemon=True).start()

    def _send_intervention(self, text: str, conversation_id: str | None) -> None:
        link = self._link(conversation_id) if conversation_id else None
        try:
            if self.settings.ntfy_url:
                headers = {"Title": _header(INTERVENTION), "Tags": "warning", "Markdown": "yes",
                           "Priority": self.priority("intervention")}
                if link:
                    headers["Click"] = link
                if self.settings.ntfy_token:
                    headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
                _post(self.settings.ntfy_url, text[:1000].encode(), headers)
            if self.settings.webhook_url:
                payload = {"event": "agentos.intervention", "category": "intervention", "message": text[:2000],
                           "conversation": {"id": conversation_id, "url": link} if conversation_id else None}
                _hook(self.settings.webhook_url, payload, INTERVENTION, text, link)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"relay: notification failed ({_why(exc)})", file=sys.stderr, flush=True)

    def report(self, text: str, conversation_id: str | None = None) -> None:
        """AgentOS's Governor tells the user what's done or found: nothing for them to do (relay.agentos)."""
        if not self.wants("report"):
            return
        threading.Thread(target=self._send_report, args=(text, conversation_id), daemon=True).start()

    def _send_report(self, text: str, conversation_id: str | None) -> None:
        link = self._link(conversation_id) if conversation_id else None
        try:
            if self.settings.ntfy_url:
                headers = {"Title": _header(REPORT), "Tags": "memo", "Markdown": "yes",
                           "Priority": self.priority("report")}
                if link:
                    headers["Click"] = link
                if self.settings.ntfy_token:
                    headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
                _post(self.settings.ntfy_url, text[:1000].encode(), headers)
            if self.settings.webhook_url:
                payload = {"event": "agentos.report", "category": "report", "message": text[:2000],
                           "conversation": {"id": conversation_id, "url": link} if conversation_id else None}
                _hook(self.settings.webhook_url, payload, REPORT, text, link)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"relay: notification failed ({_why(exc)})", file=sys.stderr, flush=True)

    def vpn(self, tunnel: str, text: str, recovered: bool = False) -> None:
        """A VPN tunnel's problem (relay.vpn: it fell back, nothing connects), or that it's connected
        again after one."""
        if not self.wants("vpn"):
            return
        threading.Thread(target=self._send_vpn, args=(tunnel, text, recovered), daemon=True).start()

    def _send_vpn(self, tunnel: str, text: str, recovered: bool) -> None:
        link = self.settings.public_url.rstrip("/") or None  # (Relay: the tunnel's in Settings → Workspaces → VPN)
        heading = f"VPN {tunnel}: " + ("connected again" if recovered else "a problem")
        try:
            if self.settings.ntfy_url:
                chosen = self.priority("vpn")  # (connected again: no louder than default)
                quieter = min(chosen, "default", key=PRIORITIES.index)
                headers = {"Title": _header(heading),
                           "Tags": "white_check_mark" if recovered else "warning",
                           "Priority": quieter if recovered else chosen}
                if link:
                    headers["Click"] = link
                if self.settings.ntfy_token:
                    headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
                _post(self.settings.ntfy_url, text[:1000].encode(), headers)
            if self.settings.webhook_url:
                payload = {"event": "vpn.recovered" if recovered else "vpn.problem", "category": "vpn",
                           "tunnel": tunnel,
                           "message": text[:2000]}
                _hook(self.settings.webhook_url, payload, heading, text, link)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"relay: notification failed ({_why(exc)})", file=sys.stderr, flush=True)

    def script(self, text: str, title: str | None = None, priority: str | None = None,
               url: str | None = None) -> str | None:
        """What a script of the user's says (the SDK's notify(), POST /api/notifications), pushed now: None when
        it went, else why not (not set up, turned off, or a push service that didn't take it)."""
        if not self.configured():
            return "Notifications aren't set up (Settings → Notifications: ntfy or a webhook)"
        if not self.pushes("script"):
            return "Notifications from scripts are off (Settings → Notifications)"
        title = title or "From your script"
        sends = []
        if self.settings.ntfy_url:
            headers = {"Title": _header(title), "Tags": "scroll", "Markdown": "yes",
                       "Priority": priority if priority in PRIORITIES else self.priority("script")}
            if url:
                headers["Click"] = url
            if self.settings.ntfy_token:
                headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
            sends.append(("ntfy", self.settings.ntfy_url, text[:1000].encode(), headers))
        if self.settings.webhook_url:
            payload = {"event": "script", "category": "script", "title": title, "message": text[:2000], "url": url,
                       "priority": priority if priority in PRIORITIES else self.priority("script")}
            sends.append(("webhook", self.settings.webhook_url, _hooked(self.settings.webhook_url, payload, title,
                                                                         text, url),
                          {"Content-Type": "application/json"}))
        errors = []
        for target, where, data, headers in sends:
            try:
                _post(where, data, headers)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                errors.append(f"{target}: {_why(exc)}")
        return "; ".join(errors) or None

    def test(self) -> list[str]:
        """Send a test notification to every configured target; returns errors."""
        sample = Finished("test", "Test notification", "chat", "succeeded", 0,
                          "Notifications from Relay work.")
        return self._send_all(sample, report=True)

    def _send_all(self, finished: Finished, report: bool = False) -> list[str]:
        errors = []
        for target, send in (("ntfy", self._ntfy), ("webhook", self._webhook)):
            try:
                send(finished)
            except _Skip:
                continue
            except (urllib.error.URLError, OSError, ValueError) as exc:
                message = f"{target}: {_why(exc)}"
                errors.append(message)
                if not report:
                    print(f"relay: notification failed ({message})", file=sys.stderr, flush=True)
        return errors

    def link(self, finished: Finished) -> str | None:
        return self._link(finished.conversation_id)

    def _link(self, conversation_id: str) -> str | None:
        base = self.settings.public_url.rstrip("/")
        if not base or conversation_id == "test":
            return base or None
        return f"{base}/c/{conversation_id}"

    def _ntfy(self, finished: Finished) -> None:
        url = self.settings.ntfy_url
        if not url:
            raise _Skip
        ok = finished.status == "succeeded"
        headers = {
            "Title": _header(titled(finished)),
            "Tags": "white_check_mark" if ok else "x",
            "Markdown": "yes",  # (an agent's answer: ntfy shows it as it's meant, not its **asterisks**)
            "Priority": self.priority(finished.category),
        }
        if link := self.link(finished):
            headers["Click"] = link
        if self.settings.ntfy_token:
            headers["Authorization"] = f"Bearer {self.settings.ntfy_token}"
        _post(url, message(finished).encode(), headers)

    def _webhook(self, finished: Finished) -> None:
        url = self.settings.webhook_url
        if not url:
            raise _Skip
        payload = {
            "event": "run.finished",
            "category": finished.category,
            "status": finished.status,
            "conversation": {"id": finished.conversation_id, "title": finished.title,
                             "mode": finished.mode, "url": self.link(finished)},
            "seconds": round(finished.seconds, 1),
            "message": message(finished),
            "changes": finished.changes,
        }
        _hook(url, payload, titled(finished), payload["message"], self.link(finished))


class _Skip(Exception):
    pass


def titled(finished: Finished) -> str:
    """A run's notification's title."""
    return ("Done: " if finished.status == "succeeded" else "Failed: ") + finished.title


def message(finished: Finished) -> str:
    if finished.status != "succeeded":
        return (finished.error or "The run failed.")[:300]
    text = " ".join(finished.text.split())
    if finished.changes and finished.changes.get("files"):
        c = finished.changes
        text = f"{c['files']} file(s) changed +{c['additions']} −{c['deletions']}. " + text
    return (text[:297] + "…") if len(text) > 300 else (text or "Finished.")


def _why(exc: Exception) -> str:
    """Why a push didn't go, in words: what the address answered, or that nothing did ("[Errno 111] Connection
    refused" and a bare "Internal Server Error" were a simulated user's)."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"it answered {exc.code} {exc.reason}"
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ConnectionRefusedError):
        return "nothing answers at that address (connection refused)"
    if isinstance(reason, socket.gaierror):
        return "there's no such address (its name doesn't resolve)"
    if isinstance(reason, TimeoutError) or "timed out" in str(reason):
        return "no answer in time"
    return str(getattr(reason, "strerror", None) or reason)


def _header(value: str) -> str:
    # HTTP headers are Latin-1; ntfy understands RFC 2047 for anything else.
    try:
        value.encode("latin-1")
        return value[:200]
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(value[:200].encode()).decode() + "?="


def _hook(url: str, payload: dict[str, Any], title: str, text: str, link: str | None = None) -> None:
    """A webhook's POST (_hooked)."""
    _post(url, _hooked(url, payload, title, text, link), {"Content-Type": "application/json"})


def _hooked(url: str, payload: dict[str, Any], title: str, text: str, link: str | None) -> bytes:
    """What a webhook gets: Relay's JSON; a Discord webhook, which takes nothing else, a message of its own form
    (the title in bold, the text, the link), pinging nobody (an @everyone in an agent's answer)."""
    if not DISCORD.match(url):
        return json.dumps(payload).encode()
    content = "\n".join([f"**{title}**", text[:1800], *([link] if link else [])])[:2000]
    return json.dumps({"content": content, "username": "Relay", "allowed_mentions": {"parse": []}}).encode()


def _post(url: str, body: bytes, headers: dict[str, str]) -> Any:
    if not url.startswith(("http://", "https://")):
        raise ValueError("the URL must start with http:// or https://")
    # Relay's own name: Discord (behind Cloudflare) turns away Python's own, "Python-urllib" (403, error 1010)
    request = urllib.request.Request(url, data=body, headers={"User-Agent": f"Relay/{__version__}", **headers},
                                     method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()
