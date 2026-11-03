"""Relay's own web search: a local SearXNG (searxng/searxng), started and kept by Relay.

Every agent gets it next to its own search: chat models as the function web_search
(relay.toolbox), agents with a shell as the MCP tool search or `relay search` (relay.delegation,
relay.tools_script), subagents too. It's one of Relay's boxes for the web (relay.webbox: on
demand, no port here, its route this machine's connection or a VPN tunnel's, stopped when unused).

settings.search: {"enabled": true, "tunnel": ""}; the tunnel is web fetch's route too (relay.fetch).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import urllib.parse
from pathlib import Path
from typing import Any

from relay import config, tunables
from relay.webbox import WebBox, WebError

IMAGE = "docker.io/searxng/searxng:latest"
CATEGORIES = ("general", "news", "it", "science")
TIME_RANGES = ("day", "week", "month", "year")
RESULTS = 10  # shown per search
SETTINGS = """\
# Written by Relay (relay/search.py) each time it starts SearXNG: changes here don't last.
use_default_settings: true
general:
  instance_name: "Relay"
server:
  secret_key: "{secret}"
  limiter: false
  public_instance: false
  image_proxy: false
search:
  formats: [html, json]
"""


class SearchError(WebError):
    pass


class Search(WebBox):
    KIND = "search"
    LABEL = "io.relay.search"
    PORT = 48080  # in its network: a tunnel's is its WorkBoxes' too, whose servers like 8080
    WHAT = "Web search (SearXNG)"
    Error = SearchError

    def config(self) -> dict[str, Any]:
        found = self.settings.search if isinstance(self.settings.search, dict) else {}
        return {"enabled": found.get("enabled", True) is not False, "tunnel": self.route()}

    def enabled(self) -> bool:
        return self.config()["enabled"]

    def _settings_file(self) -> Path:
        """SearXNG's settings.yml, with a secret of its own (kept)."""
        folder = config.data_dir() / "searxng"
        folder.mkdir(parents=True, exist_ok=True)
        secret_file = folder / "secret"
        if not secret_file.exists():
            secret_file.touch(mode=0o600)
            secret_file.write_text(secrets.token_hex(24))
        file = folder / "settings.yml"
        text = SETTINGS.format(secret=secret_file.read_text().strip())
        if not file.exists() or file.read_text() != text:
            file.touch(mode=0o600)
            file.chmod(0o600)  # (its secret)
            file.write_text(text)
        return file

    def argv(self) -> list[str]:
        file = self._settings_file()
        return ["-e", "FORCE_OWNERSHIP=false", "-e", "GRANIAN_HOST=127.0.0.1", "-e", f"GRANIAN_PORT={self.PORT}",
                # (its settings' own spec: changed, it's made anew)
                "--label", f"{self.LABEL}.settings={hashlib.sha256(file.read_bytes()).hexdigest()[:12]}",
                "-v", f"{file}:/etc/searxng/settings.yml:ro", "--restart=on-failure", IMAGE]

    def query(self, query: str, category: str = "general", time_range: str = "", language: str = "",
              page: int = 1) -> dict[str, Any]:
        """SearXNG's answer (its JSON) to one search, within search.timeout: the engines get a little less
        (timeout_limit), so it answers in time with those that did."""
        self.ensure()
        wait = int(tunables.get("search.timeout"))
        params = {"q": query, "format": "json", "categories": category, "pageno": page,
                  **({"time_range": time_range} if time_range else {}), **({"language": language} if language else {}),
                  "timeout_limit": max(1, wait - 2)}
        answer = self.ask("/search?" + urllib.parse.urlencode(params), timeout=wait)
        self.used = time.time()
        if answer is None:
            raise SearchError(f"SearXNG didn't answer the search within {wait} s (Settings → Limits → Web search): "
                              f"{self.log(4)}")
        try:
            found = json.loads(answer)
        except ValueError as exc:
            raise SearchError("SearXNG's answer wasn't JSON") from exc
        return found if isinstance(found, dict) else {}

    def text(self, arguments: dict[str, Any]) -> str:
        """A search for an agent (web_search, the MCP tool search, `relay search`): what SearXNG
        found, as text. SearchError when it can't search."""
        query = str(arguments.get("query") or "").strip()
        if not query:
            raise SearchError("What to search for: query")
        category = str(arguments.get("category") or "general").lower()
        time_range = str(arguments.get("time_range") or "").lower()
        try:
            page = max(1, min(int(arguments.get("page") or 1), 10))
        except (TypeError, ValueError):
            page = 1
        found = self.query(query, category if category in CATEGORIES else "general",
                           time_range if time_range in TIME_RANGES else "", str(arguments.get("language") or "")[:10],
                           page)
        return format_results(query, found, page)


def format_results(query: str, found: dict[str, Any], page: int = 1) -> str:
    """SearXNG's JSON as a model reads it: answers first, then the results, numbered."""
    lines = [f"Web search (SearXNG) for {query!r}" + (f", page {page}" if page > 1 else "") + ":"]
    for answer in found.get("answers") or []:
        text = answer.get("answer") if isinstance(answer, dict) else answer
        if text:
            lines.append(f"Answer: {str(text).strip()}")
    for box in (found.get("infoboxes") or [])[:1]:
        if isinstance(box, dict) and (box.get("content") or box.get("infobox")):
            lines.append(f"{box.get('infobox') or ''}: {str(box.get('content') or '').strip()[:600]}".strip(": "))
    results = [r for r in found.get("results") or [] if isinstance(r, dict) and r.get("url")][:RESULTS]
    for number, result in enumerate(results, 1):
        lines.append(f"{number}. {str(result.get('title') or result['url']).strip()}\n   {result['url']}")
        about = " ".join(str(result.get("content") or "").split())[:300]
        engines = ", ".join(result.get("engines") or []) or str(result.get("engine") or "")
        when = str(result.get("publishedDate") or "")[:10]
        details = "; ".join(filter(None, [engines, when]))
        if about or details:
            lines.append(f"   {about}" + (f" ({details})" if details else ""))
    if not results and len(lines) == 1:
        lines.append("Nothing found.")
    if found.get("suggestions"):
        lines.append("Also try: " + "; ".join(str(s) for s in found["suggestions"][:5]))
    silent = [f"{e[0]} ({e[1]})" for e in found.get("unresponsive_engines") or [] if isinstance(e, list) and len(e) > 1]
    if silent:
        lines.append("(Didn't answer: " + ", ".join(silent) + ")")
    if results:
        lines.append("To read one, fetch its address (Relay's fetch reads it with a browser when it must).")
    return "\n".join(lines)
