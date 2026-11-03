// The extension in Chromium against a real Relay and a real agent: settings, a page and a screenshot going along
// (and the agent seeing them), the queue, Stop, reopening, the history, and what Relay kept of it all.
// It makes one chat in Relay, which stays there.
//
//   RELAY_URL=http://127.0.0.1:8420 RELAY_APP_PASSWORD=relay_… node real.cjs [provider] [model]

const http = require("http");
const path = require("path");
const { launch, check, finish } = require("./harness.cjs");

const relay = (process.env.RELAY_URL || "http://127.0.0.1:8420").replace(/\/+$/, "");
const password = process.env.RELAY_APP_PASSWORD;
const provider = process.argv[2] || "claude-code";
const model = process.argv[3] ?? "haiku";
const { shots } = require("./harness.cjs");
const LONG = { timeout: 3 * 60000 };

// What only this page says, so an answer shows the agent read it (or saw it).
const PAGE = `<!doctype html><html><head><title>Lighthouse Keeping Weekly</title></head><body>
<main><h1 style="font-size:64px">ZEBRA PARADE</h1>
<p>This week's keeper of the month is Marit Oosterhuis, `
  + `who has kept the Westhoofd light for 31 years.</p></main></body></html>`;

async function api(method, route, body) {
  const response = await fetch(relay + route, {
    method,
    headers: { Authorization: `Bearer ${password}`, "X-Relay-Request": "1",
      ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body && JSON.stringify(body),
  });
  return response.json();
}

// Asked in the panel; the answer once it's done: its text, its errors, its footer.
async function ask(panel, text) {
  const before = await panel.$$eval(".msg.assistant", (all) => all.length);
  await panel.fill("#input", text);
  await panel.press("#input", "Enter");
  await panel.waitForFunction((n) => document.querySelectorAll(".msg.assistant").length > n, before, LONG);
  return done(panel);
}

// Until the answer is done; a permission it asks meanwhile is allowed with the panel's own button.
async function done(panel) {
  for (;;) {
    const state = await panel.waitForFunction(() => {
      if (!document.querySelector("#send").classList.contains("stop")) return "done";
      return [...document.querySelectorAll(".msg.assistant")].pop()
        ?.querySelector(".note.ask button.primary") ? "ask" : false;
    }, null, LONG);
    if ((await state.jsonValue()) === "done") break;
    console.log(`     it asks: ${(await panel.textContent(".note.ask .note-title")).trim()}: allowed in the panel`);
    await panel.click(".note.ask button.primary");
    await panel.waitForFunction(() => !document.querySelector(".note.ask button.primary:not(:disabled)"), null, LONG);
  }
  await panel.waitForTimeout(1500);  // (Relay's own copy of it comes after: the footer)
  return panel.evaluate(() => {
    const answer = [...document.querySelectorAll(".msg.assistant")].pop();
    return { text: answer.querySelector(".md")?.textContent || "", all: answer.textContent,
      errors: [...answer.querySelectorAll(".error")].map((e) => e.textContent),
      foot: answer.querySelector(".foot").textContent };
  });
}

(async () => {
  if (!password) throw new Error("RELAY_APP_PASSWORD isn't set");
  const server = http.createServer((req, res) => {
    res.writeHead(req.url.startsWith("/__page") ? 200 : 404, { "Content-Type": "text/html; charset=utf-8" });
    res.end(req.url.startsWith("/__page") ? PAGE : "");
  }).listen(0, "127.0.0.1");
  await new Promise((r) => server.once("listening", r));
  const { context, worker, id, errors, watch } = await launch();

  // Settings.
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
  await options.waitForFunction(() => document.querySelectorAll("#provider option").length > 1);
  await options.selectOption("#provider", provider);
  await options.waitForFunction((m) => !m
    || [...document.querySelectorAll("#model option")].some((o) => o.value === m), model, { timeout: 60000 });
  await options.selectOption("#model", model);
  await options.click("#save");
  await options.screenshot({ path: path.join(shots, "real-options.png"), fullPage: true });

  const site = await context.newPage();
  await site.goto(`http://127.0.0.1:${server.address().port}/__page`);
  const panel = await context.newPage();
  watch(panel, "panel");
  await panel.goto(`chrome-extension://${id}/sidepanel.html`);
  await panel.waitForSelector(".empty", { timeout: 30000 });
  await panel.waitForFunction((m) => document.querySelector("#model").value === m, model, { timeout: 60000 });
  check(await panel.$eval("#provider", (s) => s.value) === provider,
    `a new chat gets ${provider} · ${model || "its default"}`);

  // The page goes along, and the agent read it.
  await panel.click("#add-page");
  await panel.waitForFunction(() => document.querySelectorAll("#contexts .chip").length === 1);
  let answer =
    await ask(panel,
      "Who is this week's keeper of the month, according to the page I sent? Answer with just "
      + "the name. Don't use any tools.");
  check(answer.text.includes("Marit Oosterhuis"), `the agent read the page: “${answer.text.trim()}”`);
  check(answer.errors.length === 0, `no error in the answer${answer.errors.length ? `: ${answer.errors}` : ""}`);
  check(/\d+ s/.test(answer.foot), `its footer: ${answer.foot}`);
  const { current } = await worker.evaluate(() => chrome.storage.local.get("current"));
  console.log(`     the chat: ${relay}/c/${current}`);

  // A screenshot goes along, and the agent saw it.
  await site.bringToFront();  // (Chrome takes the tab you see)
  await panel.click("#add-shot");
  await panel.waitForSelector("#contexts .thumb", { timeout: 10000 }).catch(() => null);
  if (await panel.$("#contexts .thumb")) {
    answer = await ask(panel,
      "Look at the screenshot I attached: what words are in its big heading? Answer with just those words.");
    check(/zebra\s+parade/i.test(answer.text), `the agent saw the screenshot: “${answer.text.trim()}”`);
  } else {
    check(false, `a screenshot is attached: ${await panel.textContent("#notice")}`);
  }

  // Sent while it answers: Relay's queue, and answered next.
  const before = await panel.$$eval(".msg.assistant", (all) => all.length);
  await panel.fill("#input", "Reply with just the word ALPHA, nothing else.");
  await panel.press("#input", "Enter");
  await panel.waitForFunction((n) => document.querySelectorAll(".msg.assistant").length > n, before, LONG);
  await panel.fill("#input", "Reply with just the word BETA, nothing else.");
  await panel.press("#input", "Enter");
  // (shown as next; or, when that answer had just ended, sent right away)
  const queued =
    await panel.waitForFunction(() => document.querySelector("#queue .queued-item")?.textContent.includes("Next:")
      || [...document.querySelectorAll(".msg.user .bubble")].pop()
      ?.textContent.includes("BETA"), null, { timeout: 5000 })
    .then(() => true, () => false);
  check(queued, "sent while it answers: queued");
  await panel.waitForFunction(() =>
    [...document.querySelectorAll(".msg.user .bubble")].pop()?.textContent.includes("BETA")
    && [...document.querySelectorAll(".msg.assistant")].pop().querySelector(".md")
    ?.textContent.includes("BETA"), null, LONG);
  answer = await done(panel);
  const asked = await panel.$$eval(".msg.user .bubble", (all) => all.map((b) => b.textContent).slice(-2));
  check(asked[0].includes("ALPHA") && asked[1].includes("BETA") && answer.text.includes("BETA"),
    `and answered next: “${answer.text.trim()}”`);
  check(!(await panel.$("#queue .queued-item")), "and gone from the queue");

  // Stopped as soon as it started, two queued meanwhile: one taken back, one that waits after the Stop (as Relay
  // has it) and goes with Send now.
  const counted = await panel.$$eval(".msg.assistant", (all) => all.length);
  await panel.fill("#input", "Write a story of 2000 words about a lighthouse keeper. Don't use any tools.");
  await panel.press("#input", "Enter");
  await panel.waitForFunction((n) => document.querySelectorAll(".msg.assistant").length > n, counted, LONG);
  await panel.fill("#input", "Reply with just the word NEVER, nothing else.");
  await panel.press("#input", "Enter");
  await panel.waitForSelector("#queue .queued-item", { timeout: 10000 });
  await panel.click("#queue .chip-x");
  await panel.waitForFunction(() => !document.querySelector("#queue .queued-item"), null, { timeout: 10000 });
  await panel.fill("#input", "Reply with just the word GAMMA, nothing else.");
  await panel.press("#input", "Enter");
  await panel.waitForSelector("#queue .queued-item", { timeout: 10000 });
  const waiting = (await api("GET", `/api/conversations/${current}`)).queued || [];
  check(waiting.length === 1 && waiting[0].content?.includes("GAMMA"),
    `one taken back: gone from Relay's queue (${JSON.stringify(waiting.map((q) => q.content))})`);
  await panel.click("#send");  // (Stop)
  answer = await done(panel);
  check(answer.all.includes("Stopped."), `stopped (${answer.text.length} characters of it came)`);
  check(!(await panel.textContent("#thread")).includes("null"), "nothing in the thread says null");
  const held = await panel.waitForFunction(() => document.querySelector("#queue").textContent.startsWith("Waiting:")
    && document.querySelector("#queue button.btn"), null, { timeout: 15000 }).then(() => true, () => false);
  check(held, `the queued one waits after the Stop, with Send now: “${await panel.textContent("#queue")}”`);
  await panel.waitForTimeout(3000);
  check(!(await panel.textContent("#thread")).includes("GAMMA"), "and isn't sent by itself");
  await panel.screenshot({ path: path.join(shots, "real-panel.png") });
  if (held) {
    const before = await panel.$$eval(".msg.assistant", (all) => all.length);
    await panel.click("#queue button.btn");
    await panel.waitForFunction((n) => document.querySelectorAll(".msg.assistant").length > n, before, LONG);
    answer = await done(panel);
    check(answer.text.includes("GAMMA") && !(await panel.$("#queue .queued-item")),
      `Send now: answered “${answer.text.trim()}”`);
  }

  // Opened again: the same chat, as Relay kept it; the history has it.
  await panel.reload();
  await panel.waitForFunction(() => document.querySelectorAll(".msg.assistant").length >= 5, null, { timeout: 30000 });
  const conv = await api("GET", `/api/conversations/${current}`);
  check((await panel.textContent("#title-text")) === (conv.title || "Untitled"), `reopened: “${conv.title}”`);
  check((await panel.textContent(".msg.user .chips")).includes("Lighthouse Keeping Weekly"),
    "the page shows as a chip in the stored question");
  await panel.click("#title");
  await panel.waitForSelector(".history-row.current", { timeout: 30000 });
  check(true, "the history lists it, marked");
  await panel.click("#title");

  // What Relay kept.
  const users = conv.messages.filter((m) => m.role === "user");
  const answers = conv.messages.filter((m) => m.role === "assistant");
  check(users[0]?.content.includes('<page title="Lighthouse Keeping Weekly"'), "Relay has the page in its tag");
  check((users[1]?.attachments || []).length === 1,
    `and the screenshot as an attachment: ${JSON.stringify(users[1]?.attachments)}`);
  check(answers.at(-2)?.status === "interrupted", `the stopped answer is “${answers.at(-2)?.status}”`);
  check(!users.some((m) => m.content.includes("NEVER")) && users.at(-1)?.content.includes("GAMMA"),
    "the one taken back never went; the one sent with Send now did");
  check(conv.provider_id === provider && (conv.model || "") === model,
    `the chat's agent: ${conv.provider_id} · ${conv.model}`);
  check(conv.title?.startsWith("Who is this week's keeper") && !/From my browser|</.test(conv.title),
    `named by the question alone: “${conv.title}”`);
  const row = (await api("GET", "/api/conversations")).find((r) => r.id === current);
  console.log(`     Relay's list says: external ${JSON.stringify(row?.external)}, `
    + `started_by ${JSON.stringify(conv.started_by ?? row?.started_by)}`);
  console.log(`     cost: $${answers.reduce((sum, m) =>
    sum + Number([...(m.events || [])].reverse().find((e) => e.type === "usage")?.cost || 0), 0).toFixed(4)}`);

  const code = finish(errors);
  await context.close();
  server.close();
  process.exit(code);
})().catch((error) => {
  console.error(error);
  process.exit(2);
});
