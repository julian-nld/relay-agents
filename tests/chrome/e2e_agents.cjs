// Relay's agents in this browser, against mock_relay.py with Relay's chrome plugin loaded: a tool called as an agent
// calls it (POST /__tool) comes to the panel, is allowed there (or not), done in the tab, and answered.
//
//   python3 mock_relay.py 8421 &   node e2e_agents.cjs 8421

const fs = require("fs");
const os = require("os");
const path = require("path");
const { launch, check, finish, pluginVersion } = require("./harness.cjs");

const port = Number(process.argv[2] || 8421);
const relay = `http://127.0.0.1:${port}`;
const { shots } = require("./harness.cjs");
const AUTH = { Authorization: "Bearer relay_test", "X-Relay-Request": "1", "Content-Type": "application/json" };

const post = async (route, body) =>
  (await fetch(relay + route, { method: "POST", headers: AUTH, body: JSON.stringify(body) })).json();
const log = async () => (await fetch(`${relay}/__log`)).json();
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  fs.mkdirSync(shots, { recursive: true });
  await fetch(`${relay}/__reset`);
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "relay-chat-"));
  const launched = await launch();
  const { context, id, errors, watch } = launched;
  let { worker } = launched;  // (another one once Chrome stopped it and it started again)

  // Connected, and a chat whose agent uses the browser.
  await wait(500);
  const options = context.pages().find((p) => p.url().includes("options.html")) || await context.newPage();
  if (!options.url().includes("options.html")) await options.goto(`chrome-extension://${id}/options.html`);
  watch(options, "options");
  await options.fill("#url", `127.0.0.1:${port}`);
  await options.fill("#password", "relay_test");
  await options.click("#connect");
  await options.waitForFunction(() => document.querySelector("#plugin").textContent.length > 0);
  check((await options.textContent("#plugin")).includes(`chrome plugin ${pluginVersion} is there`),
    `the settings see the plugin: ${await options.textContent("#plugin")}`);
  check(await options.$eval("#agents", (c) => c.checked), "agents may use the browser: on by default");
  const chat = await post("/api/conversations", { provider_id: "claude-code", title: "Marktplaats helper" });

  // (as an agent of that chat calls a tool: its answer once there is one)
  const tool = (name, args = {}, run = "r1") => post("/__tool", { name: `chrome_${name}`, arguments: args,
    conversation_id: chat.id, run_id: run, directory: folder });

  const site = await context.newPage();
  await site.goto(`${relay}/__page/form`);
  const panel = await context.newPage();
  watch(panel, "panel");
  await panel.goto(`chrome-extension://${id}/sidepanel.html`);
  await panel.waitForSelector(".empty");
  await wait(300);

  // Reading: asked once, allowed; then read again in that answer without asking.
  let call = tool("read");
  await panel.waitForSelector(".ask:not(.doing)");
  const card = await panel.textContent(".ask");
  check(card.includes("Marktplaats helper") && card.includes("wants to:") && card.includes("read “The Form”"),
    `the card says who and what: “${card}”`);
  check(card.includes(`Always on 127.0.0.1:${port}`), "with Deny, Allow and Always on that site");
  check(await worker.evaluate(() => chrome.action.getBadgeText({})) === "1",
    "the toolbar button says something's asked");
  await panel.screenshot({ path: path.join(shots, "panel-agent-ask.png") });
  await panel.click(".ask .btn.primary");
  let answer = await call;
  const page = answer.text;
  check(answer.kind === "ok" && /^Tab \d+: “The Form” · http/.test(page),
    `allowed: the agent reads the page (${page.split("\n")[0]})`);
  check(page.includes("# Messages") && page.includes("Buyer: Is it still for sale?"), "its text, headings marked");
  const ref = (pattern) => Number((page.match(pattern) || [])[1]);
  const message = ref(/\[(\d+) textbox “Message”\]/);
  const send = ref(/\[(\d+) button “Send”\]/);
  const search = ref(/\[(\d+) textbox “Search”\]/);
  const size = ref(/\[(\d+) select “Size”: “Small” \(options: Small \| Large\)\]/);
  const agree = ref(/\[(\d+) checkbox “I agree” ☐\]/);
  const link = ref(/\[(\d+) link “The test page” → \/__page\]/);
  check([message, send, search, size, agree, link].every((n) => n > 0),
    `what it can use, numbered: ${[message, send, search, size, agree, link]}`);
  check(!page.includes("Hidden text"), "not what's hidden");
  const tabId = Number(page.match(/^Tab (\d+)/)[1]);
  const gone =
    await panel.waitForFunction(() => !document.querySelector(".ask"), null,
      { timeout: 5000 }).then(() => true, () => false);
  check(gone && await worker.evaluate(() => chrome.action.getBadgeText({})) === "", "the card goes once it's done");
  answer = await tool("read", { offset: 20 });
  check(answer.text.includes("(from character 20 of") && !(await panel.$(".ask:not(.doing)")),
    "read again in that answer: not asked again");

  // Typing: asked, with what's typed.
  call = tool("type", { ref: message, text: "Hi, 35 euros is fine.\nRegards" });
  await panel.waitForSelector(".ask:not(.doing)");
  const typing = await panel.textContent(".ask");
  check(typing.includes(`type into [${message} textbox “Message”] in “The Form”`)
    && typing.includes("Hi, 35 euros is fine."),
    `typing: asked, with the text: “${typing}”`);
  await panel.click(".ask .btn.primary");
  answer = await call;
  check(answer.text
    === `Typed “Hi, 35 euros is fine. Regards” into [${message} textbox “Message”].`, `typed: ${answer.text}`);
  check(await site.$eval("#message", (t) => t.value) === "Hi, 35 euros is fine.\nRegards", "it's in the page's field");

  // Clicking: denied, then always allowed on the site.
  call = tool("click", { ref: send });
  await panel.waitForSelector(".ask:not(.doing)");
  check((await panel.textContent(".ask")).includes(`click [${send} button “Send”] in “The Form”`),
    "a click: asked, with what it clicks");
  await panel.click(".ask .btn:has-text('Deny')");
  answer = await call;
  check(answer.kind === "ok" && answer.text.includes("didn't allow"), `denied: the agent hears it (${answer.text})`);
  check(!(await site.textContent("#list")).includes("Sent:"), "and nothing happened");
  call = tool("click", { ref: send });
  await panel.waitForSelector(".ask:not(.doing)");
  await panel.click(`.ask .btn:has-text('Always on')`);
  answer = await call;
  check(answer.text.startsWith(`Clicked [${send} button “Send”]. The tab is still at “The Form”`),
    `clicked: ${answer.text}`);
  check((await site.textContent("#list")).includes("Sent: Hi, 35 euros is fine."), "the page did it");
  answer = await tool("click", { ref: send }, "r2");
  check(answer.text.startsWith("Clicked") && (await site.$$eval("#list p", (ps) => ps.length)) === 3,
    "always on that site: another answer's click isn't asked");

  // The rest: Enter in a form, a list, a checkbox, scrolling, a link, back.
  answer = await tool("type", { ref: search, text: "zebra", submit: true });
  check(answer.text.includes("and pressed Enter") && (await site.textContent("#list")).includes("Searched: zebra"),
    "typed with submit: the form is sent");
  answer = await tool("type", { ref: size, text: "large" });
  check(await site.$eval("#size", (s) => s.value) === "Large", `a list set to an option: ${answer.text}`);
  answer = await tool("click", { ref: agree });
  check(await site.$eval("#agree", (c) => c.checked), "a checkbox clicked");
  answer = await tool("type", { ref: agree, text: "x" });
  check(answer.kind === "error" && answer.text.includes("isn't a field to type in"),
    `typing in a checkbox: ${answer.text}`);
  answer = await tool("scroll", { direction: "bottom" });
  check(answer.text.includes("100% of the way down")
    && await site.evaluate(() => scrollY > 1000), `scrolled: ${answer.text}`);
  answer = await tool("click", { ref: 9999 });
  check(answer.kind === "error" && answer.text.includes("no element [9999]"),
    `a number that isn't there: ${answer.text}`);
  answer = await tool("click", { ref: link });
  check(answer.text.includes("The tab went to “The Test Page”"), `a link followed: ${answer.text}`);
  answer = await tool("open", { url: "back", tab: tabId });
  check(answer.text.includes("“The Form”") && answer.text.includes("Buyer: Is it still for sale?"), "back, and read");

  // A screenshot, kept in the chat's folder.
  answer = await tool("screenshot", { tab: tabId });
  const shot = (answer.text.match(/\.relay-chrome\/(screenshot-[\w-]+\.jpg)/) || [])[1];
  const file = shot && path.join(folder, ".relay-chrome", shot);
  check(Boolean(file && fs.existsSync(file))
    && fs.readFileSync(file).subarray(0, 3).equals(Buffer.from([0xff, 0xd8, 0xff])),
    `a screenshot, as a JPEG in the chat's folder: ${answer.text.split("\n")[0]}`);

  // Opening a page: a new tab, read; the agent goes on in it.
  answer = await tool("open", { url: `${relay}/__page` });
  const opened = Number((answer.text.match(/^Tab (\d+)/) || [])[1]);
  check(opened && opened !== tabId && answer.text.includes("Testing extensions"),
    `opened in a new tab, and read (tab ${opened})`);
  answer = await tool("read");
  check(answer.text.startsWith(`Tab ${opened}:`), "then the agent's tab is that one");
  answer = await tool("open", { url: "javascript:alert(1)" });
  check(answer.kind === "error" && answer.text.includes("Only http and https"), `no other addresses: ${answer.text}`);
  answer = await tool("read", { tab: 999999 });
  check(answer.kind === "error" && answer.text.includes("no tab 999999"), `a tab that isn't there: ${answer.text}`);

  // A page's harder parts: an editor, a div that's a button, display: contents, a whole card as a link, a password.
  answer = await tool("open", { url: `${relay}/__page/tricky` });
  const tricky = answer.text;
  const editor = Number((tricky.match(/\[(\d+) textbox “Editor”: “old”\]/) || [])[1]);
  const press = Number((tricky.match(/\[(\d+) button “Press”\]/) || [])[1]);
  check(editor > 0 && press > 0, "an editor (contenteditable) and a div with role button");
  check(tricky.includes("In display contents"), "what's in a display: contents element");
  check(/\[\d+ link → \/item\/1\]\s*\n### A whole card as a link\n/.test(tricky)
    && tricky.includes("Lots of words about it."),
    "a whole card as a link: marked, its text read as text");
  check(tricky.includes("textbox “Password” type=password: “•••”")
    && !tricky.includes("secret"), "a password isn't read");
  await tool("type", { ref: editor, text: "new text" });
  await tool("click", { ref: press });
  answer = await tool("read");
  check(answer.text.includes("textbox “Editor”: “new text”") && answer.text.includes("pressed"),
    "typed in the editor, the div pressed");

  // The tabs: asked (they say what you have open).
  call = tool("tabs");
  await panel.waitForSelector(".ask:not(.doing)");
  check((await panel.textContent(".ask")).includes("see which tabs you have open"), "the tabs: asked");
  await panel.click(".ask .btn.primary");
  answer = await call;
  check(answer.text.includes(`${tabId} · “The Form”`) && !answer.text.includes("chrome-extension://"),
    `listed, not the extension's own: ${answer.text.replace(/\n/g, " / ")}`);

  // Another site, asked; the answer ends meanwhile: the card goes, the tool hears it.
  const other = await context.newPage();
  await other.goto(`http://localhost:${port}/__page/form`);
  const otherTab = Number((await tool("tabs")).text.match(/(\d+) · “The Form” · http:\/\/localhost/)[1]);
  call = tool("read", { tab: otherTab }, "r5");
  await panel.waitForSelector(".ask:not(.doing)");
  check((await panel.textContent(".ask")).includes("read “The Form”"), "another site: asked again");
  await post("/__run_ended", { run_id: "r5" });
  answer = await call;
  check(answer.text.includes("Cancelled"), `the answer ended: ${answer.text}`);
  await panel.waitForFunction(() => !document.querySelector(".ask"), null, { timeout: 8000 });
  check(true, "and its card went");

  // Opening an address of a site that's always allowed, in a tab of one that isn't: asked (it leaves that site).
  call = tool("open", { url: `${relay}/__page`, tab: otherTab }, "r7");
  await panel.waitForSelector(".ask:not(.doing)");
  check((await panel.textContent(".ask")).includes(`open ${relay}/__page in “The Form” (localhost:${port})`),
    "opening in a tab of a site that isn't always allowed: asked");
  await panel.click(".ask .btn:has-text('Deny')");
  await call;

  // Two panels (two windows): both ask; allowed in one, done once, gone from the other.
  const second = await context.newPage();
  watch(second, "second panel");
  await second.goto(`chrome-extension://${id}/sidepanel.html`);
  await second.waitForSelector(".empty");
  await wait(500);
  call = tool("read", { tab: otherTab }, "r6");
  await panel.waitForSelector(".ask:not(.doing)");
  await second.waitForSelector(".ask:not(.doing)");
  check(true, "two panels: both ask");
  const before = (await log()).filter((r) => r.path.endsWith("/claim")).length;
  await second.click(".ask .btn.primary");
  answer = await call;
  await panel.waitForFunction(() => !document.querySelector(".ask"), null, { timeout: 8000 });
  const claims = (await log()).filter((r) => r.path.endsWith("/claim")).length - before;
  check(answer.text.startsWith(`Tab ${otherTab}:`),
    `done in the one where it was allowed: ${answer.text.split("\n")[0]}`);
  check(claims === 1, `once (${claims} claim), and gone from the other`);
  await second.close();

  // The options list what's always allowed, and take it back.
  await options.reload();
  await options.waitForSelector(".site-row");
  check((await options.textContent("#sites")).includes(`http://127.0.0.1:${port}`)
    && (await options.textContent("#sites")).includes("reading and using it"),
    "the settings list the site that's always allowed");
  await options.click(".site-row .btn");
  await options.waitForFunction(() => document.querySelector("#sites").textContent.includes("Nothing yet"));
  call = tool("click", { ref: send, tab: tabId }, "r8");
  await panel.waitForSelector(".ask:not(.doing)");
  check(true, "taken back: asked again");
  await panel.click(".ask .btn:has-text('Deny')");
  await call;
  await options.screenshot({ path: path.join(shots, "options-agents.png"), fullPage: true });

  // Without a panel: the service worker still asks for commands. A step that needs you is a notification (and the
  // toolbar's badge); the window it opens (approve.html) has the card, and closes once you answered.
  await panel.close();
  await wait(1000);
  call = tool("read", { tab: otherTab }, "r9");
  let notes = [];
  for (let i = 0; i < 50 && !notes.length; i++) {
    notes = Object.keys(await worker.evaluate(() => chrome.notifications.getAll()));
    if (!notes.length) await wait(200);
  }
  check(notes.length === 1, `the panel closed: a notification (${notes})`);
  check(await worker.evaluate(() => chrome.action.getBadgeText({})) === "1", "and the badge");
  const approve = await context.newPage();
  watch(approve, "approve");
  await approve.goto(`chrome-extension://${id}/approve.html#${notes[0]}`);
  await approve.waitForSelector(".ask:not(.doing)");
  check((await approve.textContent(".ask")).includes("read “The Form”"),
    `its window has the card: ${await approve.textContent(".ask-what")}`);
  check(Object.keys(await worker.evaluate(() => chrome.notifications.getAll())).length === 0,
    "the notification goes when the card shows");
  await approve.screenshot({ path: path.join(shots, "approve.png") });
  await approve.click(".ask .btn.primary");
  answer = await call;
  check(answer.kind === "ok" && answer.text.startsWith(`Tab ${otherTab}:`),
    `allowed there: done (${answer.text.split("\n")[0]})`);
  const closed = await approve.waitForEvent("close", { timeout: 5000 }).then(() => true, () => approve.isClosed());
  check(closed, "and the window closed itself");

  // Chrome stops the service worker (as when it's idle): the alarm starts it again, and it asks for commands as before.
  await worker.evaluate(() => {
    self.before = true;  // (gone with it)
    return chrome.alarms.create("relay-agents", { when: Date.now() + 5000 });
  });
  const cdp = await context.newCDPSession(await context.newPage());
  const workers = async () =>
    (await cdp.send("Target.getTargets")).targetInfos.filter((t) => t.type === "service_worker");
  for (const target of await workers()) await cdp.send("Target.closeTarget", { targetId: target.targetId });
  let stopped = false;
  for (let i = 0; i < 30 && !stopped; i++) {
    stopped = (await workers()).length === 0;
    if (!stopped) await wait(100);
  }
  check(stopped, "the service worker stopped");
  let back = false;
  for (let i = 0; i < 40 && !back; i++) {
    await wait(500);
    back = (await workers()).length === 1
      && (await (await fetch(`${relay}/api/plugins/chrome/status`, { headers: AUTH })).json()).connected;
  }
  worker = context.serviceWorkers()[0];
  check(back && await worker.evaluate(() => self.before === undefined),
    "the alarm started it again, and it's connected");
  check(Boolean(await worker.evaluate(() => chrome.alarms.get("relay-agents"))), "its alarm is there again");

  // With the setting off, the agent hears the browser isn't there.
  await post("/__plugin", { GONE: 2 });
  const again = await context.newPage();
  watch(again, "panel again");
  await again.goto(`chrome-extension://${id}/sidepanel.html`);
  await again.waitForSelector(".empty");
  await options.uncheck("#agents");
  await wait(3000);
  answer = await tool("read");
  check(answer.text.includes("isn't connected"), "the setting off: not there");

  // A Relay without the plugin (or with it off): the settings say so, the panel doesn't ask it for commands.
  await options.check("#agents");
  await post("/__plugin", { off: true });
  await options.reload();
  await options.waitForFunction(() => document.querySelector("#plugin").textContent.length > 0);
  check((await options.textContent("#plugin")).includes("doesn't have its chrome plugin"),
    `without the plugin: ${await options.textContent("#plugin")}`);
  await again.reload();
  await again.waitForSelector(".empty");
  await wait(2500);
  const asked = (await log()).length;
  await wait(2500);
  check(!(await log()).slice(asked).some((r) => r.path.includes("/api/plugins/chrome/")),
    "and the extension doesn't ask it for commands");

  const code = finish(errors);
  await context.close();
  process.exit(code);
})().catch((error) => {
  console.error(error);
  process.exit(2);
});
