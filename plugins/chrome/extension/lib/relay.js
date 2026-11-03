// Relay's API, as the extension uses it: the same calls as Relay's Python SDK (sdk/python/relay_sdk).
// Signed in with an app password (Settings → Account), else with the browser's own sign-in (its cookie).

export class RelayError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.status = status;
  }
}

export const DEFAULTS = {
  url: "",            // Relay's address, e.g. https://relay.example.com or http://my-server:8420
  appPassword: "",    // relay_…; empty: the browser's sign-in to Relay
  provider: "",       // the agent for new chats; empty: your latest conversation's
  model: "",
  enterSends: true,   // Enter sends, Shift+Enter a new line (else Ctrl/Cmd+Enter sends)
  pageLimit: 40000,   // characters of a page's text that go along
  browserAgents: true,  // Relay's agents may use this browser (with Relay's chrome plugin, each step allowed)
};

export async function loadSettings() {
  const { settings } = await chrome.storage.local.get("settings");
  return { ...DEFAULTS, ...(settings || {}) };
}

export async function saveSettings(changes) {
  const settings = { ...(await loadSettings()), ...changes };
  await chrome.storage.local.set({ settings });
  return settings;
}

// An address as typed (my-server:8420, https://name:password@relay.example/relay/): where Relay is, the
// host permission it needs, and a proxy's own sign-in (Caddy's basic_auth), which a fetch can't take in its URL.
export function parseAddress(raw) {
  let text = String(raw || "").trim();
  if (!text) return null;
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) text = `http://${text}`;
  let url;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  if (!/^https?:$/.test(url.protocol)) return null;
  const proxy = url.username
    ? `Basic ${btoa(`${decodeURIComponent(url.username)}:${decodeURIComponent(url.password)}`)}` : null;
  const base = `${url.origin}${url.pathname.replace(/\/+$/, "")}`;
  return { base, origin: url.origin, port: Number(url.port) || (url.protocol === "https:" ? 443 : 80),
    pattern: `${url.protocol}//${url.hostname}/*`, proxy };
}

export class Relay {
  constructor(settings) {
    this.settings = settings;
    this.address = parseAddress(settings.url);
  }

  get configured() {
    return Boolean(this.address);
  }

  // Whether Chrome lets the extension reach Relay (Settings asks for it when the address is saved).
  async allowed() {
    return Boolean(this.address) && chrome.permissions.contains({ origins: [this.address.pattern] });
  }

  link(path = "/") {
    return this.address ? `${this.address.base}${path}` : null;
  }

  auth() {
    const password = this.settings.appPassword?.trim();
    if (this.address?.proxy) {  // (Authorization is the proxy's: the app password goes in Relay's own header)
      return { Authorization: this.address.proxy, ...(password ? { "X-Relay-App-Password": password } : {}) };
    }
    return password ? { Authorization: `Bearer ${password}` } : {};
  }

  async request(method, path, body, { raw = null, headers = {}, signal = null } = {}) {
    if (!this.address) throw new RelayError("Relay's address isn't set (the extension's settings)");
    const options = {
      method,
      headers: { "X-Relay-Request": "1", ...this.auth(), ...headers },
      credentials: this.settings.appPassword ? "omit" : "include",
      signal,
    };
    if (raw) {
      options.body = raw;
    } else if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(this.link(path), options);
    } catch {
      throw new RelayError(await this.unreachable());
    }
    const type = response.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await response.json().catch(() => ({})) : await response.text();
    if (!response.ok) {
      throw new RelayError(data?.error
        || `Relay answered ${response.status} ${response.statusText}`.trim(), response.status);
    }
    return data;
  }

  async unreachable() {
    if (!(await this.allowed())) return "Chrome doesn't let the extension reach Relay yet: "
      + "save its address in the settings";
    return `Can't reach Relay at ${this.address.base}`;
  }

  // One of Relay's server-sent streams: its messages ({ event, data, id }) until its "end" (that one too). A lost
  // connection is picked up again, at `path()` as it is then (and with Last-Event-ID, as an EventSource does);
  // `what` it brings, for an error.
  async *stream(path, signal, what) {
    let last = null;
    let tries = 0;
    for (;;) {
      let response;
      try {
        response = await fetch(this.link(path()), {
          headers: { Accept: "text/event-stream", ...this.auth(), ...(last ? { "Last-Event-ID": last } : {}) },
          credentials: this.settings.appPassword ? "omit" : "include",
          signal,
        });
      } catch (error) {
        if (signal?.aborted) return;
        if (++tries > 5) throw new RelayError(await this.unreachable());
        await pause(1000 * tries, signal);
        continue;
      }
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new RelayError(data.error || `Relay answered ${response.status}`, response.status);
      }
      try {
        for await (const message of readStream(response.body)) {
          tries = 0;
          if (message.id) last = message.id;
          yield message;
          if (message.event === "end") return;
        }
        return;  // (the stream closed without its end: what it was about is over)
      } catch (error) {
        if (signal?.aborted) return;
        if (++tries > 5) throw new RelayError(`Lost Relay while ${what} came: ${error.message || error}`);
        await pause(1000 * tries, signal);
      }
    }
  }

  // A run's events as they come, until its end.
  async *events(runId, signal) {
    const path = () => `/api/runs/${encodeURIComponent(runId)}/events`;
    for await (const message of this.stream(path, signal, "the answer")) {
      if (!message.event && message.data) yield JSON.parse(message.data);
    }
  }

  // A script run's output as it's written, from `after` characters on (a lost connection goes on from those
  // seen by then, as Relay's Scripts page does): { text } each time, { conversations } (those it started) when
  // they change, and last { end }: the run as it ended.
  async *scriptOutput(runId, after, signal) {
    let seen = after;
    const path = () => `/api/scripts/runs/${encodeURIComponent(runId)}/output?after=${seen}`;
    for await (const message of this.stream(path, signal, "the script's output")) {
      const data = parse(message.data);
      if (message.event === "end") {
        yield { end: data || {} };
        return;
      }
      if (message.event === "conversations" && Array.isArray(data)) {
        yield { conversations: data };
      } else if (!message.event && typeof data?.text === "string") {
        seen += data.text.length;
        yield { text: data.text };
      }
    }
  }

  // What the extension asks of Relay

  state() { return this.request("GET", "/api/state"); }

  async providers() { return (await this.request("GET", "/api/providers")).providers || []; }

  async models(provider) {
    return (await this.request("GET", `/api/providers/${encodeURIComponent(provider)}/models`)).models || [];
  }

  conversations(query = "") {
    return this.request("GET", `/api/conversations${query ? `?q=${encodeURIComponent(query)}` : ""}`);
  }

  conversation(id) { return this.request("GET", `/api/conversations/${encodeURIComponent(id)}`); }

  create({ provider, model, title }) {
    const data = { provider_id: provider, mode: "chat" };
    if (model) data.model = model;
    if (title) data.title = title;
    return this.request("POST", "/api/conversations", data);
  }

  change(id, data) { return this.request("PATCH", `/api/conversations/${encodeURIComponent(id)}`, data); }

  send(id, text, attachments = []) {
    return this.request("POST", `/api/conversations/${encodeURIComponent(id)}/messages`, { text, attachments });
  }

  // { queued: its id }, or { run_id } when the answer had just ended and it went right away.
  queue(id, text, attachments = []) {
    return this.request("POST", `/api/conversations/${encodeURIComponent(id)}/queue`, { text, attachments });
  }

  // A queued message sent now (after a stopped or failed answer the queue waits); { run_id }.
  sendQueued(id, queuedId) {
    return this.request("POST",
      `/api/conversations/${encodeURIComponent(id)}/queue/${encodeURIComponent(queuedId)}/send`);
  }

  unqueue(id, queuedId) {
    return this.request("DELETE", `/api/conversations/${encodeURIComponent(id)}/queue/${encodeURIComponent(queuedId)}`);
  }

  stop(runId) { return this.request("POST", `/api/runs/${encodeURIComponent(runId)}/stop`); }

  answerPermission(runId, id, decision) {
    return this.request("POST", `/api/runs/${encodeURIComponent(runId)}/permission`,
      { id: String(id), decision: String(decision) });
  }

  // Your scripts (Relay's Scripts page): each name, private, example, size, modified, running.
  async scripts() { return (await this.request("GET", "/api/scripts")).scripts || []; }

  // One: its text and its last runs (newest first).
  script(name) { return this.request("GET", `/api/scripts/${encodeURIComponent(name)}`); }

  // Run it, as the page's Run does (in its folder's WorkBox, signed in as you); `args` as typed in its field.
  async runScript(name, args = "") {
    return (await this.request("POST", `/api/scripts/${encodeURIComponent(name)}/run`, { args })).run;
  }

  // A run as it is now: script, args, status, exit_code, started, finished, output (its last part: `cut`
  // characters before it are gone), conversations.
  scriptRun(id) { return this.request("GET", `/api/scripts/runs/${encodeURIComponent(id)}`); }

  stopScript(id) { return this.request("POST", `/api/scripts/runs/${encodeURIComponent(id)}/stop`); }

  scriptLink(name) {
    return this.link(name ? `/scripts/${name.split("/").map(encodeURIComponent).join("/")}` : "/scripts");
  }

  // Relay's chrome plugin (plugins/chrome): what its tools ask of this browser. Whether Relay has it, loaded and on
  // ({ name, version, … } from Relay's list of plugins), or null.
  async chromePlugin() {
    const { plugins = [] } = await this.request("GET", "/api/plugins");
    return plugins.find((p) => p.name === "chrome" && p.loaded && p.enabled !== false) || null;
  }

  // The commands waiting for an answer, once something changed since `since` (or after `wait` seconds):
  // { version, commands }.
  chromeCommands(since, wait, signal) {
    return this.request("GET", `/api/plugins/chrome/commands?since=${since}&wait=${wait}`, undefined, { signal });
  }

  claimChromeCommand(id, client) {
    return this.request("POST", `/api/plugins/chrome/commands/${encodeURIComponent(id)}/claim`, { client });
  }

  answerChromeCommand(id, result) {
    return this.request("POST", `/api/plugins/chrome/commands/${encodeURIComponent(id)}/result`, result);
  }

  async upload(blob, name) {
    const made = await this.request("POST", "/api/uploads", undefined, {
      raw: blob,
      headers: { "X-Filename": encodeURIComponent(name), "Content-Type": blob.type || "application/octet-stream" },
    });
    return made.id;
  }

  // The agent for a new chat, as the SDK picks it: the settings' one; else your latest conversation's; else
  // the first provider that's on.
  async defaultAgent(providers = null) {
    const enabled = (providers || await this.providers()).filter((p) => p.enabled !== false);
    const known = (id) => enabled.some((p) => p.id === id);
    if (this.settings.provider && known(this.settings.provider)) {
      return { provider: this.settings.provider, model: this.settings.model || "" };
    }
    const latest = (await this.conversations().catch(() => [])).find((row) => known(row.provider_id));
    if (latest) return { provider: latest.provider_id, model: latest.model || "" };
    if (!enabled.length) throw new RelayError("No provider in Relay is on (Relay's Settings → Providers)");
    return { provider: enabled[0].id, model: "" };
  }
}

export async function connect() {
  return new Relay(await loadSettings());
}

// Server-sent events from a fetch's body: { event, data, id } each.
async function* readStream(body) {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let message = { event: null, data: [], id: null };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += value;
      const lines = buffer.split(/\r\n|\r|\n/);
      buffer = lines.pop();
      for (const line of lines) {
        if (!line) {
          if (message.event || message.data.length) yield { ...message, data: message.data.join("\n") };
          message = { event: null, data: [], id: null };
        } else if (!line.startsWith(":")) {  // (": keep-alive", Relay's sign of life every 15 s)
          const at = line.indexOf(":");
          const field = at < 0 ? line : line.slice(0, at);
          const value = at < 0 ? "" : line.slice(at + 1).replace(/^ /, "");
          if (field === "event") message.event = value;
          else if (field === "data") message.data.push(value);
          else if (field === "id") message.id = value;
        }
      }
    }
  } finally {
    reader.cancel().catch(() => {});  // (the connection closed too, not left to the garbage collector)
  }
}

function parse(data) {
  try {
    return data ? JSON.parse(data) : null;
  } catch {
    return null;
  }
}

function pause(ms, signal) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
  });
}
