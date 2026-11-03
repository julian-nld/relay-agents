"""Relay's web fetch (relay.fetch): what it tries until a page comes (http, a browser, Startpage's
Anonymous View, the Wayback Machine), and the text an agent reads."""

import base64
import datetime
import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from relay import config, fetch
from relay.app import create_app
from relay.containers import ContainerError
from relay.db import Database
from relay.fetch import FetchError, Reader, page_text
from relay.providers import Registry
from relay.toolbox import Toolbox

H = {"x-relay-request": "1"}
WORDS = " ".join(f"word{n}" for n in range(200))
ARTICLE = f"""<html><head><title>A page</title><script>track()</script></head><body>
<header><nav><a href="/">Home</a> <a href="/about">About</a></nav></header>
<main><h1>Hello <em>world</em></h1><p>First   paragraph with a <a href="/docs/x">link to docs</a>.</p>
<ul><li>one</li><li>two <b>bold</b></li></ul><pre>code  line 1
  indented</pre><table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table><p>{WORDS}</p></main>
<footer>© footer</footer></body></html>"""
CHECK = "<html><head><title>Just a moment...</title></head><body><script>x()</script>Checking your browser</body></html>"
SHELL = "<html><head><title>App</title></head><body><div id=root></div><script src=/app.js></script></body></html>"
PDF = base64.b64decode(
    "JVBERi0xLjQKMSAwIG9iago8PCAvVHlwZSAvQ2F0YWxvZyAvUGFnZXMgMiAwIFIgPj4KZW5kb2JqCjIgMCBvYmoKPDwgL1R5cGUgL1BhZ2VzIC9L"
    "aWRzIFszIDAgUl0gL0NvdW50IDEgPj4KZW5kb2JqCjMgMCBvYmoKPDwgL1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvTWVkaWFCb3ggWzAgMCAz"
    "MDAgMTQ0XSAvQ29udGVudHMgNCAwIFIgL1Jlc291cmNlcyA8PCAvRm9udCA8PCAvRjEgNSAwIFIgPj4gPj4gPj4KZW5kb2JqCjQgMCBvYmoKPDwg"
    "L0xlbmd0aCA0NiA+PgpzdHJlYW0KQlQgL0YxIDE4IFRmIDIwIDgwIFRkIChIZWxsbyBmcm9tIGEgUERGKSBUaiBFVAplbmRzdHJlYW0KZW5kb2Jq"
    "CjUgMCBvYmoKPDwgL1R5cGUgL0ZvbnQgL1N1YnR5cGUgL1R5cGUxIC9CYXNlRm9udCAvSGVsdmV0aWNhID4+CmVuZG9iagp4cmVmCjAgNgowMDAw"
    "MDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDkgMDAwMDAgbiAKMDAwMDAwMDA1OCAwMDAwMCBuIAowMDAwMDAwMTE1IDAwMDAwIG4gCjAwMDAwMDAy"
    "NDEgMDAwMDAgbiAKMDAwMDAwMDMzNyAwMDAwMCBuIAp0cmFpbGVyCjw8IC9TaXplIDYgL1Jvb3QgMSAwIFIgPj4Kc3RhcnR4cmVmCjQwNwolJUVP"
    "Rgo=")


def html(page, url, status=200, via="http"):
    return {"status": status, "url": url, "type": "text/html", "text": page, "via": via}


class Web:
    """fetch.cjs as Reader asks it: answers per (address, how); what was asked, kept."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, url, how):
        self.asked.append((url, how))
        found = self.answers.get((url, how)) or self.answers.get(url)
        if found is None and how == "startpage":
            raise FetchError("Startpage has no result that's this address")
        if found is None:
            return {"status": 404, "url": url, "type": "text/html", "text": "<html><body>Not found</body></html>",
                    "via": how}
        if isinstance(found, Exception):
            raise found
        return {**found, "via": how}


def day(days):
    return (datetime.date.today() - datetime.timedelta(days=days)).strftime("%Y%m%d")


def test_a_page_reads_as_light_markdown():
    title, text = page_text(ARTICLE, "https://example.com/page")
    assert title == "A page"
    assert text.startswith("# Hello world\n\nFirst paragraph with a [link to docs](https://example.com/docs/x).")
    assert "- one\n- two bold" in text and "```\ncode  line 1\n  indented\n```" in text and "a | b\n1 | 2" in text
    assert "Home" not in text and "footer" not in text and "track()" not in text
    # An article's own header stays, and a sidebar's facts
    _, text = page_text(f"<html><body><header>Site</header><main><article><header><h1>Title</h1><p>By Ann</p></header>"
                        f"<p>{WORDS}</p><aside>Weekly Downloads 12,345</aside></article></main><footer>Site</footer>"
                        "</body></html>")
    assert text.startswith("# Title\n\nBy Ann") and "Weekly Downloads 12,345" in text and "Site" not in text
    # A feed of articles: the whole body, not the first one alone
    feed = "<html><body>" + "".join(f"<article><p>{n} {WORDS}</p></article>" for n in ("first", "second")) + "</body></html>"
    assert "first" in page_text(feed)[1] and "second" in page_text(feed)[1]


def test_http_when_it_answers_a_browser_when_its_refused_or_built_by_javascript():
    web = Web({("https://a.example/", "http"): html(ARTICLE, "https://a.example/"),
               ("https://b.example/", "http"): html("<html><body>Forbidden</body></html>", "https://b.example/", 403),
               ("https://b.example/", "browser"): html(ARTICLE, "https://b.example/"),
               ("https://c.example/", "http"): html(SHELL, "https://c.example/"),
               ("https://c.example/", "browser"): html(ARTICLE, "https://c.example/app")})
    reader = Reader(web.get)
    text = reader.text({"url": "https://a.example/"})
    assert text.splitlines()[0] == "https://a.example/ (fetched): A page" and "# Hello world" in text
    assert web.asked == [("https://a.example/", "http")]
    assert reader.text({"url": "https://b.example/"}).splitlines()[0] == "https://b.example/ (opened in a browser): A page"
    assert reader.text({"url": "https://c.example/"}).splitlines()[0] == (
        "https://c.example/ → https://c.example/app (opened in a browser): A page")
    with pytest.raises(FetchError, match="Only http and https"):
        reader.text({"url": "file:///etc/passwd"})


def test_a_site_that_keeps_refusing_from_the_wayback_machine_said_so():
    url = "https://news.example/"
    web = Web({(url, "http"): html(CHECK, url, 403), (url, "browser"): html(CHECK, url, 200),
               # (its newest copy is of a refusal too: a week back it's the page)
               f"https://web.archive.org/web/2id_/{url}": html(CHECK, f"https://web.archive.org/web/{day(1)}000000id_/{url}", 403),
               f"https://web.archive.org/web/{day(7)}id_/{url}": html(
                   ARTICLE, f"https://web.archive.org/web/{day(8)}101010id_/{url}")})
    text = Reader(web.get).text({"url": url})
    first, second = text.splitlines()[:2]
    assert first.startswith(f"{url} → https://web.archive.org/web/{day(8)}101010id_/{url} (fetched)")
    when = datetime.datetime.strptime(day(8), "%Y%m%d").date().isoformat()
    assert second == f"Archived copy from the Wayback Machine, {when} (8 days old): the site gave Relay a bot check."
    assert "# Hello world" in text


def test_a_wall_without_an_archived_copy_says_what_it_tried():
    url = "https://shop.example/"
    wall = html("<html><head><title>Privacy Gate</title></head><body>Accept our cookies</body></html>",
                "https://consent.example/consent?site=shop")
    web = Web({(url, "http"): wall, (url, "browser"): wall,
               f"https://web.archive.org/web/2id_/{url}": {"status": 429, "url": "https://web.archive.org/", "type": "text/html",
                                                           "text": "Too Many Requests"}})
    with pytest.raises(FetchError) as failed:
        Reader(web.get).text({"url": url})
    assert str(failed.value) == (f"Couldn't fetch {url}: http: a consent or login wall; a browser: a consent or login wall; "
                                 "Startpage's Anonymous View: Startpage has no result that's this address; "
                                 "and the Wayback Machine has no copy.")
    assert len([u for u, _ in web.asked if "archive.org" in u]) == 1  # (its limit: not the older ones too)
    # Next to no text, but not refused: that, said so
    login = html("<html><body><script>app()</script>Log in to see more</body></html>", "https://social.example/")
    web = Web({("https://social.example/", "http"): login, ("https://social.example/", "browser"): login})
    text = Reader(web.get).text({"url": "https://social.example/"})
    assert "Next to no text came (the page may need a login)" in text and "Log in to see more" in text
    # An address on the LAN never goes to the Wayback Machine
    for lan in ("http://192.168.1.5:8420/", "http://localhost:3000/", "http://nas.local/", "http://printer/"):
        web = Web({(lan, "http"): html(CHECK, lan, 403), (lan, "browser"): html(CHECK, lan, 403)})
        with pytest.raises(FetchError, match="no copy"):
            Reader(web.get).text({"url": lan})
        assert not [u for u, how in web.asked if "archive.org" in u or how == "startpage"], lan
    assert fetch.public("https://news.example.com/") and fetch.public("http://93.184.216.34/")
    # Without a browser here: it says so
    with pytest.raises(FetchError, match="Relay's browser isn't available here"):
        Reader(Web({}).get, browser=False).text({"url": "https://gone.example/"})


def test_startpages_anonymous_view_before_the_archive():
    """A site that keeps refusing Relay: Startpage fetches it (its Anonymous View), else the archive."""
    url = "https://forum.example/r/selfhosted/"
    refusal = html(CHECK, url, 403)
    web = Web({(url, "http"): refusal, (url, "browser"): refusal, (url, "startpage"): html(ARTICLE, url)})
    text = Reader(web.get).text({"url": url})
    first, second = text.splitlines()[:2]
    assert first == f"{url} (through Startpage's Anonymous View): A page" and "# Hello world" in text
    assert second == "Through Startpage's Anonymous View (Startpage fetched it): the site gave Relay a bot check."
    assert not [u for u, _ in web.asked if "archive.org" in u]
    # Through Startpage a bot check too (DataDome's CAPTCHA in a frame): the archive, saying what the site gave
    captcha = html('<html><head><title>forum</title></head><body><iframe src="https://geo.captcha-delivery.com/captcha/'
                   '?initialCid=x" title="DataDome CAPTCHA"></iframe><script>dd()</script></body></html>', url)
    assert fetch.judged(captcha, url) == "a bot check"
    web = Web({(url, "http"): refusal, (url, "browser"): refusal, (url, "startpage"): captcha,
               f"https://web.archive.org/web/2id_/{url}": html(ARTICLE, f"https://web.archive.org/web/{day(3)}000000id_/{url}")})
    text = Reader(web.get).text({"url": url})
    assert text.splitlines()[1].startswith("Archived copy from the Wayback Machine") and \
        text.splitlines()[1].endswith("the site gave Relay a bot check.")
    assert (url, "startpage") in web.asked
    # Without Relay's browser (Startpage asks for one): not there
    web = Web({(url, "http"): refusal})
    with pytest.raises(FetchError):
        Reader(web.get, browser=False).text({"url": url})
    assert not [how for _, how in web.asked if how == "startpage"]


@pytest.mark.parametrize("page", ["<title>Humans only</title>", "<p>Press &amp; Hold to confirm you are a human</p>",
                                  f"<h1>Humans only</h1><p>{WORDS}</p>"])  # (a long footer under it)
def test_more_bot_checks_are_known(page):
    assert fetch.judged(html(f"<html><body>{page}<script>x()</script></body></html>", "https://a.example/"),
                        "https://a.example/") == "a bot check"


def test_a_pdf_reads_as_its_text_and_an_image_isnt_text():
    web = Web({"https://p.example/paper.pdf": {"status": 200, "url": "https://p.example/paper.pdf", "type": "application/pdf",
                                               "base64": base64.b64encode(PDF).decode()},
               "https://p.example/logo.png": {"status": 200, "url": "https://p.example/logo.png", "type": "image/png",
                                              "base64": base64.b64encode(b"\x89PNG" + b"x" * 2000).decode()}})
    reader = Reader(web.get)
    assert reader.text({"url": "https://p.example/paper.pdf"}).endswith("--- page 1 ---\nHello from a PDF")
    assert reader.text({"url": "https://p.example/logo.png"}).endswith("It's image/png of 2 KB: not text.")


def test_a_long_page_comes_in_parts_from_what_was_kept(monkeypatch):
    from relay import tunables
    monkeypatch.setattr(tunables, "get", lambda name: 5000 if name == "chat.tool_output" else None)
    long = "<html><body><main>" + "".join(f"<p>paragraph {n} {WORDS}</p>" for n in range(10)) + "</main></body></html>"
    web = Web({"https://long.example/": html(long, "https://long.example/")})
    reader = Reader(web.get)
    first = reader.text({"url": "https://long.example/"})
    assert first.endswith("more characters: fetch it again with offset=4000.)")
    second = reader.text({"url": "https://long.example/", "offset": 4000})
    assert second.splitlines()[0].endswith("(from character 4000)") and len(web.asked) == 1  # (kept, not fetched again)
    assert "Nothing more" in reader.text({"url": "https://long.example/", "offset": 10**6})


def test_the_fetch_box_and_without_it():
    containers = SimpleNamespace(image="localhost/relay-agent:test", check=lambda: None)
    fetcher = fetch.Fetcher(config.Settings(), None, containers)
    argv = fetcher.argv()
    assert argv[-3:] == ["localhost/relay-agent:test", "node", str(fetch.CONTAINERFILE.parent / "fetch.cjs")]
    assert f"RELAY_FETCH_PORT={fetch.Fetcher.PORT}" in argv and "--init" in argv
    assert f"{fetch.CONTAINERFILE.parent}:{fetch.CONTAINERFILE.parent}:ro" in argv
    # What the box answers: a page, or why not
    fetcher.ensure = lambda: None
    fetcher.ask = lambda path, timeout=10: json.dumps({"error": "net::ERR_NAME_NOT_RESOLVED"})
    with pytest.raises(FetchError, match="ERR_NAME_NOT_RESOLVED"):
        fetcher._get("https://nowhere.example/", "browser")
    fetcher.ask = lambda path, timeout=10: None
    with pytest.raises(FetchError, match="didn't answer"):
        fetcher._get("https://nowhere.example/", "http")
    # No image (no podman): here, http only, and it says so
    fetcher.containers = SimpleNamespace(image="x", check=lambda: (_ for _ in ()).throw(ContainerError("podman isn't installed")))
    fetcher.here = Reader(Web({"https://a.example/": html(ARTICLE, "https://a.example/")}).get, browser=False)
    text = fetcher.text({"url": "https://a.example/"})
    assert "# Hello world" in text and text.endswith("(Relay's browser isn't available: podman isn't installed)")


def test_chat_models_web_fetch_is_relays(tmp_path, monkeypatch):
    box = Toolbox(tmp_path, code=False, fetch=lambda arguments: f"page of {arguments['url']}")
    assert box.call("web_fetch", {"url": "https://a.example/"}) == (True, "page of https://a.example/")
    names = {d["function"]["name"]: d for d in box.definitions()}
    assert "offset" in names["web_fetch"]["function"]["parameters"]["properties"]
    failing = Toolbox(tmp_path, code=False, fetch=lambda arguments: (_ for _ in ()).throw(FetchError("a bot check")))
    assert failing.call("web_fetch", {"url": "https://a.example/"}) == (False, "a bot check")
    # Without Relay (tests, say): fetched here
    monkeypatch.setattr(fetch, "_HERE", Reader(Web({"https://a.example/": html(ARTICLE, "https://a.example/")}).get,
                                               browser=False))
    ok, text = Toolbox(tmp_path, code=False).call("web_fetch", {"url": "https://a.example/"})
    assert ok and "# Hello world" in text


def test_trying_a_fetch_in_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_HOME", str(tmp_path / "data"))
    settings = config.Settings(workspace_roots=[str(tmp_path)], chat_root=str(tmp_path / "chats"))
    registry = Registry(tmp_path / "providers")
    registry.ensure_defaults()
    app = create_app(settings, Database(tmp_path / "relay.db"), registry)
    relay = app.state.relay
    monkeypatch.setattr(relay.fetch, "text", lambda arguments: f"{arguments['url']} (opened in a browser): A page\n\n"
                        + "x" * 4000)
    monkeypatch.setattr(relay.fetch, "running", lambda: True)
    monkeypatch.setattr(relay.search, "running", lambda: False)
    with TestClient(app) as client:
        assert client.post("/api/setup", json={"username": "me", "password": "correct horse"}, headers=H).status_code == 200
        tried = client.post("/api/search/try", json={"query": "https://a.example/"}, headers=H).json()
        assert tried["fetched"] == "https://a.example/ (opened in a browser): A page" and tried["more"]
        assert len(tried["text"]) == 3000
        assert client.get("/api/search").json()["status"]["fetching"] is True
        monkeypatch.setattr(relay.fetch, "text", lambda arguments: (_ for _ in ()).throw(FetchError("a bot check")))
        assert client.post("/api/search/try", json={"query": "https://a.example/"}, headers=H).status_code == 409


def test_next_to_no_text_doesnt_go_to_the_archive():
    url = "https://social.example/feed"
    login = html("<html><body><script>app()</script>Log in to see more</body></html>", url)
    web = Web({(url, "http"): login, (url, "browser"): login})
    text = Reader(web.get).text({"url": url})
    assert "Next to no text came" in text
    assert not [u for u, how in web.asked if "archive.org" in u or how == "startpage"]  # (they'd have the same)


def test_the_archives_older_copies_are_asked_at_once_the_newest_good_one_taken(monkeypatch):
    import threading
    import time as clock
    url = "https://paper.example/"
    asked_at = {}

    class Slow(Web):
        def get(self, address, how):
            asked_at[address] = clock.monotonic()
            if "/web/" in address and "/2id_/" not in address:
                clock.sleep(0.2)  # (the archive is slow: all at once, these take 0.2 s, not 0.8)
            return super().get(address, how)

    refusal = html(CHECK, url, 403)
    web = Slow({(url, "http"): refusal, (url, "browser"): refusal,
                f"https://web.archive.org/web/2id_/{url}": html(CHECK, f"https://web.archive.org/web/{day(1)}id_/{url}", 403),
                f"https://web.archive.org/web/{day(7)}id_/{url}": html(CHECK, f"https://web.archive.org/web/{day(8)}id_/{url}", 403),
                f"https://web.archive.org/web/{day(30)}id_/{url}": html(ARTICLE, f"https://web.archive.org/web/{day(31)}id_/{url}"),
                f"https://web.archive.org/web/{day(180)}id_/{url}": html(ARTICLE, f"https://web.archive.org/web/{day(181)}id_/{url}")})
    started = clock.monotonic()
    text = Reader(web.get).text({"url": url})
    assert clock.monotonic() - started < 0.6
    assert f"/web/{day(31)}id_/" in text.splitlines()[0] and "(31 days old)" in text.splitlines()[1]
    dated = [at for address, at in asked_at.items() if "/web/" in address and "/2id_/" not in address]
    assert len(dated) == 4 and max(dated) - min(dated) < 0.15  # (asked together)
    # Slower than its time: nothing from the archive, and soon
    monkeypatch.setattr(fetch, "ARCHIVE_TIME", 0.1)
    web.answers = {k: v for k, v in web.answers.items() if "/2id_/" in str(k) or not str(k).startswith("https://web")}
    for days in (7, 30, 180, 365):
        web.answers[f"https://web.archive.org/web/{day(days)}id_/{url}"] = html(ARTICLE, f"https://web.archive.org/web/{day(days)}id_/{url}")
    started = clock.monotonic()
    with pytest.raises(FetchError, match="no copy"):
        Reader(web.get).text({"url": url})
    assert clock.monotonic() - started < 0.2 + 0.15
    assert threading.active_count() < 50
