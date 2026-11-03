// Relay's web fetch (relay/fetch.py): a small HTTP server in a box of its own (relay-fetch, of the
// WorkBox image), on 127.0.0.1 in its network only; Relay asks it from inside (podman exec wget).
//
//   GET /fetch?url=…&how=http        the address with Node's fetch, as a browser asks
//   GET /fetch?url=…&how=browser     the page as Chromium shows it (its JavaScript run)
//   GET /fetch?url=…&how=startpage   the page through Startpage's Anonymous View (viaStartpage)
//   GET /healthz
//
// It answers JSON: {status, url (where it ended up), type, title?, text | base64, via, truncated?}
// or {error}. Text types come as text, others (a PDF, an image) as base64; Relay makes them
// readable (relay/fetch.py). One Chromium, started on the first page and closed after a while
// unused; each page in a context of its own.

"use strict";
const http = require("http");

const { chromium } = require("/usr/local/lib/node_modules/playwright-core");

const PORT = Number(process.env.RELAY_FETCH_PORT || 48081);
const AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36";
const HEADERS = {
  "user-agent": AGENT, accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
  "accept-language": "en-US,en;q=0.9",
};
const MAX = 15 * 1024 * 1024; // bytes read of one address
const HTTP_MS = 20 * 1000;
const PAGE_MS = 30 * 1000;
const IDLE_MS = 5 * 60 * 1000; // Chromium closes after this long without a page
const TEXT = /^(text\/|application\/(json|xml|xhtml\+xml|javascript|ld\+json|rss\+xml|atom\+xml)|$)/;
const CHECKING = /just a moment|attention required|checking your browser|please wait|security verification/i;

function log(...parts) {
  console.error(new Date().toISOString(), ...parts);
}

async function viaHttp(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), HTTP_MS);
  try {
    const response = await fetch(url, { headers: HEADERS, redirect: "follow", signal: controller.signal });
    const header = response.headers.get("content-type") || "";
    const type = header.split(";")[0].trim().toLowerCase();
    const chunks = [];
    let size = 0;
    let truncated = false;
    const reader = response.body ? response.body.getReader() : null;
    while (reader) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(Buffer.from(value));
      size += value.length;
      if (size >= MAX) {
        truncated = true;
        controller.abort();
        break;
      }
    }
    const body = Buffer.concat(chunks).subarray(0, MAX);
    const answer = { status: response.status, url: response.url || url, type, via: "http", truncated };
    if (TEXT.test(type)) {
      const charset = (/charset=([^;]+)/i.exec(header) || [])[1] || "utf-8";
      let text;
      try { text = new TextDecoder(charset.trim().toLowerCase()).decode(body); } catch { text = body.toString("utf8"); }
      return { ...answer, text };
    }
    return { ...answer, base64: body.toString("base64") };
  } finally {
    clearTimeout(timer);
  }
}

let browser = null;
let idle = null;
let startpageContext = null;

async function launched() {
  if (!browser) {
    browser = await chromium.launch({ headless: true, executablePath: "/usr/bin/chromium",
      args: ["--no-sandbox", "--disable-dev-shm-usage"] });
    browser.on("disconnected", () => { browser = null; startpageContext = null; });
  }
  clearTimeout(idle);
  idle = setTimeout(() => { const b = browser; browser = null; if (b) b.close().catch(() => {}); }, IDLE_MS);
  return browser;
}

async function viaBrowser(url) {
  const context = await (await launched()).newContext({ userAgent: AGENT, locale: "en-US",
    viewport: { width: 1280, height: 900 } });
  try {
    const page = await context.newPage();
    let status = 0;
    page.on("response", (response) => {  // (the last document the page itself became: after a bot check too)
      if (response.request().isNavigationRequest() && response.frame() === page.mainFrame()) status = response.status();
    });
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: PAGE_MS });
    await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});  // (some never go quiet)
    for (let waited = 0; waited < 15 && CHECKING.test(await page.title().catch(() => "")); waited++) {
      await page.waitForTimeout(1000);  // (a bot check that passes by itself goes on to the page)
    }
    await page.waitForLoadState("domcontentloaded", { timeout: 5000 }).catch(() => {});
    return { status, url: page.url(), type: "text/html", title: await page.title().catch(() => ""),
      text: await page.content(), via: "browser" };
  } finally {
    await context.close().catch(() => {});
  }
}

// Startpage's Anonymous View: Startpage fetches the page from its own servers and shows it through its
// proxy (av-….startpage.com/__av/…), past many a wall that refuses Relay. Such a link comes only with a
// search's result, so Startpage is searched for the address and the result that is it opened that way.
// One context is kept for it while Chromium is up: Startpage's own bot check (a proof of work) once.
const STARTPAGE = "https://www.startpage.com/sp/search?query=";
const AV_PAGE = /\/__av\//;
const AV_LINK = /(?:https?:\/\/av-[a-z0-9-]+\.startpage\.com)?\/__av\/([A-Za-z0-9_-]+)/g;

function original(token) {  // (an Anonymous View address's token: Startpage's own, then the address in base64)
  const start = token.indexOf("aHR0c", 8);  // ("http")
  if (start < 0) return null;
  const real = Buffer.from(token.slice(start), "base64url").toString("utf8");
  return /^https?:\/\/[^\s"'<>]+$/.test(real) ? real : null;
}

function escaped(text) {
  return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function sameAddress(found, asked) {  // (a result for an address without a query may carry one of its own)
  try {
    const a = new URL(found), b = new URL(asked);
    const where = (u) => u.hostname.replace(/^www\./, "") + u.pathname.replace(/\/+$/, "");
    return where(a) === where(b) && (!b.search || a.search === b.search);
  } catch {
    return false;
  }
}

async function viaStartpage(url) {
  const deadline = Date.now() + PAGE_MS + 10 * 1000;
  const left = () => Math.max(1000, deadline - Date.now());
  if (!startpageContext) {
    startpageContext = await (await launched()).newContext({ userAgent: AGENT, locale: "en-US",
      viewport: { width: 1280, height: 900 } });
  } else {
    await launched();  // (Chromium stays up a while longer)
  }
  const page = await startpageContext.newPage();
  try {
    await page.goto(STARTPAGE + encodeURIComponent(url), { waitUntil: "domcontentloaded", timeout: left() });
    await page.locator('.result a[href*="/av/proxy"]').first().waitFor({ timeout: Math.min(20000, left()) })
      .catch(() => {});
    const results = await page.locator(".result").evaluateAll((boxes) => boxes.map((box) => {
      const link = box.querySelector('a[href^="http"]:not([href*="startpage.com"])');
      const av = box.querySelector('a[href*="/av/proxy"]');
      const title = box.querySelector(".result-title");
      return { url: link && link.href, av: av && av.href, title: title ? title.innerText.trim() : "" };
    }));
    if (!results.length) {
      return { error: `Startpage gave no results (${(await page.title().catch(() => "")) || "a check, perhaps"})` };
    }
    const found = results.find((r) => r.url && r.av && sameAddress(r.url, url));
    if (!found) return { error: "Startpage has no result that's this address" };
    let status = 0;
    page.on("response", (response) => {
      if (response.request().isNavigationRequest() && response.frame() === page.mainFrame()) status = response.status();
    });
    await page.goto(found.av, { waitUntil: "domcontentloaded", timeout: left() });
    await page.waitForURL(AV_PAGE, { timeout: left() });  // (after Startpage's page that it's masking you)
    await page.waitForLoadState("domcontentloaded", { timeout: left() }).catch(() => {});
    await page.waitForLoadState("networkidle", { timeout: Math.min(5000, left()) }).catch(() => {});
    const opening = () =>
      page.evaluate(() => (document.body ? document.body.innerText.slice(0, 600) : "")).catch(() => "");
    for (let waited = 0; waited < 8 && Date.now() < deadline && CHECKING.test(await opening()); waited++) {
      await page.waitForTimeout(1000);  // (the site's bot check that passes by itself, through Startpage too)
    }
    await page.waitForLoadState("domcontentloaded", { timeout: Math.min(5000, left()) }).catch(() => {});
    // The page as the site made it: without what Startpage adds next to its body (a banner, a feedback
    // form, a dialog), its links to the site again (Startpage keeps rewriting them, so in the text), its title
    await page.evaluate(() => {
      for (const el of [...document.documentElement.children]) {
        if (el !== document.head && el !== document.body) el.remove();
      }
      document.querySelectorAll("#av-banner-style, #av-banner-tpl").forEach((el) => el.remove());
    });
    let text = (await page.content()).replace(AV_LINK, (link, token) => original(token) || link);
    if (found.title) text = text.replace(/<title[^>]*>[\s\S]*?<\/title>/i, `<title>${escaped(found.title)}</title>`);
    const at = (AV_PAGE.test(page.url()) && original(page.url().split("/__av/")[1].split(/[?#]/)[0])) || found.url;
    return { status, url: at, type: "text/html", title: found.title, text, via: "startpage" };
  } finally {
    await page.close().catch(() => {});
  }
}

http.createServer(async (request, response) => {
  const asked = new URL(request.url, "http://relay");
  const reply = (code, body) => {
    response.writeHead(code, { "content-type": "application/json" });
    response.end(JSON.stringify(body));
  };
  if (asked.pathname === "/healthz") return reply(200, { ok: true });
  if (asked.pathname !== "/fetch") return reply(404, { error: "not here" });
  const url = asked.searchParams.get("url") || "";
  if (!/^https?:\/\//i.test(url)) return reply(400, { error: "Only http and https addresses" });
  try {
    const how = asked.searchParams.get("how");
    reply(200, how === "browser" ? await viaBrowser(url) : how === "startpage" ? await viaStartpage(url)
      : await viaHttp(url));
  } catch (error) {
    log("fetch", url, error && error.message);
    reply(200, { error: String(error && error.message || error).split("\n")[0].slice(0, 300) });
  }
}).listen(PORT, "127.0.0.1", () => log(`relay fetch on 127.0.0.1:${PORT}`));
