// Relay's chrome plugin with a real Relay and a real agent: the agent finds a tab, reads it, types and clicks in it,
// is denied a click, takes a screenshot, and a step waiting when its run is stopped goes with it. Every step is
// allowed (or not) on its card in the panel, as the user would. Relay needs its chrome plugin (plugins/chrome) on.
// It makes one chat in Relay, which stays there.
//
//   RELAY_URL=http://127.0.0.1:8420 RELAY_APP_PASSWORD=relay_… node real_agents.cjs [provider] [model]

const fs = require("fs");
const http = require("http");
const path = require("path");
const { launch, check, finish, pluginVersion } = require("./harness.cjs");

const relay = (process.env.RELAY_URL || "http://127.0.0.1:8420").replace(/\/+$/, "");
const password = process.env.RELAY_APP_PASSWORD;
const provider = process.argv[2] || "claude-code";
const model = process.argv[3] ?? "haiku";
const { shots } = require("./harness.cjs");
const LONG = { timeout: 4 * 60000 };

// What only this page says (a new word each run), a field and a button that shows what was sent.
const WORD = `KESTREL-${Math.floor(1000 + Math.random() * 9000)}`;
const PAGE = `<!doctype html><html><head><title>Relay Chrome trial</title></head><body>
<h1 style="font-size:56px">ORANGE LIGHTHOUSE</h1>
<p>The secret word is ${WORD}.</p>
<label>Message <input id="message"></label> <button id="send" onclick="
  const p = document.createElement('p'); p.textContent = 'Sent: ' + document.getElementById('message').value;
  document.getElementById('list').append(p)">Send</button>
<div id="list"></div></body></html>`;

async function api(method, route, body) {
  const response = await fetch(relay + route, {
    method,
    headers: { Authorization: `Bearer ${password}`, "X-Relay-Request": "1",
      ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body && JSON.stringify(body),
  });
  return response.json();
}

// Asked in the panel; until the answer is done, each card answered by `decide(card text)` ("allow", "deny" or
// "stop"), and a permission Relay asks for allowed. The cards it showed, and the answer.
async function ask(panel, text, decide) {
  const before = await panel.$$eval(".msg.assistant", (all) => all.length);
  await panel.fill("#input", text);
  await panel.press("#input", "Enter");
  await panel.waitForFunction((n) => document.querySelectorAll(".msg.assistant").length > n, before, LONG);
  const cards = [];
  for (;;) {
    const state = await (await panel.waitForFunction(() => {
      if (document.querySelector("#asks .ask:not(.doing) .btn.primary")) return "card";
      if (!document.querySelector("#send").classList.contains("stop")) return "done";
      return [...document.querySelectorAll(".msg.assistant")].pop()
        ?.querySelector(".note.ask button.primary") ? "ask" : false;
    }, null, LONG)).jsonValue();
    if (state === "done") break;
    if (state === "ask") {
      console.log(`     Relay asks: ${(await panel.textContent(".note.ask .note-title")).trim()}: allowed`);
      await panel.click(".note.ask button.primary");
      await panel.waitForFunction(() => !document.querySelector(".note.ask button.primary:not(:disabled)"), null, LONG);
      continue;
    }
    const element = await panel.$("#asks .ask:not(.doing)");
    const card = (await element.textContent()).replace(/\s+/g, " ").trim();
    const choice = decide(card);
    cards.push({ card, choice });
    console.log(`     card: ${card.slice(0, 160)} → ${choice}`);
    if (choice === "stop") {
      await panel.click("#send");  // (Stop)
      break;
    }
    await (await element.$(choice === "allow" ? ".btn.primary" : ".btn:has-text('Deny')")).click();
    // (that card answered: gone, or doing it; the next step's card may be there already)
    await panel.waitForFunction((el) => !el.isConnected || el.classList.contains("doing"), element, LONG);
  }
  await panel.waitForTimeout(1500);  // (Relay's own copy of it comes after)
  const answer = await panel.evaluate(() => {
    const last = [...document.querySelectorAll(".msg.assistant")].pop();
    return { text: last.querySelector(".md")?.textContent || "",
      errors: [...last.querySelectorAll(".error")].map((e) => e.textContent) };
  });
  return { cards, ...answer };
}

(async () => {
  if (!password) throw new Error("RELAY_APP_PASSWORD isn't set");
  const server = http.createServer((req, res) => {
    res.writeHead(req.url.startsWith("/__page") ? 200 : 404, { "Content-Type": "text/html; charset=utf-8" });
    res.end(req.url.startsWith("/__page") ? PAGE : "");
  }).listen(0, "127.0.0.1");
  await new Promise((r) => server.once("listening", r));
  const { context, worker, id, errors, watch } = await launch();

  // Settings: signed in, the plugin seen, agents allowed to use the browser.
  await new Promise((r) => setTimeout(r, 500));
  const options = context.pages().find((p) => p.url().includes("options.html")) || await context.newPage();
  if (!options.url().includes("options.html")) await options.goto(`chrome-extension://${id}/options.html`);
  watch(options, "options");
  await options.fill("#url", relay);
  await options.fill("#password", password);
  await options.click("#connect");
  await options.waitForFunction(() =>
    /\b(ok|bad)\b/.test(document.querySelector("#status").className), null, { timeout: 30000 });
  const status = await options.textContent("#status");
  check(status.includes("signed in as"), `connected to the real Relay: ${status}`);
  if (!status.includes("signed in as")) process.exit(finish(errors));
  await options.waitForFunction(() =>
    document.querySelector("#plugin").textContent.length > 0, null, { timeout: 30000 });
  check((await options.textContent("#plugin")).includes(`chrome plugin ${pluginVersion} is there`),
    `the settings see the real plugin: ${await options.textContent("#plugin")}`);
  check(await options.$eval("#agents", (c) => c.checked), "agents may use this browser");
  await options.waitForFunction(() => document.querySelectorAll("#provider option").length > 1);
  await options.selectOption("#provider", provider);
  await options.waitForFunction((m) => !m
    || [...document.querySelectorAll("#model option")].some((o) => o.value === m), model, { timeout: 60000 });
  await options.selectOption("#model", model);
  await options.click("#save");

  const site = await context.newPage();
  await site.goto(`http://127.0.0.1:${server.address().port}/__page-trial`);
  const panel = await context.newPage();
  watch(panel, "panel");
  await panel.goto(`chrome-extension://${id}/sidepanel.html`);
  await panel.waitForSelector(".empty", { timeout: 30000 });
  await panel.waitForFunction((m) => document.querySelector("#model").value === m, model, { timeout: 60000 });
  let heard = false;
  for (let i = 0; i < 20 && !heard; i++) {
    heard = (await api("GET", "/api/plugins/chrome/status")).connected === true;
    if (!heard) await panel.waitForTimeout(500);
  }
  check(heard, "the plugin hears the panel");

  // Find the tab, read it, type and click: each step allowed on its card.
  let answer = await ask(panel, "In my Chrome there's a tab titled “Relay Chrome trial”. Use your "
    + "chrome tools, one step at a time: find that tab with chrome_tabs, read it with chrome_read, type "
    + "exactly “Hello from the agent” into its Message field with chrome_type (don't submit), then "
    + "click its Send button with chrome_click. Then answer with just the secret word that page shows.", () => "allow");
  const said = answer.cards.map((c) => c.card).join("\n");
  check(said.includes("Relay Chrome trial"), "the cards name the page");
  check(answer.cards.some((c) => c.card.includes("type into") && c.card.includes("Hello from the agent")),
    "the typing card shows the exact text");
  check(answer.cards.some((c) => c.card.includes("click") && c.card.includes("“Send”")),
    "the click card names the button");
  check(answer.text.includes(WORD), `the agent read the page: “${answer.text.trim()}”`);
  check((await site.textContent("#list")).includes("Sent: Hello from the agent"), "and typed and clicked in it");
  check(answer.errors.length === 0, `no error in the answer${answer.errors.length ? `: ${answer.errors}` : ""}`);
  await panel.screenshot({ path: path.join(shots, "real-agents.png") });
  const { current } = await worker.evaluate(() => chrome.storage.local.get("current"));
  console.log(`     the chat: ${relay}/c/${current}`);

  // A click denied: nothing happens, and the agent hears it.
  const sent = await site.$$eval("#list p", (ps) => ps.length);
  answer = await ask(panel,
    "Click that page's Send button once more with chrome_click, then tell me in one sentence whether it worked.",
    (card) => card.includes("click") ? "deny" : "allow");
  check(answer.cards.some((c) => c.choice === "deny"), "a click asked, and denied");
  check(await site.$$eval("#list p", (ps) => ps.length) === sent, "nothing happened on the page");
  check(/allow|denied|declin|permission|not (?:been )?(?:allowed|permitted)|didn't|did not/i.test(answer.text),
    `the agent says so: “${answer.text.trim()}”`);

  // A screenshot, kept in the chat's folder, and the agent saw it.
  answer = await ask(panel, "Take a screenshot of that tab with chrome_screenshot, "
    + "open the file it saves, and answer with just the words of the page's big heading.", () => "allow");
  check(answer.cards.some((c) => c.card.toLowerCase().includes("screenshot")), "a screenshot asked");
  check(/orange\s+lighthouse/i.test(answer.text), `the agent saw it: “${answer.text.trim()}”`);
  const conv = await api("GET", `/api/conversations/${current}`);
  const folder = path.join(conv.directory || "", ".relay-chrome");
  const kept = fs.existsSync(folder) ? fs.readdirSync(folder).filter((f) => f.startsWith("screenshot-")) : [];
  check(kept.length > 0, `kept in the chat's folder: ${folder} (${kept.join(", ")})`);

  // Stopped while a step waits: its card goes with the run.
  answer = await ask(panel, "Read that tab once more with chrome_read and tell me its title.", () => "stop");
  check(answer.cards.length === 1 && answer.cards[0].choice === "stop", "a step waiting, the run stopped");
  const gone =
    await panel.waitForFunction(() => !document.querySelector("#asks .ask"), null,
      { timeout: 30000 }).then(() => true, () => false);
  check(gone, "its card went with the run");

  // The side panel closed: a step is a notification, allowed in the window it opens, and the agent goes on.
  await panel.close();
  const sentAt = (await api("GET", `/api/conversations/${current}`)).messages.length;
  await api("POST", `/api/conversations/${current}/messages`, {
    text: "Read that tab once more with chrome_read and answer with just the words of its big heading." });
  let notes = [];
  for (let i = 0; i < 240 && !notes.length; i++) {
    notes = Object.keys(await worker.evaluate(() => chrome.notifications.getAll()));
    if (!notes.length) await new Promise((r) => setTimeout(r, 500));
  }
  check(notes.length === 1, `the panel closed: a notification (${notes})`);
  const approve = await context.newPage();
  watch(approve, "approve");
  await approve.goto(`chrome-extension://${id}/approve.html#${notes[0]}`);
  await approve.waitForSelector("#asks .ask:not(.doing) .btn.primary", { timeout: 30000 });
  console.log(`     card: ${(await approve.textContent("#asks .ask")).replace(/\s+/g, " ").slice(0, 120)} → allow`);
  await approve.screenshot({ path: path.join(shots, "real-approve.png") });
  await approve.click("#asks .ask:not(.doing) .btn.primary");
  let last = null;
  for (let i = 0; i < 240; i++) {
    const messages = (await api("GET", `/api/conversations/${current}`)).messages.slice(sentAt);
    last = messages.filter((m) => m.role === "assistant").at(-1);
    if (last && !["running", "queued"].includes(last.status)) break;
    await new Promise((r) => setTimeout(r, 1000));
  }
  const heading = (last?.content || "").trim();
  check(last?.status === "succeeded" && /orange\s+lighthouse/i.test(heading),
    `allowed in its window: the agent read it (“${heading}”)`);

  // What Relay kept: the tools' calls, with the plugin's answers.
  const after = await api("GET", `/api/conversations/${current}`);
  const events = after.messages.filter((m) => m.role === "assistant").flatMap((m) => m.events || []);
  const used = [...new Set(events.map((e) => e.name || e.tool || "").filter((n) => n.includes("chrome_")))];
  console.log(`     Relay kept calls of: ${used.join(", ")}`);
  check(["tabs", "read", "type", "click", "screenshot"].every((t) => used.some((n) => n.includes(`chrome_${t}`))),
    "Relay kept the tool calls of each step");
  check(after.messages.filter((m) => m.role === "assistant").at(-2)?.status === "interrupted",
    "the stopped answer is interrupted");
  console.log(`     cost: $${after.messages.filter((m) => m.role === "assistant").reduce((sum, m) =>
    sum + Number([...(m.events || [])].reverse().find((e) => e.type === "usage")?.cost || 0), 0).toFixed(4)}`);

  const code = finish(errors);
  await context.close();
  server.close();
  process.exit(code);
})().catch((error) => {
  console.error(error);
  process.exit(2);
});
