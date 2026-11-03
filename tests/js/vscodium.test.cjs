// The VSCodium extension's parts without VSCodium (plugins/vscodium/extension/lib): Relay's API and a run's stream,
// an answer built from its events, and a file as it was before a change, from git's own diffs.
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");
const lib = (name) => require(path.join(root, "plugins/vscodium/extension/lib", name));
const { Client, Stream, credentials, newer } = lib("client.js");
const { answer, apply, turns } = lib("events.js");
const { before } = lib("patch.js");

// A fetch of our own: what was asked, and an answer
function fake(respond) {
  const asked = [];
  const fetch = async (url, options) => {
    asked.push({ url, ...options });
    return respond(url, options);
  };
  return { asked, fetch };
}

function json(body, status = 200) {
  return { ok: status < 400, status, statusText: status === 409 ? "Conflict" : "",
    headers: new Map([["content-type", "application/json"]]), text: async () => JSON.stringify(body) };
}

test("an app password goes as a bearer, and behind a proxy's sign-in in Relay's own header", () => {
  assert.deepEqual(credentials("http://localhost:8420/", "relay_x"),
    { base: "http://localhost:8420", headers: { Authorization: "Bearer relay_x" } });
  const proxied = credentials("https://me:p%40ss@relay.example.org/relay/", "relay_x");
  assert.equal(proxied.base, "https://relay.example.org/relay");
  assert.equal(proxied.headers.Authorization, `Basic ${Buffer.from("me:p@ss").toString("base64")}`);
  assert.equal(proxied.headers["X-Relay-App-Password"], "relay_x");
  assert.equal(credentials("192.168.1.2:8420", "").base, "http://192.168.1.2:8420");
  assert.throws(() => credentials("http://[bad", "x"), /isn't an address/);
});

test("a call sends JSON with Relay's headers, and an error comes in Relay's own words", async () => {
  const { asked, fetch } = fake((url) => url.endsWith("/busy") ? json({ error: "The task is working" }, 409)
    : json({ id: "c1" }));
  const client = new Client("http://relay:8420", "relay_x", { version: "1.0.0", fetch });
  assert.deepEqual(await client.post("/api/conversations", { mode: "chat" }), { id: "c1" });
  assert.equal(asked[0].method, "POST");
  assert.equal(asked[0].body, '{"mode":"chat"}');
  assert.equal(asked[0].headers["Content-Type"], "application/json");
  assert.equal(asked[0].headers["User-Agent"], "relay-vscodium/1.0.0");
  assert.equal(asked[0].headers.Authorization, "Bearer relay_x");
  await assert.rejects(client.post("/busy"),
    (error) => error.message === "The task is working" && error.status === 409);
  // A 401 that isn't Relay's (a proxy's own sign-in, as Caddy's basic_auth): said so, with what to do
  const proxied = new Client("https://relay.example.org", "x", { fetch: async () => ({ ok: false, status: 401,
    statusText: "Unauthorized", headers: new Map(), text: async () => "<html>401</html>" }) });
  await assert.rejects(proxied.get("/api/state"), /sign-in of its own .*https:\/\/name:password@host/);
  const down = new Client("http://relay:8420", "x", { fetch: async () => { throw new TypeError("fetch failed"); } });
  await assert.rejects(down.get("/api/state"), /Can't reach Relay at http:\/\/relay:8420/);
});

test("a run's stream: events across pieces, a sign of life, caught-up, until its end", async () => {
  const pieces =
    ["data: {\"type\":\"text\",\"de", "lta\":\"Hi\"}\n\n: keep-alive\n\n", "event: caught-up\ndata: {}\n\n",
      "id: 3\r\ndata: {\"type\":\"done\",\"status\":\"succeeded\"}\r\n\r\nevent: end\ndata: {}\n\n",
      "data: {\"type\":\"text\",\"delta\":\"never\"}\n\n"];
  const { asked, fetch } = fake(() => ({ ok: true, status: 200, body: (async function* () {
    for (const piece of pieces) yield new TextEncoder().encode(piece);
  })() }));
  const seen = [];
  await new Client("http://relay:8420", "x", { fetch }).events("run 1", (event) => seen.push(event));
  assert.equal(asked[0].url, "http://relay:8420/api/runs/run%201/events");
  assert.equal(asked[0].headers.Accept, "text/event-stream");
  assert.deepEqual(seen, [{ type: "text", delta: "Hi" }, { type: "caught-up" }, { type: "done", status: "succeeded" }]);
  const stream = new Stream();
  assert.deepEqual(stream.push("data: a\ndata: b\n"), []);
  assert.deepEqual(stream.push("\n"), [{ event: null, data: "a\nb" }]);
});

test("an answer from its events: text and thinking grow, tools get their results, questions change in place", () => {
  const a = answer("r1");
  const events = [
    { type: "step", model: "sonnet" },
    { type: "reasoning", delta: "Let me" }, { type: "reasoning", delta: " look." },
    { type: "tool_call", id: "t1", name: "Bash", input: { command: "ls -la\nmore" } },
    { type: "tool_call", id: "t2", name: "relay__delegate", input: {} },
    { type: "tool_call", id: "t3", name: "Read", input: { file_path: "/p/a.txt" } },
    { type: "tool_result", id: "t1", ok: true, output: "a.txt" },
    { type: "permission", id: "p0", action: "bash", resources: ["ls"], decision: "once" },
    { type: "permission", id: "p1", action: "bash", resources: ["rm x"], decision: null, expires: 99 },
    { type: "text", delta: "Hello" }, { type: "text", delta: " there" },
    { type: "permission", id: "p1", action: "bash", resources: ["rm x"], decision: "denied" },
    { type: "form", id: "f1", title: "Which?", fields: [{ key: "k", type: "string" }], status: "pending" },
    { type: "subagent", id: "c2", task: "Check the tests\nand more", status: "running", provider: "opencode" },
    { type: "subagent", id: "c2", status: "succeeded" },
    { type: "error", message: "Oops" }, { type: "error", message: "Oops" },
    { type: "done", status: "failed" },
  ];
  for (const event of events) apply(a, event);
  assert.equal(a.model, "sonnet");
  assert.equal(a.status, "failed");
  assert.deepEqual(a.blocks.map((b) => b.type),
    ["reasoning", "tool", "tool", "permission", "text", "form", "subagent", "error"]);
  assert.equal(a.blocks[0].text, "Let me look.");
  assert.deepEqual([a.blocks[1].name, a.blocks[1].what, a.blocks[1].kind, a.blocks[1].status, a.blocks[1].output],
    ["Bash", "ls -la …", "terminal", "done", "a.txt"]);
  assert.deepEqual([a.blocks[2].path, a.blocks[2].status], ["/p/a.txt", "stopped"]);  // (never answered: the run ended)
  assert.deepEqual([a.blocks[3].decision, a.blocks[3].asked, a.blocks[3].what], ["denied", true, "rm x"]);
  assert.equal(a.blocks[4].text, "Hello there");
  assert.deepEqual([a.blocks[5].status, a.blocks[5].run], ["cancelled", "r1"]);
  assert.deepEqual([a.blocks[6].label, a.blocks[6].status, a.blocks[6].provider],
    ["Check the tests …", "succeeded", "opencode"]);
  assert.deepEqual(a.hidden, ["t2"]);
});

test("a conversation's messages as turns: the one still coming is left to its stream", () => {
  const messages = [
    { id: 1, role: "user", content: "Hi", attachments: [{ id: "u1", name: "a.png" }], run_id: "r1" },
    { id: 2, role: "assistant", content: "Hello", run_id: "r1", status: "succeeded", run_model: "m",
      events: [{ type: "text", delta: "Hello" }, { type: "done", status: "succeeded" }] },
    { id: 3, role: "user", content: "Again", run_id: "r2" },
    { id: 4, role: "assistant", content: "", run_id: "r2", status: "failed", error: "No room", events: [] },
    { id: 5, role: "assistant", content: "Relay: switched to opencode", run_id: null },
    { id: 6, role: "user", content: "Go on", run_id: "r3" },
    { id: 7, role: "assistant", content: "", run_id: "r3", events: [] },
  ];
  const found = turns(messages, "r3");
  assert.deepEqual(found.map((t) => t.role), ["user", "assistant", "user", "assistant", "note", "user"]);
  assert.deepEqual(found[0].attachments, ["a.png"]);
  assert.deepEqual(found[1].blocks, [{ type: "text", text: "Hello" }]);
  assert.equal(found[1].model, "m");
  assert.deepEqual(found[3].blocks, [{ type: "error", message: "No room" }]);
});

test("a newer version is told by its numbers", () => {
  assert.equal(newer("1.0.10", "1.0.9"), true);
  assert.equal(newer("1.1", "1.0.9"), true);
  assert.equal(newer("1.0.0", "1.0.0"), false);
  assert.equal(newer("0.9.9", "1.0.0"), false);
});

// git's own diffs: each file as it was comes back from the file as it is, exactly
test("a file as it was before a change, from git's diff of it", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "relay-vscodium-"));
  const git = (...args) =>
    execFileSync("git", ["-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false",
      ...args], { cwd: repo, encoding: "utf8" });
  const lines = (n, f = (i) => `line ${i}`) => Array.from({ length: n }, (_, i) => f(i + 1)).join("\n") + "\n";
  const files = {
    "middle.txt": [lines(30), lines(30, (i) => i === 15 ? "changed 15" : `line ${i}`)],
    "hunks.txt": [lines(60), lines(62, (i) => i === 3 ? "new 3" : i === 58 ? "" : `line ${i > 3 ? i - 1 : i}`)],
    "removed.txt": ["one\ntwo\n", null],
    "made.txt": [null, "fresh\nfile\n"],
    "eol-gone.txt": ["a\nb\n", "a\nb\nc"],
    "eol-back.txt": ["a\nb", "a\nb\nc\n"],
    "empty-lines.txt": ["a\n\n\nb\n\nc\n", "a\n\nB\n\nc\n\n"],
    "crlf.txt": ["a\r\nb\r\nc\r\n", "a\r\nB\r\nc\r\n"],
    "start.txt": [lines(10), `new first\n${lines(10).split("\n").slice(2).join("\n")}`],
    "emptied.txt": ["x\ny\n", ""],
  };
  try {
    git("init", "-q");
    for (const [name, [was]] of Object.entries(files)) if (was !== null) fs.writeFileSync(path.join(repo, name), was);
    git("add", "-A");
    git("commit", "-q", "-m", "start");
    for (const [name, [, now]] of Object.entries(files)) {
      if (now === null) fs.rmSync(path.join(repo, name));
      else fs.writeFileSync(path.join(repo, name), now);
    }
    git("add", "-A");
    for (const [name, [was, now]] of Object.entries(files)) {
      const diff = git("diff", "--cached", "HEAD", "--", name);
      assert.equal(before(now ?? "", diff), was ?? "", name);
    }
    // A file changed since its diff, a diff cut short, a binary one: none can be told exactly
    const diff = git("diff", "--cached", "HEAD", "--", "middle.txt");
    assert.equal(before(files["middle.txt"][1].replace("line 14", "edited"), diff), null);
    assert.equal(before(files["middle.txt"][1], diff.split("\n").slice(0, -4).join("\n")), null);
    assert.equal(before("x", "diff --git a/p.png b/p.png\nBinary files a/p.png and b/p.png differ\n"), null);
    assert.equal(before("same", "diff --git a/a b/b\nsimilarity index 100%\nrename from a\nrename to b\n"), "same");
    assert.equal(before("", "diff --git a/e b/e\nnew file mode 100644\nindex 0000000..e69de29\n"), "");
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

// This computer as one of Relay's (lib/connector.js): relay-computer.py's jobs and loop, in Node
const connector = lib("connector.js");

test("the computer runs a command in its folder, with its output, a time-out that ends what it started", async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "relay-computer-"));
  try {
    const said = [];
    const done =
      await connector.run({ command: "echo hi; echo oops >&2; pwd; exit 3", cwd: folder }, (t) => said.push(t));
    assert.deepEqual([done.ok, done.exit], [true, 3]);
    assert.match(done.output, /hi/);
    assert.match(done.output, /oops/);
    assert.ok(done.output.includes(fs.realpathSync(folder)));
    assert.match(said[0], /^> echo hi/);
    assert.match((await connector.run({ command: "pwd" })).output, new RegExp(os.homedir()));  // (~ by default)
    assert.deepEqual(await connector.run({ command: "true", cwd: path.join(folder, "nope") }),
      { ok: false, error: `No folder ${path.join(folder, "nope")}` });
    const started = Date.now();
    const late =
      await connector.run({ command: `sleep 30 & echo $! > ${folder}/child; sleep 30`, cwd: folder, timeout: 1 });
    assert.deepEqual([late.ok, late.error], [false, "Stopped after 1 seconds"]);
    assert.ok(Date.now() - started < 10000);
    const child = Number(fs.readFileSync(path.join(folder, "child"), "utf8"));
    await new Promise((resolve) => setTimeout(resolve, 200));
    assert.throws(() => process.kill(child, 0));  // (what it started went with it)
    const long = await connector.run({ command: "head -c 300000 /dev/zero | tr '\\0' x" });
    assert.match(long.output, /^\[… 240000 characters left out\]\n/);
    assert.equal(long.output.split("\n")[1].length, connector.MAX_OUTPUT);
  } finally {
    fs.rmSync(folder, { recursive: true, force: true });
  }
});

test("the computer reads and writes files, ~ and $VAR in their paths", () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "relay-computer-"));
  try {
    process.env.RELAY_TEST_FOLDER = folder;
    const wrote =
      connector.write({ path: "$RELAY_TEST_FOLDER/a/b.txt", content_b64: Buffer.from("héllo").toString("base64") });
    assert.deepEqual(wrote, { ok: true, path: path.join(folder, "a", "b.txt"), size: 6 });
    const got = connector.read({ path: `${folder}/a/b.txt` });
    assert.equal(Buffer.from(got.content_b64, "base64").toString(), "héllo");
    assert.deepEqual(connector.read({ path: `${folder}/none` }), { ok: false, error: `No file ${folder}/none` });
    assert.equal(connector.where("~/x"), path.join(os.homedir(), "x"));
    assert.equal(connector.token('NAME = "laptop"\nTOKEN = "abc_-1"\n'), "abc_-1");
    assert.equal(connector.token("nothing"), null);
  } finally {
    fs.rmSync(folder, { recursive: true, force: true });
  }
});

test("the computer's loop: work asked for, done, its result sent; a checkpoint first for what changes things",
  async () => {
    const asked = [];
    let polls = 0;
    let stop;
    const fetch = async (url, options) => {
      asked.push({ url, body: JSON.parse(options.body), headers: options.headers });
      if (url.endsWith("/poll")) {
        polls += 1;
        if (polls === 3) stop();
        const jobs = polls === 1
          ? [{ id: "j1", kind: "run", command: "echo yes" }, { id: "j2", kind: "read", path: "/none" },
            { id: "j3", kind: "frobnicate" }] : [];
        return { ok: true, status: 200, text: async () => JSON.stringify({ jobs }) };
      }
      return { ok: true, status: 200, text: async () => "{}" };
    };
    const before = [];
    const states = [];
    const c = new connector.Connector({ url: "https://me:pw@relay.example.org/", token: "t/1", fetch,
      before: async (job) => before.push(job.id), state: (s) => states.push(s) });
    stop = () => c.stop();
    await c.start();
    await new Promise((resolve) => setTimeout(resolve, 300));
    assert.equal(asked[0].url, "https://relay.example.org/computer/t%2F1/poll");
    assert.equal(asked[0].headers.Authorization, `Basic ${Buffer.from("me:pw").toString("base64")}`);
    assert.equal(asked[0].body.info.client, "vscodium");
    const results = Object.fromEntries(asked.filter((a) => a.url.endsWith("/result")).map((a) => [a.body.id, a.body]));
    assert.deepEqual([results.j1.ok, results.j1.exit, results.j1.output], [true, 0, "yes\n"]);
    assert.deepEqual(results.j2, { id: "j2", ok: false, error: "No file /none" });
    assert.deepEqual(results.j3, { id: "j3", ok: false, error: "Error: No job “frobnicate” here" });
    assert.deepEqual(before, ["j1"]);  // (a read changes nothing)
    assert.deepEqual(states, ["online"]);
    // Relay no longer knows it: it stops, and says so
    const gone = new connector.Connector({ url: "http://relay", token: "x", state: (s) => states.push(s),
      fetch: async () => ({ ok: false, status: 404, text: async () => "{}" }) });
    await gone.start();
    assert.equal(states.at(-1), "unknown");
  });

// A computer task's checkpoints (lib/checkpoints.js): what the agent changed in a folder on this computer, and back
const { Checkpoints } = lib("checkpoints.js");

test("checkpoints keep a folder from outside, say what changed, and put files back (and that again)", async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "relay-folder-"));
  const store = fs.mkdtempSync(path.join(os.tmpdir(), "relay-store-"));
  const put = (name, text) => {
    fs.mkdirSync(path.dirname(path.join(folder, name)), { recursive: true });
    fs.writeFileSync(path.join(folder, name), text);
  };
  try {
    put("keep.txt", "one\ntwo\nthree\n");
    put("gone.txt", "bye\n");
    put("old name.txt", "same\ncontent\nhere\n");
    put(".gitignore", "secret.env\n");
    put("secret.env", "KEY=1\n");
    put("node_modules/x/index.js", "big\n");
    const points = new Checkpoints(folder, store);
    const base = await points.take("The task started");
    assert.match(base, /^[0-9a-f]{40}$/);
    assert.ok(!fs.existsSync(path.join(folder, ".git")));  // (from outside)
    assert.equal(await points.take("again"), base);  // (nothing changed: the same one)
    put("keep.txt", "one\nTWO\nthree\nfour\n");
    fs.rmSync(path.join(folder, "gone.txt"));
    fs.renameSync(path.join(folder, "old name.txt"), path.join(folder, "new name.txt"));
    put("src/made.py", "print(1)\n");
    put("secret.env", "KEY=2\n");
    put("node_modules/x/index.js", "bigger\n");
    const { files } = await points.changes(base);
    const by = Object.fromEntries(files.map((f) => [f.path, f]));
    assert.deepEqual(Object.keys(by).sort(), ["gone.txt", "keep.txt", "new name.txt", "src/made.py"]);
    assert.deepEqual([by["keep.txt"].status, by["keep.txt"].additions, by["keep.txt"].deletions], ["modified", 2, 1]);
    assert.deepEqual([by["new name.txt"].status, by["new name.txt"].from], ["renamed", "old name.txt"]);
    assert.equal(by["gone.txt"].status, "deleted");
    assert.equal(by["src/made.py"].status, "added");
    assert.match(await points.diff(base, by["keep.txt"]), /-two\n\+TWO/);
    assert.equal((await points.original(base, "keep.txt")).toString(), "one\ntwo\nthree\n");
    assert.equal(await points.original(base, "src/made.py"), null);
    // One file back, then all of them; what was there just before can be put back too
    await points.restore(base, [by["keep.txt"]]);
    assert.equal(fs.readFileSync(path.join(folder, "keep.txt"), "utf8"), "one\ntwo\nthree\n");
    const undo = await points.restore(base);
    assert.ok(!fs.existsSync(path.join(folder, "src/made.py")) && !fs.existsSync(path.join(folder, "new name.txt")));
    assert.equal(fs.readFileSync(path.join(folder, "old name.txt"), "utf8"), "same\ncontent\nhere\n");
    assert.equal(fs.readFileSync(path.join(folder, "gone.txt"), "utf8"), "bye\n");
    assert.equal(fs.readFileSync(path.join(folder, "secret.env"), "utf8"), "KEY=2\n");  // (ignored: left alone)
    assert.deepEqual((await points.changes(base)).files, []);
    await points.restore(undo);
    assert.equal(fs.readFileSync(path.join(folder, "src/made.py"), "utf8"), "print(1)\n");
    assert.ok(!fs.existsSync(path.join(folder, "gone.txt")));
  } finally {
    fs.rmSync(folder, { recursive: true, force: true });
    fs.rmSync(store, { recursive: true, force: true });
  }
});

test("an edit's lines for the chat: Edit, MultiEdit, opencode's edit, a Write, a patch", () => {
  const { editOf, lineDiff, summary, usageLine } = lib("events.js");
  assert.deepEqual(lineDiff("a\nb\nc", "a\nB\nc\nd"), [[" ", "a"], ["-", "b"], ["+", "B"], [" ", "c"], ["+", "d"]]);
  assert.deepEqual(editOf({ file_path: "/p/x.py", old_string: "x = 1", new_string: "x = 2" }),
    [["-", "x = 1"], ["+", "x = 2"]]);
  assert.deepEqual(editOf({ filePath: "/p/x.py", oldString: "a", newString: "b" }), [["-", "a"], ["+", "b"]]);
  assert.deepEqual(editOf({ file_path: "/p/x",
    edits: [{ old_string: "a", new_string: "b" }, { old_string: "c", new_string: "d" }] }),
    [["-", "a"], ["+", "b"], ["@", "…"], ["-", "c"], ["+", "d"]]);
  assert.deepEqual(editOf({ file_path: "/p/new.md", content: "# Hi\nthere" }), [["+", "# Hi"], ["+", "there"]]);
  assert.deepEqual(editOf({ input:
    "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n ctx\n*** End Patch" }).slice(3, 6),
    [["-", "old"], ["+", "new"], [" ", "ctx"]]);
  assert.equal(editOf({ command: "ls" }), null);
  // Relay's tools script by its whole path: "relay", as Relay's page says it
  assert.equal(summary({ command: "/home/u/.cache/opencode-manager/relay-tools/AbC_1/relay run --cwd /x 'ls'" }),
    "relay run --cwd /x 'ls'");
  assert.equal(usageLine({ tokens: { input_tokens: 1200, output_tokens: 310 }, cost: 0.0123, billing: "api" }),
    "1,200 in · 310 out · $0.01 API");
  assert.equal(usageLine({ tokens: { input_tokens: 5, cache_read_input_tokens: 1000 }, billing: "subscription" }),
    "1,005 in · in plan");
  assert.equal(usageLine(null), "");
});

test("an answer knows the message it answered (Retry) and keeps its changes and tokens", () => {
  const found = turns([
    { id: 7, role: "user", content: "Do it", run_id: "r1" },
    { id: 8, role: "assistant", content: "Done", run_id: "r1", status: "succeeded", checkpoint: "abc",
      changes: { files: 2, additions: 3, deletions: 1 },
      events: [{ type: "text", delta: "Done" },
        { type: "usage", tokens: { input_tokens: 9 }, cost: 0, billing: "free" }] },
  ]);
  assert.deepEqual([found[1].prompt, found[1].id, found[1].undo], [7, 8, true]);
  assert.deepEqual(found[1].changes, { files: 2, additions: 3, deletions: 1 });
  assert.deepEqual(found[1].usage, { tokens: { input_tokens: 9 }, cost: 0, billing: "free" });
});

test("paths from two machines: Relay's with /, this computer's with \\ and a drive letter", () => {
  const { inside, near } = lib("places.js");
  assert.equal(inside("/a/demo/x.py", "/a/demo"), true);
  assert.equal(inside("/a/demo", "/a/demo/"), true);
  assert.equal(inside("/a/demo2/x.py", "/a/demo"), false);  // (not a prefix of a name)
  assert.equal(inside("C:\\Users\\Me\\Proj\\src\\a.ts", "c:\\users\\me\\proj"), true);
  assert.equal(inside("c:/users/me/proj2", "C:\\Users\\Me\\Proj"), false);
  assert.equal(inside("/", "/"), true);
  assert.equal(inside("/x", "/"), true);
  assert.equal(near("/a", "/a/b"), true);
  assert.equal(near("/a/b", "/a"), true);
  assert.equal(inside(null, "/a"), false);
});

test("checkpoints asked for at once take their turns; odd names come back as they were", async () => {
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "relay-folder-"));
  const store = fs.mkdtempSync(path.join(os.tmpdir(), "relay-store-"));
  try {
    const names = ["spaced name.txt", "ünïcødé ✓.md", "quote\"d.txt", "tab\tname.txt", "dir with space/inner.txt"];
    for (const name of names) {
      fs.mkdirSync(path.dirname(path.join(folder, name)), { recursive: true });
      fs.writeFileSync(path.join(folder, name), `${name}\n`);
    }
    fs.mkdirSync(path.join(folder, "nested"));
    execFileSync("git", ["init", "-q"], { cwd: path.join(folder, "nested") });  // (a repository of its own in it)
    fs.writeFileSync(path.join(folder, "nested", "in.txt"), "in\n");
    const points = new Checkpoints(folder, store);
    const [base, again] =
      await Promise.all([points.take("one"), points.take("two"), points.changes("HEAD").catch(() => null)]);
    assert.equal(base, again);  // (one after the other: the second found nothing new)
    for (const name of names) fs.writeFileSync(path.join(folder, name), "changed\n");
    const looks = await Promise.all([points.changes(base), points.changes(base), points.take("three")]);
    assert.deepEqual(looks[0].files.map((f) => f.path).sort(), [...names].sort());
    assert.deepEqual(looks[1].files.map((f) => f.path).sort(), [...names].sort());
    await points.restore(base);
    for (const name of names) assert.equal(fs.readFileSync(path.join(folder, name), "utf8"), `${name}\n`);
    assert.equal(fs.readFileSync(path.join(folder, "nested", "in.txt"), "utf8"), "in\n");  // (left alone)
  } finally {
    fs.rmSync(folder, { recursive: true, force: true });
    fs.rmSync(store, { recursive: true, force: true });
  }
});

// Agents use VSCodium: a window's questions, and how answers read
test("a name's place on its line: the symbol as a whole name, else the line's first name past the keywords", () => {
  const { column } = lib("tools.js");
  assert.equal(column("  const total = sum(items);", "sum"), 16);
  assert.equal(column("  const sumOf = sum(items);", "sum"), 16);  // (not the one in sumOf, at 8)
  assert.equal(column("export async function load(id) {", null), 22);
  assert.equal(column("  return;", null), -1);
  assert.equal(column("x = a.$el", "$el"), 6);
  assert.equal(column("nothing here", "gone"), -1);
});

test("problems, places, a definition and symbols, as an agent reads them", () => {
  const { definitionText, placesText, problemLine, problemsText, symbolsText } = lib("tools.js");
  assert.equal(problemLine({ path: "a.ts", line: 3, col: 5, error: true,
    message: "Nope\nmore", source: "ts", code: 2322 }),
    "a.ts:3:5: error: Nope (ts 2322)");
  assert.equal(problemLine({ path: "b.py", line: 1, col: 1, error: false, message: "unused", source: "ruff" }),
    "b.py:1:1: warning: unused (ruff)");
  assert.equal(problemsText([]), null);
  assert.equal(problemsText([{ path: "b", line: 1, col: 1, error: false, message: "w" },
    { path: "a", line: 2, col: 1, error: true, message: "e" }], { where: " in the folder" }),
  "1 error and 1 warning in the folder:\na:2:1: error: e\nb:1:1: warning: w");
  assert.equal(placesText([{ path: "b.js", line: 9, col: 2, text: "  go();" },
    { path: "a.js", line: 1, col: 1, text: "go" }],
    { what: "reference" }), "2 references in 2 files:\na.js:1:1: go\nb.js:9:2: go();");
  assert.equal(definitionText([{ path: "a.js", line: 2, col: 10, lines: [[1, "// x"], [2, "function go() {"]] }]),
    "a.js:2:10\n    1  // x\n    2  function go() {");
  assert.equal(symbolsText([{ name: "Foo", kind: "class", container: "m", path: "a.ts", line: 4 }]),
    "class Foo (in m): a.ts:4");
  assert.equal(symbolsText([{ name: "A", kind: "class", line: 1, depth: 0 },
    { name: "b", kind: "method", line: 2, depth: 1 }],
    { outline: true }), "class A (line 1)\n  method b (line 2)");
});

test("a window says what it has, takes its questions and answers each", async () => {
  const { ToolWindow } = lib("tools.js");
  const sent = [];
  let round = 0;
  let window;
  const client = {
    post: async (route, body) => { sent.push([route, body]); return {}; },
    get: async (route) => {
      round += 1;
      if (round > 3) window.stop();  // (a failed check below would otherwise have it ask for ever)
      if (round === 1) {
        const error = new Error("Unknown window: say which folders it has first (windows)");
        error.status = 404;
        throw error;
      }
      if (round === 2) return { version: 1,
        questions: [{ id: "q1", action: "hover", args: { path: "a.js", line: 1 } }] };
      assert.match(route, /wait=0/);  // (while it answers one: no ask held open)
      window.stop();
      return { version: 1, questions: [{ id: "q1" }] };  // (the one it's answering: not taken twice)
    },
  };
  let carried = 0;
  window = new ToolWindow({ client, report: () => ({ folders: ["/p"], tasks: [], focused: true }),
    carry: async (question) => {
      carried += 1;
      await new Promise((resolve) => setTimeout(resolve, 500));
      if (question.args.path === "a.js") return "type: number";
      throw new Error("no");
    } });
  await window.start();
  await new Promise((resolve) => setTimeout(resolve, 600));
  assert.equal(round, 3);
  assert.equal(carried, 1);
  const told = sent.filter(([route]) => route.endsWith("/windows"));
  assert.equal(told.length, 2);  // (said again when Relay didn't know it)
  assert.deepEqual(told[0][1], { client: window.id, folders: ["/p"], tasks: [], focused: true });
  assert.deepEqual(sent.at(-1),
    ["/api/plugins/vscodium/tools/q1/result", { client: window.id, ok: true, text: "type: number" }]);
});

// Keep/Undo in the editor: the agent's changes a hunk at a time
test("hunks between a file before the agent and now, each put back on its own", () => {
  const { hunks, place, script, undo } = lib("hunks.js");
  const before = "a\nb\nc\nd\ne\n";
  const now = "a\nB\nc\nd\ne\nf";
  const found = hunks(before, now);
  assert.deepEqual(found.map((h) => [h.oldStart, h.old, h.newStart, h.new]),
    [[1, ["b\n"], 1, ["B\n"]], [5, [], 5, ["f"]]]);
  assert.equal(undo(now, found[0]), "a\nb\nc\nd\ne\nf");
  assert.equal(undo(now, found[1]), "a\nB\nc\nd\ne\n");
  assert.deepEqual(place(now, found[0]), { start: 2, end: 4, text: "b\n" });
  // (the same change at the same place: the same key, also once the other one was put back)
  assert.equal(hunks(before, undo(now, found[1]))[0].key, found[0].key);
  assert.notEqual(found[0].key, found[1].key);
  assert.deepEqual(hunks("x\n", "x\n"), []);
  assert.deepEqual(hunks("", "new\n").map((h) => h.new), [["new\n"]]);
  // (any two texts: the script gives both back, and every hunk put back, last first, gives the text before)
  let seed = 7;
  const random = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
  for (let n = 0; n < 500; n++) {
    const a = Array.from({ length: Math.floor(random() * 10) }, () => "abcd"[Math.floor(random() * 4)] + "\n");
    const b = a.filter(() => random() > 0.3)
      .flatMap((l) => (random() > 0.7 ? [l, "xy"[Math.floor(random() * 2)] + "\n"] : [l]));
    const [x, y] = [a.join(""), b.join("")];
    const ops = script(a, b);
    assert.equal(ops.filter(([s]) => s !== "+").map(([, l]) => l).join(""), x);
    assert.equal(ops.filter(([s]) => s !== "-").map(([, l]) => l).join(""), y);
    assert.equal(hunks(x, y).reverse().reduce(undo, y), x);
  }
});

// Inline editing (Ctrl+I): the question, and the code in the answer
test("an inline edit asks about the picked lines with those around them, and takes the code block back", () => {
  const { code, prompt } = lib("edit.js");
  const lines = Array.from({ length: 100 }, (_, n) => `line ${n + 1}`);
  const asked =
    prompt({ file: "src/a.js", language: "javascript", lines, from: 49, to: 50, instruction: " Make it loud " });
  assert.match(asked, /^Make it loud\n\nIn src\/a.js, lines 50-51:\n```javascript\nline 10\n/);  // (40 lines before)
  assert.match(asked, /line 49\n<<<PICKED LINES START>>>\nline 50\nline 51\n<<<PICKED LINES END>>>\nline 52\n/);
  assert.match(asked, /line 91\n```\n\nAnswer with only the new code/);
  assert.doesNotMatch(asked, /line 92\n/);
  const empty =
    prompt({ file: "b.py", language: "python", lines: ["x = 1", "", "y = 2"], from: 1, to: 1, instruction: "log" });
  assert.match(empty, /at line 2:\n```python\nx = 1\n<<<THE CURSOR: THE NEW CODE GOES HERE>>>\ny = 2\n```/);
  assert.match(prompt({ file: "c.md", language: "markdown", lines: ["```", "a", "```"], from: 1, to: 1,
    instruction: "x" }), /````markdown\n/);  // (a fence longer than the file's own)
  assert.equal(code("Sure:\n```js\n  if (!a) return 0;\n  return a;\n```\nThat handles it."),
    "  if (!a) return 0;\n  return a;");
  assert.equal(code("```\n<<<PICKED LINES START>>>\nx\n<<<PICKED LINES END>>>\n```"), "x");
  assert.equal(code("\n  bare = 1\n\n"), "  bare = 1");
});

// Permission control in VSCodium: asked before a change on this computer, one question at a time
test("permission control: what's asked, one at a time, for an answer, in time, and a command's time left", async () => {
  const { Gate, LATE, REFUSED } = lib("permissions.js");
  let mode = "ask";
  let clock = 1000000;
  const asked = [];
  let reply = async () => "once";
  const gate = new Gate({ mode: () => mode, about: async (job) => ({ id: "c1", run: job.run || null }),
    ask: async (job, about, seconds) => { asked.push([job.kind, about.run, seconds]); return reply(job); },
    now: () => clock });
  assert.deepEqual(await gate.allow({ kind: "read", path: "/x" }), { ok: true });  // (a read changes nothing)
  assert.deepEqual(await gate.allow({ kind: "write", path: "/x", run: "r1" }), { ok: true });
  assert.deepEqual(asked, [["write", "r1", 90]]);
  reply = async () => "denied";
  assert.deepEqual(await gate.allow({ kind: "run", command: "rm x", run: "r1" }), { ok: false, error: REFUSED });
  // Allowed for its answer: the run's next ones aren't asked, another run's are
  reply = async () => "answer";
  await gate.allow({ kind: "run", command: "a", run: "r2" });
  reply = async () => "denied";
  assert.deepEqual(await gate.allow({ kind: "write", path: "/y", run: "r2" }), { ok: true });
  assert.equal((await gate.allow({ kind: "write", path: "/y", run: "r3" })).ok, false);
  assert.equal(asked.length, 4);
  // Commands only, or not at all
  mode = "commands";
  assert.deepEqual(await gate.allow({ kind: "write", path: "/z", run: "r4" }), { ok: true });
  mode = "off";
  assert.deepEqual(await gate.allow({ kind: "run", command: "b", run: "r4" }), { ok: true });
  assert.equal(asked.length, 4);
  // A command's own time-out: what's left after the asking; a short one gets a short question
  mode = "ask";
  reply = async () => { clock += 30000; return "once"; };
  assert.deepEqual(await gate.allow({ kind: "run", command: "c", timeout: 120, run: "r5" }), { ok: true, timeout: 90 });
  await gate.allow({ kind: "run", command: "d", timeout: 20, run: "r5" });
  assert.equal(asked.at(-1)[2], 30);  // (20 + 25 − 15: before Relay stops waiting)
  // One question at a time: the second waits for the first, whose "for this answer" lets it through
  const order = [];
  let release;
  reply = (job) => new Promise((resolve) => {
    order.push(job.command);
    if (job.command === "first") release = () => resolve("answer");
    else resolve("denied");
  });
  const first = gate.allow({ kind: "run", command: "first", run: "r6" });
  const second = gate.allow({ kind: "run", command: "second", run: "r6" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(order, ["first"]);
  release();
  assert.equal((await first).ok, true);
  assert.equal((await second).ok, true);
  assert.deepEqual(order, ["first"]);
  // No time left for an answer (a question before it took it all): denied, as late, without asking
  const late = new Gate({ mode: () => "ask", now: () => clock, ask: async () => "once",
    about: async () => { clock += 200000; return null; } });
  assert.deepEqual(await late.allow({ kind: "run", command: "e" }), { ok: false, error: LATE });
});

test("the connector asks before a change and does nothing when refused, a command with the time it's left",
  async () => {
    const asked = [];
    let polls = 0;
    let stop;
    const fetch = async (url, options) => {
      asked.push({ url, body: JSON.parse(options.body) });
      if (url.endsWith("/poll")) {
        polls += 1;
        if (polls === 3) stop();
        const jobs = polls === 1 ? [{ id: "w", kind: "write", path: path.join(os.tmpdir(), "relay-never-written"),
          content_b64: "eA==" }, { id: "r", kind: "run", command: "echo ok", timeout: 120 },
          { id: "q", kind: "read", path: "/none" }] : [];
        return { ok: true, status: 200, text: async () => JSON.stringify({ jobs }) };
      }
      return { ok: true, status: 200, text: async () => "{}" };
    };
    const allowed = [];
    const before = [];
    const c = new connector.Connector({ url: "http://relay", token: "t", fetch,
      before: async (job) => before.push([job.id, job.timeout]),
      allow: async (job) => {
        allowed.push(job.id);
        return job.kind === "write"
          ? { ok: false, error: "The user refused this in VSCodium" } : { ok: true, timeout: 50 };
      } });
    stop = () => c.stop();
    await c.start();
    await new Promise((resolve) => setTimeout(resolve, 300));
    const results = Object.fromEntries(asked.filter((a) => a.url.endsWith("/result")).map((a) => [a.body.id, a.body]));
    assert.deepEqual(results.w, { id: "w", ok: false, error: "The user refused this in VSCodium" });
    assert.equal(fs.existsSync(path.join(os.tmpdir(), "relay-never-written")), false);
    assert.equal(results.r.output, "ok\n");
    assert.deepEqual(allowed.sort(), ["r", "w"]);  // (not the read)
    assert.deepEqual(before, [["r", 50]]);  // (the checkpoint only for what goes ahead)
  });

test("a tool's file by its whole path, also ~ and Windows'; the connector's command output as it comes, each job told",
  async () => {
    const pathOf = (p) => {
      const a = answer("r");
      apply(a, { type: "tool_call", id: "t", name: "Edit", input: { file_path: p } });
      return a.blocks[0].path;
    };
    assert.deepEqual(["/a/b.js", "~/x.txt", "C:\\x\\y.js", "c:/x.js", "rel/x.js"].map(pathOf),
      ["/a/b.js", "~/x.txt", "C:\\x\\y.js", "c:/x.js", null]);
    const pieces = [];
    const done =
      await connector.run({ command: "printf 'é'; sleep 0.2; echo two" }, () => {}, (text) => pieces.push(text));
    assert.equal(done.output, "étwo\n");
    assert.deepEqual(pieces.join(""), "étwo\n");
    assert.ok(pieces.length >= 2);  // (as it came: before and after the pause)
    let polls = 0;
    let stop;
    const told = [];
    const c = new connector.Connector({ url: "http://relay", token: "t",
      after: (job, result) => told.push([job.id, result.ok]),
      fetch: async (url) => {
        if (url.endsWith("/poll")) {
          polls += 1;
          if (polls === 2) stop();
          return { ok: true, status: 200, text: async () => JSON.stringify({ jobs: polls === 1
            ? [{ id: "a", kind: "read", path: "/none" }] : [] }) };
        }
        return { ok: true, status: 200, text: async () => "{}" };
      } });
    stop = () => c.stop();
    await c.start();
    await new Promise((resolve) => setTimeout(resolve, 100));
    assert.deepEqual(told, [["a", false]]);
  });

test("Relay on this machine or another: this computer's name as Relay names it", () => {
  const { hostName, sameMachine } = lib("places.js");
  assert.equal(hostName("MyLaptop.local"), "mylaptop");
  assert.equal(hostName("Alex's PC"), "alex-s-pc");
  assert.equal(sameMachine("http://localhost:8420", "laptop"), true);
  assert.equal(sameMachine("http://127.0.0.1:8420/relay", "laptop"), true);
  assert.equal(sameMachine("https://alex:pw@relay.example.org", "laptop"), false);
  assert.equal(sameMachine("http://laptop:8420", "Laptop"), true);
  assert.equal(sameMachine("192.168.1.2:8420", "laptop"), false);
});
