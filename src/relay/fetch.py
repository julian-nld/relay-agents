"""Relay's own web fetch: a page as text an agent can read, when a plain fetch is refused or
comes back empty.

Every agent gets it (next to its own fetch, which stays): chat models as web_fetch
(relay.toolbox), agents with a shell as the MCP tool fetch or `relay fetch` (relay.delegation,
relay.tools_script), subagents too. It's one of Relay's boxes for the web (relay.webbox), on the
same route as web search: relay-fetch, of Relay's WorkBox image, runs container/fetch.cjs, which
fetches an address as a browser asks (http) or opens it in Chromium (browser).

What it tries, in order, until one gives a page:
1. http: fast; enough for most.
2. browser, when http was refused (401/403/429/503, a bot check, a consent or login wall) or came
   back with next to no text (a page its JavaScript builds).
3. When the site keeps refusing Relay's browser too: Startpage's Anonymous View (Startpage fetches
   the page from its own servers; it's searched for the address, and the result that's it opened
   that way), said to be one.
4. Then the newest copy at the Wayback Machine that's the page (not of a refusal: further back for
   those, asked at once), said to be one, with its date. Neither 3 nor 4 for a page that only gave
   next to no text (they have the same), nor for an address on the LAN.

HTML becomes light markdown (its main content: page_text), a PDF its text (pdf_text, pypdf), at
most a tool result's worth a time (Settings → Limits): the rest with offset (kept a while, not fetched again). Without
podman (or tests), it fetches here, with http only (Reader with fetch_here).
"""

from __future__ import annotations

import base64
import binascii
import collections
import concurrent.futures
import datetime
import html.parser
import io
import ipaddress
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from relay import config, tunables
from relay.containers import CONTAINERFILE, ContainerError
from relay.webbox import WebBox, WebError

if TYPE_CHECKING:
    from relay.containers import Containers
    from relay.vpn import Vpn

KEEP_FOR = 10 * 60  # seconds a fetched page is kept for its next part
KEEP = 16  # pages kept
THIN = 150  # words: fewer, and an HTML page with scripts is taken for one its JavaScript builds
REFUSED = (401, 403, 429, 451, 503)
CHECK = re.compile(r"just a moment|attention required|checking your browser|verify you are human|are you a robot|"
                   r"captcha|access denied|enable javascript and cookies|press & hold|confirm you are a human|"
                   r"humans only", re.I)
# (DataDome's, say: a page that's one)
CAPTCHA = re.compile(r"""<iframe\b[^>]*\b(?:src|title)=["'][^"']*captcha""", re.I)
WALL = re.compile(r"consent|privacy|cookie|login|log-in|signin|sign-in|log in|sign in", re.I)
AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
ARCHIVE = "https://web.archive.org/web"
BACK = (0, 7, 30, 180, 365)  # days: the Wayback Machine's copies tried (0: its newest), until one is the page
ARCHIVE_TIME = 20  # seconds for the archive's copies, all together


class FetchError(WebError):
    pass


# ------------------------------------------------------------------ text an agent reads

SKIP = {"script", "style", "noscript", "svg", "template", "nav", "form", "button", "iframe", "select", "dialog",
        "canvas", "head"}
# The site's own, around its content: left out when reading the whole body (an article's are its own; a
# sidebar's facts stay, an npm package's downloads say)
AROUND = {"header", "footer"}
BLOCKS = {"p", "div", "section", "article", "main", "blockquote", "figure", "figcaption", "dl", "dt", "dd", "ul", "ol",
          "table", "hr", "address", "details", "summary"}


class _Page(html.parser.HTMLParser):
    """A page as light markdown: headings, paragraphs, list items, code, table rows, links."""

    def __init__(self, base: str, within: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.within = within  # only inside the first element of this tag ("main", "article"), if given
        self.inside = 0 if within else 1  # (how deep in it)
        self.done = False
        self.skipping = 0
        self.pre = 0
        self.out: list[str] = []
        self.line: list[str] = []
        self.links: list[tuple[str, int]] = []  # (href, where its text started in self.line)
        self.cells: list[str] | None = None
        self.title = ""
        self.in_title = False

    def _flush(self, prefix: str = "") -> None:
        text = " ".join("".join(self.line).split())
        if text:
            self.out.append(prefix + text)
        self.line = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if self.done:
            return
        if self.within:
            self.inside += tag == self.within
            if not self.inside:
                return
        if tag in SKIP or (tag in AROUND and not self.within):
            self.skipping += 1
            return
        if self.skipping:
            return
        if tag == "pre":
            self._flush()
            self.pre += 1
            self.line.append("```\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self._flush()
        elif tag == "li":
            self._flush()
            self.line.append("\x00- ")
        elif tag == "br":
            self.line.append("\n" if self.pre else " ")
        elif tag == "tr":
            self._flush()
            self.cells = []
        elif tag in ("td", "th") and self.cells is not None:
            self.cells.append("")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.links.append((href, len("".join(self.line))))
        elif tag in BLOCKS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if self.done or not self.inside:
            return
        if tag in SKIP or (tag in AROUND and not self.within):
            self.skipping = max(0, self.skipping - 1)
            return
        if self.skipping:
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("".join(self.line).rstrip() + "\n```")
            self.line = []
        elif re.fullmatch(r"h[1-6]", tag):
            self._flush("#" * int(tag[1]) + " ")
        elif tag == "li":
            text = " ".join("".join(self.line).replace("\x00- ", "").split())
            if text:
                self.out.append("- " + text)
            self.line = []
        elif tag in ("td", "th") and self.cells is not None:
            if self.cells:
                self.cells[-1] = " ".join((self.cells[-1] + "".join(self.line)).split())
            self.line = []
        elif tag == "tr" and self.cells is not None:
            if any(self.cells):
                self.out.append(" | ".join(self.cells))
            self.cells = None
            self.line = []
        elif tag == "a" and self.links:
            href, start = self.links.pop()
            whole = "".join(self.line)
            text = " ".join(whole[start:].split())
            target = urllib.parse.urljoin(self.base, href) if href else ""
            if text and target.startswith(("http://", "https://")) and target != self.base:
                self.line = [whole[:start], f"[{text}]({target})"]
        elif tag in BLOCKS:
            self._flush()
        if self.within and tag == self.within:
            self.inside -= 1
            if not self.inside:  # (the end of the one it reads: the rest of the page isn't its content)
                self._flush()
                self.done = True

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
            return
        if self.done or self.skipping or not self.inside:
            return
        self.line.append(data if self.pre else data.replace("\n", " "))

    def text(self) -> str:
        self._flush()
        joined, last = [], ""
        for block in (b.replace("\x00", "") for b in self.out if b.strip()):
            # (a list's items, a table's rows: one under the other)
            together = (block.startswith("- ") and last.startswith("- ")) or (" | " in block and " | " in last)
            joined.append(("\n" if together else "\n\n") + block if joined else block)
            last = block
        return "".join(joined)


def page_text(page: str, url: str = "") -> tuple[str, str]:
    """(its title, its main content as light markdown): its <main>, else its one <article> (when there's
    text in it), else the whole body without the site's header and footer; never navigation or
    forms."""
    within = None
    if re.search(r"<main\b", page, re.I):  # (its sidebar too: an npm package's downloads are there)
        within = "main"
    elif len(re.findall(r"<article\b", page, re.I)) == 1:
        within = "article"
    title, text = "", ""
    for choice in ([within, None] if within else [None]):
        parser = _Page(url, choice)
        try:
            parser.feed(page)
            parser.close()
        except (AssertionError, ValueError):  # (a page html.parser can't take: what it read)
            pass
        title, text = " ".join(parser.title.split()), parser.text()
        if len(text.split()) >= THIN or choice is None:
            break
    return title, text


def pdf_text(data: bytes) -> str:
    """A PDF's text, page by page."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = []
        for number, page in enumerate(reader.pages[:300], 1):
            pages.append(f"--- page {number} ---\n{(page.extract_text() or '').strip()}")
    except (PdfReadError, ValueError, KeyError, TypeError, NotImplementedError) as exc:
        raise FetchError(f"A PDF Relay can't read: {exc}") from exc
    return "\n\n".join(pages)


def _size(count: int) -> str:
    return f"{count / 1_000_000:.1f} MB" if count >= 1_000_000 else f"{max(1, count // 1000)} KB"


def readable(answer: dict[str, Any]) -> tuple[str, str]:
    """(title, text) of what was fetched: HTML as light markdown, a PDF's text, text as it is."""
    kind = str(answer.get("type") or "")
    if "text" in answer:
        if kind in ("text/html", "application/xhtml+xml") or (not kind and "<html" in answer["text"][:2000].lower()):
            return page_text(answer["text"], str(answer.get("url") or ""))
        return "", answer["text"]
    try:
        data = base64.b64decode(answer.get("base64") or "")
    except (binascii.Error, ValueError):
        data = b""
    if kind == "application/pdf" or data[:5] == b"%PDF-":
        return "", pdf_text(data)
    return "", f"It's {kind or 'a file'} of {_size(len(data))}: not text."


def judged(answer: dict[str, Any], asked: str) -> str | None:
    """Why this isn't the page (refused, a bot check, a wall, next to no text), or None."""
    if answer.get("error"):
        return str(answer["error"])
    status = int(answer.get("status") or 0)
    kind = str(answer.get("type") or "")
    if "text" not in answer or kind not in ("text/html", "application/xhtml+xml", ""):
        return f"HTTP {status}" if status >= 400 else None  # (a PDF, JSON, an image: as it is)
    page = answer["text"]
    title, text = page_text(page, str(answer.get("url") or asked))
    if CHECK.search(title) or CHECK.search(text[:120]) or (  # (its first words: one with the site's footer too)
            len(text.split()) < THIN and (CHECK.search(text[:3000]) or CAPTCHA.search(page))):
        return "a bot check"
    if status in REFUSED or status >= 500:
        return f"HTTP {status}"
    if status >= 400:
        return f"HTTP {status} (not there)"
    there, wanted = urllib.parse.urlparse(str(answer.get("url") or asked)), urllib.parse.urlparse(asked)
    elsewhere = there.netloc.removeprefix("www.") != wanted.netloc.removeprefix("www.")
    if (elsewhere or there.path != wanted.path) and (WALL.search(there.netloc + there.path) or WALL.search(title)):
        return "a consent or login wall"
    if len(text.split()) < THIN and re.search(r"<script\b", page, re.I):
        return "next to no text (a page its JavaScript builds)"
    return None


def public(url: str) -> bool:
    """An address on the internet (not this machine, the LAN, a name only the LAN knows)."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host and not host.endswith((".local", ".lan", ".home", ".internal", ".localdomain", ".arpa"))


class Reader:
    """Fetching an address until a page comes, and its text, a part at a time. `get(url, how)`
    asks fetch.cjs's way (how: "http" or "browser"; FetchError when it can't)."""

    def __init__(self, get: Callable[[str, str], dict[str, Any]], browser: bool = True) -> None:
        self.get = get
        self.browser = browser
        self.kept: collections.OrderedDict[str, tuple[float, str, str]] = collections.OrderedDict()
        self.lock = threading.Lock()

    def text(self, arguments: dict[str, Any]) -> str:
        url = str(arguments.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise FetchError("Only http and https addresses.")
        try:
            offset = max(0, int(arguments.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        with self.lock:
            kept = self.kept.get(url)
        if kept is None or time.time() - kept[0] > KEEP_FOR or not offset:
            head, text = self.read(url)
            with self.lock:
                self.kept[url] = (time.time(), head, text)
                self.kept.move_to_end(url)
                while len(self.kept) > KEEP:
                    self.kept.popitem(last=False)
        else:
            _, head, text = kept
        # (as much as a tool's result may be, its first line and how to go on included: Settings → Limits)
        part = text[offset:offset + max(2000, tunables.get("chat.tool_output") - 1000)]
        more = len(text) - offset - len(part)
        if offset and not part:
            return f"{head}\n\n(Nothing more: it has {len(text)} characters.)"
        tail = f"\n\n(… {more} more characters: fetch it again with offset={offset + len(part)}.)" if more > 0 else ""
        return f"{head}{f' (from character {offset})' if offset else ''}\n\n{part}{tail}"

    def read(self, url: str) -> tuple[str, str]:
        """(a line saying what it is, its whole text)."""
        tried = []
        best: tuple[dict[str, Any], str] | None = None  # (an answer with some text, why it isn't more)
        refused = True  # (how the last try ended: refused, or only next to no text)
        for how in ("http", "browser") if self.browser else ("http",):
            try:
                answer = self.get(url, how)
            except FetchError as exc:
                tried.append(f"{how}: {exc}")
                refused = True
                continue
            why = judged(answer, url)
            if why is None:
                return self._page(url, answer)
            tried.append(f"{'a browser' if how == 'browser' else how}: {why}")
            refused = not why.startswith("next to no text")
            if not refused and "text" in answer:
                best = (answer, why)
        # (a page that gave next to no text, not a refusal: Startpage and the archive have the same, and take a while)
        if refused and self.browser and public(url):
            try:
                answer = self.get(url, "startpage")
            except FetchError as exc:
                answer, why = None, str(exc)
            else:
                why = judged(answer, url)
                if why is None:
                    return self._page(url, answer, "Through Startpage's Anonymous View (Startpage fetched it): the "
                                                   f"site gave Relay {tried[-1].split(': ', 1)[-1]}.")
            tried.append(f"Startpage's Anonymous View: {why}")
        archived = self._archived(url) if refused else None
        if archived is not None:
            answer, when = archived
            gave = [t for t in tried if not t.startswith("Startpage")]
            return self._page(url, answer, f"Archived copy from the Wayback Machine, {when}: the site gave Relay "
                                           f"{gave[-1].split(': ', 1)[-1] if gave else 'nothing'}.")
        if best is not None:
            return self._page(url, best[0], f"Next to no text came (the page may need a login): {best[1]}.")
        raise FetchError(f"Couldn't fetch {url}: " + "; ".join(tried) + "; and the Wayback Machine has no copy."
                         + ("" if self.browser else " (Relay's browser isn't available here.)"))

    def _page(self, url: str, answer: dict[str, Any], note: str = "") -> tuple[str, str]:
        title, text = readable(answer)
        final = str(answer.get("url") or url)
        via = {"http": "fetched", "browser": "opened in a browser",
               "startpage": "through Startpage's Anonymous View"}.get(str(answer.get("via")), "fetched")
        head = f"{url}" + (f" → {final}" if final != url else "") + f" ({via})" + (f": {title}" if title else "")
        if answer.get("truncated"):
            note = (note + " " if note else "") + "(Only its first 15 MB.)"
        return head + (f"\n{note}" if note else ""), text or "(No text.)"

    def _archived(self, url: str) -> tuple[dict[str, Any], str] | None:
        """The Wayback Machine's newest copy that's the page, and its date; None without one. When its
        newest copy is of a refusal too, those further back are asked at once, within ARCHIVE_TIME. Only
        a public address goes there: not one on the LAN."""
        if not public(url):
            return None
        deadline = time.monotonic() + ARCHIVE_TIME

        def copy_of(days: int) -> dict[str, Any] | None:
            stamp = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y%m%d") if days else "2"
            try:
                copy = self.get(f"{ARCHIVE}/{stamp}id_/{url}", "http")  # (the copy closest to that day)
            except FetchError:
                return None
            if int(copy.get("status") or 0) == 429:
                raise FetchError("the archive's limit for now")
            return copy if judged(copy, str(copy.get("url") or url)) is None else None  # (its own address: no wall)

        try:
            found = copy_of(0)
        except FetchError:
            return None
        if found is None:
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(BACK) - 1)
            asked = {days: pool.submit(copy_of, days) for days in BACK[1:]}
            concurrent.futures.wait(asked.values(), timeout=max(0.0, deadline - time.monotonic()))
            pool.shutdown(wait=False, cancel_futures=True)
            for days in BACK[1:]:  # (the newest of those that came in time)
                future = asked[days]
                if future.done() and not future.exception() and future.result() is not None:
                    found = future.result()
                    break
        if found is None:
            return None
        made = re.search(r"/web/(\d{8})", str(found.get("url") or ""))
        if not made:
            return found, "undated"
        day = datetime.datetime.strptime(made.group(1), "%Y%m%d").date()
        age = (datetime.date.today() - day).days
        return found, f"{day.isoformat()}" + (f" ({age} days old)" if age > 1 else "")


# ------------------------------------------------------------------ where it fetches

class Fetcher(WebBox):
    """relay-fetch: container/fetch.cjs in a box of Relay's WorkBox image (its Chromium)."""
    KIND = "fetch"
    LABEL = "io.relay.fetch"
    PORT = 48081  # in its network: a tunnel's is its WorkBoxes' too
    WHAT = "Web fetch"
    Error = FetchError

    def __init__(self, settings: config.Settings, vpn: Vpn, containers: Containers | None) -> None:
        super().__init__(settings, vpn)
        self.containers = containers
        self.reader = Reader(self._get)
        self.here = Reader(fetch_here, browser=False)

    def argv(self) -> list[str]:
        folder = CONTAINERFILE.parent
        return ["--init", "-e", f"RELAY_FETCH_PORT={self.PORT}", "-v", f"{folder}:{folder}:ro", "--restart=on-failure",
                self.containers.image, "node", str(folder / "fetch.cjs")]

    def text(self, arguments: dict[str, Any]) -> str:
        """A fetch for an agent (web_fetch, the MCP tool fetch, `relay fetch`): the page as text, a
        part at a time. FetchError when nothing comes."""
        if self.containers is None:
            return self.here.text(arguments)
        try:
            self.containers.check()  # (Relay's WorkBox image, built the first time)
        except ContainerError as exc:
            return self.here.text(arguments) + f"\n\n(Relay's browser isn't available: {exc})"
        return self.reader.text(arguments)

    def _get(self, url: str, how: str) -> dict[str, Any]:
        self.ensure()
        answer = self.ask("/fetch?" + urllib.parse.urlencode({"url": url, "how": how}), timeout=75)
        self.used = time.time()
        if answer is None:
            raise FetchError(f"Relay's fetch box didn't answer: {self.log(3)}")
        try:
            found = json.loads(answer)
        except ValueError as exc:
            raise FetchError("Relay's fetch box answered nonsense") from exc
        if found.get("error"):
            raise FetchError(str(found["error"]))
        return found


def fetch_here(url: str, how: str = "http") -> dict[str, Any]:
    """fetch.cjs's http, in Relay itself: without its box (no podman; tests)."""
    if how != "http":
        raise FetchError("Relay's browser isn't available here")
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept-Language": "en-US,en;q=0.9",
                                                   "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status, final, header = response.status, response.geturl(), response.headers
            data = response.read(15_000_000)
    except urllib.error.HTTPError as exc:
        status, final, header = exc.code, url, exc.headers
        data = exc.read(2_000_000) if exc.fp else b""
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"Couldn't reach it: {getattr(exc, 'reason', exc)}") from exc
    kind = header.get_content_type() if header else ""
    found: dict[str, Any] = {"status": status, "url": final, "type": kind, "via": "http"}
    if re.match(r"text/|application/(json|xml|xhtml\+xml|javascript|ld\+json|rss\+xml|atom\+xml)", kind) or not kind:
        found["text"] = data.decode((header.get_content_charset() if header else None) or "utf-8", errors="replace")
    else:
        found["base64"] = base64.b64encode(data).decode()
    return found


_HERE = Reader(fetch_here, browser=False)


def text_here(arguments: dict[str, Any]) -> str:
    """Without Relay (a Toolbox in tests, say): fetched in this process, http only."""
    return _HERE.text(arguments)

