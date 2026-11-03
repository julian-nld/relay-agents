// This computer as one of Relay's ("Your own computers"), as relay-computer.py makes one, in VSCodium's own process:
// while VSCodium runs, agents in Relay can work in a folder it has open here. It asks Relay for work (a long poll),
// runs commands, reads and writes files, and sends each result back. Nothing of VSCodium's here: node --test tries it.
"use strict";

const { execFile, spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { credentials } = require("./client");

const VERSION = 1;  // (the protocol's, as relay-computer.py says it)
const MAX_OUTPUT = 60000;  // characters of a command's output sent back (its end)
const MAX_FILE = 20 * 1024 * 1024;
const POLL = 60000;  // ms a request for work may take (Relay answers within 25 s)
const WINDOWS = process.platform === "win32";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// A path as the computer has it: ~ for the home folder, $VAR (and %VAR% on Windows) from the environment
function where(given) {
  let place = String(given || "~");
  if (place === "~" || place.startsWith("~/") || place.startsWith("~\\")) place = os.homedir() + place.slice(1);
  place = place.replace(/\$(\w+)|\$\{(\w+)\}/g, (found, a, b) => process.env[a || b] ?? found);
  if (WINDOWS) place = place.replace(/%(\w+)%/g, (found, name) => process.env[name] ?? found);
  return path.resolve(place);
}

function about() {
  return {
    os: { win32: "Windows", darwin: "Darwin", linux: "Linux" }[process.platform] || process.platform,
    release: os.release(), version: (os.version ? os.version() : "").slice(0, 120), machine: os.arch(),
    hostname: os.hostname(), user: os.userInfo().username, home: os.homedir(), node: process.version,
    connector: VERSION, client: "vscodium",
    shell: WINDOWS ? process.env.COMSPEC || "cmd.exe" : process.env.SHELL || "/bin/sh",
  };
}

function decode(bytes) {
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return bytes.toString("latin1");
  }
}

// A command and everything it started, stopped
function end(child) {
  try {
    if (WINDOWS) execFile("taskkill", ["/T", "/F", "/PID", String(child.pid)], () => {});
    else process.kill(-child.pid, "SIGKILL");
  } catch { /* ended already */ }
}

// A command in a shell of its own process group (a time-out ends what it started too), stdin closed; its output also
// to `output` as it comes (VSCodium's terminal of what it ran)
function run(job, say = () => {}, output = null) {
  const cwd = where(job.cwd);
  const seconds = Number(job.timeout) || 120;
  say(`> ${job.command}${job.cwd ? `   (in ${cwd})` : ""}`);
  if (!fs.existsSync(cwd)
    || !fs.statSync(cwd).isDirectory()) return Promise.resolve({ ok: false, error: `No folder ${cwd}` });
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(String(job.command), { shell: true, cwd, detached: !WINDOWS, windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
      resolve({ ok: false, error: error.message });
      return;
    }
    const kept = [];
    let size = 0;
    let dropped = 0;
    const take = (piece) => {  // (its end is what's sent: what's long before it goes)
      kept.push(piece);
      size += piece.length;
      while (size > MAX_OUTPUT * 4 && kept.length > 1) {
        const gone = kept.shift();
        size -= gone.length;
        dropped += gone.length;
      }
    };
    if (output) {
      const { StringDecoder } = require("string_decoder");
      for (const stream of [child.stdout, child.stderr]) {
        const decoder = new StringDecoder("utf8");  // (a character split between two pieces comes whole)
        stream.on("data", (piece) => output(decoder.write(piece)));
      }
    }
    child.stdout.on("data", take);
    child.stderr.on("data", take);
    let late = false;
    const timer = setTimeout(() => {
      late = true;
      end(child);
    }, seconds * 1000);
    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ ok: false, error: error.message });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      const output = decode(Buffer.concat(kept));
      if (late) {
        resolve({ ok: false, error: `Stopped after ${seconds} seconds`, output: output.slice(-MAX_OUTPUT) });
        return;
      }
      const over = output.length - MAX_OUTPUT;
      resolve({ ok: true, exit: code, output: over > 0 || dropped
        ? `[… ${Math.max(over, 0) + dropped} characters left out]\n${output.slice(-MAX_OUTPUT)}` : output });
    });
  });
}

function read(job, say = () => {}) {
  const file = where(job.path);
  say(`reads ${file}`);
  let stat;
  try {
    stat = fs.statSync(file);
  } catch {
    stat = null;
  }
  if (!stat || !stat.isFile()) return { ok: false, error: `No file ${file}` };
  if (stat.size > MAX_FILE) return { ok: false,
    error: `${file} is ${stat.size} bytes; at most ${MAX_FILE} can be read` };
  return { ok: true, path: file, size: stat.size, content_b64: fs.readFileSync(file).toString("base64") };
}

function write(job, say = () => {}) {
  const file = where(job.path);
  const data = Buffer.from(String(job.content_b64 || ""), "base64");
  say(`writes ${file} (${data.length} bytes)`);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, data);
  return { ok: true, path: file, size: data.length };
}

const JOBS = { run, read, write };

class Connector {
  // `allow(job)`: asked (and waited for) before a job that changes something: {ok, timeout?} to go ahead (a
  // command's time-out as it's left), or {ok: false, error}; `before(job)`: called (and waited for) then, before it
  // runs: the review's checkpoint
  // `output(text)`: a command's output as it comes; `after(job, result)`: each job done
  constructor({ url, token, fetch = globalThis.fetch, say = () => {}, state = () => {}, before = null, allow = null,
    output = null, after = () => {} }) {
    const { base, headers } = credentials(url, null);  // (a proxy's own sign-in in the address; the token is the key)
    Object.assign(this, { url: base, headers, token, fetch, say, state, before, allow, output, after });
    this.stopped = false;
    this.online = false;
  }

  async start() {
    let wait = 2000;
    while (!this.stopped) {
      let answer;
      try {
        answer = await this.post("poll", { info: about() }, POLL);
      } catch (error) {
        if (this.stopped) return;
        if ([401, 403, 404].includes(error.status)) {
          this.say("Relay doesn't know this computer (any more): turn it on again (Relay: This computer)");
          this.state("unknown", error);
          return;
        }
        if (this.online) this.say(`Lost Relay (${error.message}); trying again`);
        this.online = false;
        this.state("offline", error);
        await sleep(wait);
        wait = Math.min(wait * 2, 30000);
        continue;
      }
      if (!this.online) {
        this.online = true;
        this.say("Connected: agents in Relay can work on this computer now.");
        this.state("online");
      }
      wait = 2000;
      for (const job of answer.jobs || []) this.handle(job);
    }
  }

  stop() {
    this.stopped = true;
    this.waiting?.abort();
  }

  async handle(job) {
    let result;
    try {
      const doing = JOBS[job.kind];
      if (!doing) throw new Error(`No job “${job.kind}” here`);
      const allowed = this.allow && job.kind !== "read" ? await this.allow(job) : { ok: true };
      if (!allowed.ok) {
        this.say(`refused: ${job.command || `write ${job.path}`}`);
        result = { ok: false, error: allowed.error };
      } else {
        if (allowed.timeout) job = { ...job, timeout: allowed.timeout };
        if (this.before && job.kind !== "read") await this.before(job);
        result = await doing(job, this.say, this.output);
      }
    } catch (error) {
      result = { ok: false, error: `${error.name || "Error"}: ${error.message}` };
    }
    try {
      this.after(job, result);
    } catch { /* (what VSCodium shows of it: never the job's result) */ }
    for (let attempt = 0; attempt < 5; attempt++) {
      try {
        await this.post("result", { id: job.id, ...result }, 60000);
        return;
      } catch {
        await sleep(2000 * (attempt + 1));
      }
    }
    this.say("Couldn't send a result back to Relay");
  }

  async post(what, data, timeout) {
    const controller = new AbortController();
    if (what === "poll") this.waiting = controller;
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await this.fetch(`${this.url}/computer/${encodeURIComponent(this.token)}/${what}`, {
        method: "POST", signal: controller.signal, body: JSON.stringify(data),
        headers: { ...this.headers, "Content-Type": "application/json",
          "User-Agent": `relay-vscodium-computer/${VERSION}` },
      });
      const text = await response.text();
      if (!response.ok) {
        const error = new Error(`Relay answered ${response.status}`);
        error.status = response.status;
        throw error;
      }
      return JSON.parse(text || "{}");
    } catch (error) {
      if (error.status) throw error;
      throw new Error(error.name === "AbortError" ? "no answer in time" : error.cause?.code || error.message);
    } finally {
      clearTimeout(timer);
    }
  }
}

// The token in Relay's connector for a computer (GET /api/computers/<id>/script): the key its connector polls with
function token(script) {
  const found = /^TOKEN = "([^"]+)"$/m.exec(String(script || ""));
  return found ? found[1] : null;
}

module.exports = { Connector, MAX_OUTPUT, about, read, run, token, where, write };
