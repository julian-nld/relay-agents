// Relay's HTTP API, as the extension speaks it: an app password, Relay's own words for what it refused, and a run's
// events as they come (its server-sent stream). Nothing of VSCodium's here, so node --test tries it as it is.
"use strict";

const AGENT = "relay-vscodium";

class RelayError extends Error {
  constructor(message, status = null) {
    super(message);
    this.status = status;
  }
}

// Relay's address and how to sign in there. An address with name:password@ in it is a proxy's own sign-in (Caddy's
// basic_auth): that goes to the proxy, and the app password in a header of Relay's own, as Relay's Python SDK does.
function credentials(url, password) {
  let parsed;
  try {
    parsed = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(url) ? url : `http://${url}`);
  } catch {
    throw new RelayError(`That isn't an address: ${url}`);
  }
  const headers = {};
  if (parsed.username) {
    const given = `${decodeURIComponent(parsed.username)}:${decodeURIComponent(parsed.password)}`;
    headers.Authorization = `Basic ${Buffer.from(given).toString("base64")}`;
    if (password) headers["X-Relay-App-Password"] = password;
    parsed.username = "";
    parsed.password = "";
  } else if (password) {
    headers.Authorization = `Bearer ${password}`;
  }
  return { base: parsed.href.replace(/\/+$/, ""), headers };
}

// Relay's own words for an error (its JSON's "error"), else the HTTP status
function said(text, response) {
  try {
    const found = JSON.parse(text || "{}").error;
    if (found) return String(found);
  } catch { /* not JSON: a proxy's page */ }
  const status = `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ""}`;
  // (a 401 that isn't Relay's: a proxy in front of it, as Caddy's basic_auth, asks for its own sign-in)
  return response.status === 401 ? `${status}: the address asks for a sign-in of its own (a proxy's, in front of `
    + "Relay): put it in the address, as https://name:password@host" : status;
}

// A server-sent stream, a piece at a time: each event once its blank line has come
class Stream {
  constructor() {
    this.rest = "";
    this.event = null;
    this.data = [];
  }

  push(text) {
    const done = [];
    const lines = (this.rest + text).split(/\r\n|\r|\n/);
    this.rest = lines.pop();  // (a line not ended yet)
    for (const line of lines) {
      if (line === "") {
        if (this.event !== null || this.data.length) done.push({ event: this.event, data: this.data.join("\n") });
        this.event = null;
        this.data = [];
      } else if (line.startsWith("event:")) {
        this.event = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        this.data.push(line.slice(5).replace(/^ /, ""));
      }  // (id:, and a sign of life ": keep-alive", are passed by)
    }
    return done;
  }
}

class Client {
  constructor(url, password, { version = "", fetch = globalThis.fetch } = {}) {
    const { base, headers } = credentials(url, password);
    this.url = base;
    this.headers = { ...headers, "X-Relay-Request": "1", "User-Agent": `${AGENT}/${version}` };
    this.fetch = fetch;
  }

  async request(method, path, data, { raw, headers = {}, signal } = {}) {
    let response;
    try {
      response = await this.fetch(this.url + path, {
        method, signal,
        headers: { ...this.headers, ...(data !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
        body: raw !== undefined ? raw : data !== undefined ? JSON.stringify(data) : undefined,
      });
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      throw new RelayError(
        `Can't reach Relay at ${this.url}: ${error?.cause?.code || error?.cause?.message || error?.message}`);
    }
    const text = await response.text();
    if (!response.ok) throw new RelayError(said(text, response), response.status);
    return (response.headers.get("content-type") || "").includes("application/json")
      ? JSON.parse(text || "null") : text;
  }

  get(path, options) { return this.request("GET", path, undefined, options); }
  post(path, data = {}, options) { return this.request("POST", path, data, options); }
  patch(path, data, options) { return this.request("PATCH", path, data, options); }
  put(path, data, options) { return this.request("PUT", path, data, options); }
  del(path, options) { return this.request("DELETE", path, undefined, options); }

  // A file sent as it is, for a message's attachments: its upload (id, name, …)
  upload(name, bytes, type = "application/octet-stream") {
    return this.request("POST", "/api/uploads", undefined,
      { raw: bytes, headers: { "X-Filename": encodeURIComponent(name), "Content-Type": type } });
  }

  // A run's events as they come, to `on`, until it ends: what came before first, then a "caught-up", then the rest.
  async events(runId, on, { signal } = {}) {
    let response;
    try {
      response = await this.fetch(`${this.url}/api/runs/${encodeURIComponent(runId)}/events`,
        { headers: { ...this.headers, Accept: "text/event-stream" }, signal });
    } catch (error) {
      if (error?.name === "AbortError") return;
      throw new RelayError(`Can't reach Relay at ${this.url}: ${error?.cause?.code || error?.message}`);
    }
    if (!response.ok) throw new RelayError(said(await response.text(), response), response.status);
    const stream = new Stream();
    const decoder = new TextDecoder();
    try {
      for await (const chunk of response.body) {
        for (const { event, data } of stream.push(decoder.decode(chunk, { stream: true }))) {
          if (event === "end") return;
          if (event) on({ type: event });  // (a named one: caught-up)
          else if (data) on(JSON.parse(data));
        }
      }
    } catch (error) {
      if (error?.name === "AbortError" || signal?.aborted) return;
      throw new RelayError(`Lost Relay at ${this.url} while an answer came: ${error?.message || error}`);
    }
  }
}

// Whether version a is newer than b (x.y.z)
function newer(a, b) {
  const parts = (v) => String(v || "0").split(".").map((n) => parseInt(n, 10) || 0);
  const [x, y] = [parts(a), parts(b)];
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0);
  }
  return false;
}

module.exports = { AGENT, Client, RelayError, Stream, credentials, newer, said };
