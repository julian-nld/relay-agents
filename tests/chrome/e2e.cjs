// The extension in Chromium against mock_relay.py: settings, the side panel's chats, permissions, stopping,
// screenshots, history, what the right-click menu leaves, and the setup screen.
//
//   python3 mock_relay.py 8421 &   node e2e.cjs 8421

const fs = require("fs");
const path = require("path");
const { launch, check, finish } = require("./harness.cjs");

const port = Number(process.argv[2] || 8421);
const relay = `http://127.0.0.1:${port}`;
const { shots } = require("./harness.cjs");

const log = async () => (await fetch(`${relay}/__log`)).json();

(async () => {
  fs.mkdirSync(shots, { recursive: true });
  await fetch(`${relay}/__reset`);
  const { context, worker, id, errors, watch } = await launch();

  // Settings: a wrong app password, then the right one; the agent for new chats.
  await new Promise((r) => setTimeout(r, 500));
  const opened = context.pages().find((p) => p.url().includes("options.html"));
  check(Boolean(opened), "the settings open by themselves at installation");
  const options = opened || await context.newPage();
  if (!opened) await options.goto(`chrome-extension://${id}/options.html`);
  watch(options, "options");
  await options.fill("#url", `127.0.0.1:${port}`);
  await options.fill("#password", "relay_wrong");
  await options.click("#connect");
  await options.waitForFunction(() => document.querySelector("#status").classList.contains("bad"));
  check((await options.textContent("#status")).includes("app password"),
    `a wrong app password is said: ${await options.textContent("#status")}`);
  await options.fill("#password", "relay_test");
  await options.click("#connect");
  await options.waitForFunction(() => document.querySelector("#status").classList.contains("ok"));
  check((await options.textContent("#status")).includes("signed in as alex"),
    `connected: ${await options.textContent("#status")}`);
  await options.waitForFunction(() => document.querySelectorAll("#provider option").length > 1);
  const providers = await options.$$eval("#provider option", (os) => os.map((o) => o.value));
  check(JSON.stringify(providers) === JSON.stringify(["", "claude-code", "opencode"]),
    `providers that are on: ${providers}`);
  await options.selectOption("#provider", "opencode");
  await options.waitForFunction(() =>
    [...document.querySelectorAll("#model option")].some((o) => o.value === "opencode/big-pickle"));
  await options.selectOption("#model", "opencode/big-pickle");
  await options.click("#save");
  await options.screenshot({ path: path.join(shots, "options.png"), fullPage: true });

  // A page to read, with a selection.
  const site = await context.newPage();
  await site.goto(`${relay}/__page`);
  await site.evaluate(() => {
    const range = document.createRange();
    range.selectNodeContents(document.getElementById("p"));
    getSelection().addRange(range);
  });

  const panel = await context.newPage();
  watch(panel, "panel");
  await panel.goto(`chrome-extension://${id}/sidepanel.html`);
  await panel.waitForSelector(".empty");
  await panel.waitForFunction(() => document.querySelector("#model").value === "opencode/big-pickle");
  check(await panel.$eval("#provider", (s) => s.value) === "opencode", "a new chat gets the agent from the settings");
  await panel.screenshot({ path: path.join(shots, "panel-empty.png") });

  // The page and its selection go along.
  await panel.click("#add-page");
  await panel.waitForFunction(() => document.querySelectorAll("#contexts .chip").length === 2);
  check((await panel.textContent("#contexts")).includes("The Test Page"), "the page is attached");
  check((await panel.textContent("#contexts")).includes("Chrome extensions are tested"), "and its selection");
  await panel.fill("#input", "What is this page about?");
  await panel.screenshot({ path: path.join(shots, "panel-attached.png") });
  await panel.press("#input", "Enter");
  await panel.waitForFunction(() => document.querySelector(".msg.assistant .foot")?.textContent.includes("$0.0123"));
  const answer = await panel.textContent(".msg.assistant .md");
  check(answer.includes("You asked: What is this page about?") && answer.includes("and a page"),
    "the answer came, with the page");
  check(await panel.$eval(".msg.assistant .md strong", (e) => e.textContent)
    === "What is this page about?", "its Markdown is rendered");
  check(Boolean(await panel.$(".msg.assistant .code .copy")), "a code block has its Copy button");
  check((await panel.textContent(".tool")).includes("WebSearch: playwright extensions"), "the tool call is shown");
  check((await panel.textContent(".msg.user .bubble")) === "What is this page about?",
    "the question shows without the page's text");
  check((await panel.textContent(".msg.user .chips")).includes("The Test Page"), "what went along shows as chips");
  check((await panel.textContent("#title-text")) === "What is this page about?", "the title Relay gave it shows");
  check((await panel.$$("#contexts .chip")).length === 0, "the composer is empty again");

  // The selection, still standing in the tab, doesn't go again with the page; another selection does.
  await panel.click("#add-page");
  await panel.waitForFunction(() => document.querySelectorAll("#contexts .chip").length > 0);
  check((await panel.$$("#contexts .chip")).length === 1
    && !(await panel.textContent("#contexts")).includes("Chrome extensions"),
    "a selection that went along already doesn't go again with the page");
  await panel.click("#contexts .chip-x");
  await site.evaluate(() => {
    const range = document.createRange();
    range.selectNodeContents(document.querySelector("h1"));
    getSelection().removeAllRanges();
    getSelection().addRange(range);
  });
  await panel.click("#add-page");
  await panel.waitForFunction(() => document.querySelectorAll("#contexts .chip").length > 0);
  check((await panel.$$("#contexts .chip")).length === 2
    && (await panel.textContent("#contexts")).includes("Testing extensions"),
    "another selection does");
  while (await panel.$("#contexts .chip-x")) await panel.click("#contexts .chip-x");

  let requests = await log();
  const made = requests.find((r) => r.method === "POST" && r.path === "/api/conversations");
  check(made?.json?.provider_id === "opencode" && made?.json?.model === "opencode/big-pickle" && made?.json?.mode
    === "chat", "the chat is made with that agent");
  check(made?.json?.title === "What is this page about?", "named by the question, without what went along");
  const sent = requests.find((r) => r.method === "POST" && r.path.endsWith("/messages"));
  check(sent?.json?.text.includes('<page title="The Test Page" url="http://127.0.0.1:')
    && sent?.json?.text.includes("<selection "),
    "the message has the page and the selection in their tags");
  check(sent?.json?.text.includes("A page about testing.")
    && sent?.json?.text.includes("Testing extensions"), "with the page's text");
  check(!sent?.json?.text.includes("Menu"), "the page's main part only (not its menu)");
  // (the test page's own favicon.ico isn't the extension's)
  check(requests.every((r) => r.auth === "Bearer relay_test" || r.path === "/api/state" || r.path === "/favicon.ico"),
    "every call has the app password");
  check(requests.filter((r) => r.method !== "GET").every((r) => r.x_relay === "1"), "every change has X-Relay-Request");
  check(!requests.some((r) => r.method === "OPTIONS"), "no CORS preflight (the host permission covers it)");

  // A permission, answered in the panel.
  await panel.fill("#input", "Run it, ask permission first");
  await panel.press("#input", "Enter");
  await panel.waitForSelector(".note.ask button");
  check((await panel.textContent(".note.ask")).includes("rm -rf /tmp/x"), "a permission shows what it's for");
  await panel.screenshot({ path: path.join(shots, "panel-permission.png") });
  await panel.click(".note.ask button.primary");
  await panel.waitForFunction(() => document.querySelectorAll(".msg.assistant .foot span").length === 2);
  check((await panel.textContent(".msg.assistant:last-of-type .note"))
    .includes("allowed"), "the permission is allowed");
  requests = await log();
  check(requests.some((r) => r.path.endsWith("/permission") && r.json?.decision === "allow" && r.json?.id
    === "p1"), "the answer went to Relay");

  // Stopped while it answers.
  let answers = await panel.$$eval(".msg.assistant", (all) => all.length);
  await panel.fill("#input", "A slow answer please");
  await panel.press("#input", "Enter");
  await panel.waitForFunction((n) => document.querySelectorAll(".msg.assistant").length > n
    && [...document.querySelectorAll(".msg.assistant")].pop().querySelector(".md")?.textContent.length > 10, answers);
  check(await panel.$eval("#send", (b) => b.classList.contains("stop")), "Send is Stop while it answers");
  await panel.click("#send");
  await panel.waitForFunction(() =>
    document.querySelector(".msg.assistant:last-of-type")?.textContent.includes("Stopped."));
  check(!(await panel.$eval("#send", (b) => b.classList.contains("stop"))), "stopped: Send again");
  const stoppedAt = (await panel.textContent(".msg.assistant:last-of-type .md")).length;
  check(stoppedAt < 300, `the answer stopped part of the way (${stoppedAt} characters)`);

  // Stopped before any text came: "Stopped.", and nothing that says null.
  answers = await panel.$$eval(".msg.assistant", (all) => all.length);
  await panel.fill("#input", "Slow, and stopped at once");
  await panel.press("#input", "Enter");
  await panel.waitForFunction((n) => document.querySelectorAll(".msg.assistant").length > n, answers);
  await panel.click("#send");
  await panel.waitForFunction(() => !document.querySelector("#send").classList.contains("stop")
    && [...document.querySelectorAll(".msg.assistant")].pop().querySelector(".foot span"));
  const early = await panel.$$eval(".msg.assistant", (all) => all.pop().textContent);
  check(early.includes("Stopped.") && !early.includes("You asked"), `stopped before its text: “${early}”`);
  check(!(await panel.textContent("#thread")).includes("null"), "nothing in the thread says null");

  // A screenshot goes as an attachment.
  await panel.click("#add-shot");
  await panel.waitForSelector("#contexts .thumb", { timeout: 5000 }).catch(() => null);
  const shot = Boolean(await panel.$("#contexts .thumb"));
  check(shot, `a screenshot is attached${shot ? "" : `: ${await panel.textContent("#notice")}`}`);
  if (shot) {
    await panel.fill("#input", "What's in the screenshot?");
    await panel.press("#input", "Enter");
    await panel.waitForFunction(() => document.querySelectorAll(".msg.assistant .foot span").length === 5);
    requests = await log();
    const upload = requests.find((r) => r.path === "/api/uploads");
    check(upload?.type === "image/png" && upload.size > 1000
      && /^screenshot-.*\.png$/.test(upload.filename), "uploaded as a PNG");
    const withShot = requests.filter((r) => r.path.endsWith("/messages")).pop();
    check(withShot?.json?.attachments?.length === 1, "and attached to the message");
    check(await panel.$$eval(".msg.user",
      (all) => Boolean(all.pop().querySelector(".chips .thumb"))), "the question shows it");
  }

  // Sent while it answers: queued, and answered next.
  answers = await panel.$$eval(".msg.assistant", (all) => all.length);
  await panel.fill("#input", "A slow first one");
  await panel.press("#input", "Enter");
  await panel.waitForFunction((n) => document.querySelectorAll(".msg.assistant").length > n
    && [...document.querySelectorAll(".msg.assistant")].pop().querySelector(".md")?.textContent.length > 10, answers);
  await panel.fill("#input", "Then this one");
  await panel.press("#input", "Enter");
  await panel.waitForSelector("#queue .queued-item");
  check(await panel.$eval("#input", (i) => i.value) === "", "sent while it answers: queued, the composer empty");
  check((await panel.textContent("#queue")) === "Next:Then this one",
    `shown as next: “${await panel.textContent("#queue")}”`);
  await panel.waitForFunction(() => {
    const last = [...document.querySelectorAll(".msg.assistant")].pop();
    return last.querySelector(".md")?.textContent.includes("You asked: Then this one")
      && last.querySelector(".foot span");
  }, null, { timeout: 20000 });
  check([...await panel.$$eval(".msg.user .bubble", (all) => all.map((b) => b.textContent))].pop() === "Then this one",
    "and answered next, with what was asked");
  check(!(await panel.$("#queue .queued-item")), "and gone from the queue");
  requests = await log();
  check(requests.some((r) => r.path.endsWith("/queue") && r.json?.text === "Then this one"), "through Relay's queue");

  // A second chat; the history finds and opens both.
  await panel.click("#new");
  await panel.waitForSelector(".empty");
  await panel.fill("#input", "Second chat");
  await panel.press("#input", "Enter");
  await panel.waitForFunction(() => document.querySelector(".msg.assistant .foot span"));
  requests = await log();
  check(!("title" in (requests.filter((r) => r.method === "POST" && r.path
    === "/api/conversations").pop()?.json || {})),
    "with nothing along, Relay names it");

  // Queued, then the answer stopped: it waits (as in Relay), shown so, and goes with Send now.
  // (asked, and its answer coming: counted before it's sent, as the answer may show at once)
  const askSlow = async (text) => {
    const n = await panel.$$eval(".msg.assistant", (all) => all.length);
    await panel.fill("#input", text);
    await panel.press("#input", "Enter");
    return panel.waitForFunction((n) => document.querySelectorAll(".msg.assistant").length > n
      && [...document.querySelectorAll(".msg.assistant")].pop().querySelector(".md")?.textContent.length > 10, n);
  };
  const answered = (asked) => panel.waitForFunction((asked) => {
    const last = [...document.querySelectorAll(".msg.assistant")].pop();
    return last.querySelector(".md")?.textContent.includes(`You asked: ${asked}`) && last.querySelector(".foot span");
  }, asked, { timeout: 20000 });
  await askSlow("A slow one, to stop");
  await panel.fill("#input", "Waits after a stop");
  await panel.press("#input", "Enter");
  await panel.waitForSelector("#queue .queued-item");
  await panel.click("#send");
  await panel.waitForFunction(() => document.querySelector("#queue").textContent.startsWith("Waiting:"));
  check((await panel.textContent("#queue")).includes("Waits after a stop")
    && Boolean(await panel.$("#queue button:has-text('Send now')")),
    "queued, then stopped: it waits, with Send now");
  await panel.screenshot({ path: path.join(shots, "panel-queue.png") });
  await new Promise((r) => setTimeout(r, 500));
  check(!(await panel.textContent("#thread")).includes("Waits after a stop"), "and isn't sent by itself");
  await panel.click("#queue button:has-text('Send now')");
  await answered("Waits after a stop");
  check(!(await panel.$("#queue .queued-item")), "Send now: sent and answered, the queue empty");

  // One taken back doesn't go.
  await askSlow("slow, and one taken back");
  await panel.fill("#input", "Never sent");
  await panel.press("#input", "Enter");
  await panel.waitForSelector("#queue .queued-item");
  await panel.click("#queue .chip-x");
  await panel.waitForFunction(() => !document.querySelector("#queue .queued-item"));
  const { current } = await worker.evaluate(() => chrome.storage.local.get("current"));
  const kept =
    await (await fetch(`${relay}/api/conversations/${current}`,
      { headers: { Authorization: "Bearer relay_test" } })).json();
  check(kept.queued.length === 0, "taken back from the queue: gone from Relay's too");
  await panel.click("#send");
  await panel.waitForFunction(() => !document.querySelector("#send").classList.contains("stop"));

  // Queued just as the answer ended: it went right away, and is followed.
  await panel.fill("#input", "This one lingers");
  await panel.press("#input", "Enter");
  await panel.waitForFunction(() =>
    [...document.querySelectorAll(".msg.assistant")].pop().querySelector(".md")?.textContent
    .includes("You asked: This one lingers") && document.querySelector("#send").classList.contains("stop"));
  await new Promise((r) => setTimeout(r, 300));
  await panel.fill("#input", "Right after");
  await panel.press("#input", "Enter");
  await answered("Right after");
  check(!(await panel.$("#queue .queued-item")),
    "queued just as the answer ended: it went right away, and is followed");
  requests = await log();
  check(requests.some((r) => r.path.endsWith("/queue") && r.json?.text === "Right after" && r.right_away),
    "(through the queue, which sent it right away)");
  await panel.click("#title");
  await panel.waitForFunction(() => document.querySelectorAll(".history-row").length === 2);
  check((await panel.textContent(".history-row.current")).includes("Second chat"),
    "the history lists both, this one marked");
  await panel.screenshot({ path: path.join(shots, "panel-history.png") });
  await panel.fill("#search", "page about");
  await panel.waitForFunction(() => document.querySelectorAll(".history-row").length === 1);
  await panel.click(".history-row");
  await panel.waitForFunction(() => document.querySelectorAll(".msg.assistant").length === 7);
  check((await panel.textContent("#title-text")) === "What is this page about?",
    "the first chat opens again from the history");
  check((await panel.textContent(".msg.user .chips")).includes("The Test Page"),
    "its messages as Relay kept them, with chips");
  check(await panel.$eval("#provider", (s) => s.value) === "opencode", "with its agent");
  await panel.screenshot({ path: path.join(shots, "panel-chat.png") });

  // Changing the agent changes the conversation's.
  await panel.selectOption("#provider", "claude-code");
  await panel.waitForFunction(() => [...document.querySelectorAll("#model option")].some((o) => o.value === "opus"));
  requests = await log();
  const patch = requests.filter((r) => r.method === "PATCH").pop();
  check(patch?.json?.provider_id === "claude-code" && patch?.json?.model === null,
    "another agent: the conversation is handed over");

  // Opened again: the same conversation; one that's answering is followed.
  answers = await panel.$$eval(".msg.assistant", (all) => all.length);
  await panel.fill("#input", "slow again");
  await panel.press("#input", "Enter");
  await panel.waitForFunction((n) => document.querySelectorAll(".msg.assistant").length > n
    && [...document.querySelectorAll(".msg.assistant")].pop().querySelector(".md")?.textContent.length > 10, answers);
  await panel.reload();
  await panel.waitForFunction(() =>
    document.querySelector(".msg.assistant:last-of-type .foot span")?.textContent.includes("$"),
    null, { timeout: 20000 });
  check((await panel.textContent(".msg.assistant:last-of-type .md")).includes("word39"),
    "reopened mid-answer: it follows the answer to its end");
  check((await panel.textContent("#title-text")) === "What is this page about?", "and it's the same conversation");

  // What the right-click menu leaves: "Summarize this page" starts a new chat and sends.
  await worker.evaluate(() =>
    chrome.storage.session.set({ pending: { at: Date.now(), fresh: true, send: "Summarize this page.",
      contexts: [{ kind: "page", url: "http://example.com/", title: "Example",
        text: "Example text", truncated: false }] } }));
  await panel.waitForFunction(() => document.querySelector("#title-text").textContent
    === "Summarize this page.", null, { timeout: 10000 });
  await panel.waitForFunction(() => document.querySelector(".msg.assistant .foot span"));
  check((await panel.textContent(".msg.user .chips")).includes("Example"),
    "the right-click menu's page went along, in a new chat");
  await worker.evaluate(() =>
    chrome.storage.session.set({ pending:
      { at: Date.now(), contexts: [{ kind: "link", url: "http://example.com/x", text: "A link" }] } }));
  await panel.waitForFunction(() => document.querySelector("#contexts")?.textContent.includes("A link"));
  check(true, "a link from the menu waits in the composer");

  // Writing on the page. "Rewrite with Relay" (as the menu does it: the focused field's text taken, the field kept):
  // asked in a new chat for only the new text, which "Replace in field" puts back in its place.
  await site.evaluate(() => {
    const note =
      Object.assign(document.createElement("textarea"), { id: "note", value: "hi is it still for sale? 30 euros?" });
    const editor =
      Object.assign(document.createElement("div"), { id: "editor", contentEditable: "true", textContent: "old words" });
    document.body.append(note, editor);
  });
  await site.focus("#note");
  const siteTab =
    await worker.evaluate(async () => (await chrome.tabs.query({ url: "http://127.0.0.1/__page*" }))[0].id);
  // (lib/inpage.js in the page, as the extension runs it there: what it answered, or its error thrown here)
  const inSite = async (name, args) => {
    const found = await worker.evaluate(async ([tabId, name, args]) => {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["lib/inpage.js"] });
      const [done] =
        await chrome.scripting.executeScript({ target: { tabId }, args: [name, args], func: (name, args) => {
          try {
            return { value: globalThis.__relayPage[name](...args) };
          } catch (error) {
            return { error: String(error.message || error) };
          }
        } });
      return done.result;
    }, [siteTab, name, args]);
    if (found.error) throw new Error(found.error);
    return found.value;
  };
  const field = await inSite("field", ["t1"]);
  check(field.text === "hi is it still for sale? 30 euros?" && !field.selected,
    `the field's text taken, all of it: “${field.text}”`);
  await worker.evaluate(([tabId, text]) => chrome.storage.session.set({ pending: { at: Date.now(), fresh: true,
    contexts: [{ kind: "selection", text, url: "http://127.0.0.1/__page", title: "The Test Page" }],
    rewrite: { tabId, frameId: 0, target: "t1", selected: false },
    send: "Improve this text: clearer and better written, in its own language, saying the same." } }),
    [siteTab, field.text]);
  await panel.waitForFunction(() => [...document.querySelectorAll(".msg.assistant .foot button")]
    .some((b) => b.textContent === "Replace in field"), null, { timeout: 15000 });
  check((await panel.textContent(".msg.user .bubble"))
    .includes("Answer with only the new text"), "asked for only the new text");
  check((await panel.textContent(".msg.user .chips")).trim() === "Selection on The Test Page",
    `with only the field's text along: ${await panel.textContent(".msg.user .chips")}`);
  check(!(await panel.textContent("#title-text")).includes("Answer with only"),
    `the chat named by what was asked: “${await panel.textContent("#title-text")}”`);
  await panel.click(".msg.assistant .foot button:has-text('Replace in field')");
  await site.waitForFunction(() =>
    document.querySelector("#note").value.startsWith("Hi! Is it still for sale?"), null, { timeout: 5000 })
    .catch(() => null);
  check(await site.$eval("#note", (t) => t.value) === "Hi! Is it still for sale? I can pick it up today for 35 euros.",
    `Replace in field: the field's text is the answer (“${await site.$eval("#note", (t) => t.value)}”)`);
  check((await panel.textContent(".msg.assistant .foot")).includes("Replaced"), "the button says so");
  await panel.screenshot({ path: path.join(shots, "panel-rewrite.png") });
  // (a selection: only it is replaced; an editor: its text)
  await site.evaluate(() => {
    const note = document.querySelector("#note");
    note.value = "I offer 30 euros, regards";
    note.focus();
    note.setSelectionRange(8, 16);
  });
  const part = await inSite("field", ["t2"]);
  await inSite("replace", ["t2", "35 euros"]);
  check(part.selected && part.text === "30 euros" && await site.$eval("#note", (t) => t.value)
    === "I offer 35 euros, regards", "a selection: only it taken, and replaced");
  await site.focus("#editor");
  const edited = await inSite("field", ["t3"]);
  await inSite("replace", ["t3", "new words"]);
  check(edited.text === "old words" && await site.$eval("#editor", (e) => e.textContent) === "new words",
    "an editor: its text replaced");
  const gone = await inSite("replace", ["t3", "again"]).then(() => "", (e) => String(e.message || e));
  check(await site.$eval("#editor", (e) => e.textContent) === "new words", `once only${gone ? `: ${gone}` : ""}`);

  // "Insert in page": an answer as plain text where the cursor is in the tab's focused field.
  await site.evaluate(() => {
    const note = document.querySelector("#note");
    note.value = "Dear buyer, -- Regards";
    note.focus();
    note.setSelectionRange(12, 12);
  });
  await panel.click(".msg.assistant .foot button:has-text('Insert in page')");
  await panel.waitForFunction(() =>
    [...document.querySelectorAll(".msg.assistant .foot button")].some((b) => b.textContent === "Inserted"),
    null, { timeout: 5000 }).catch(() => null);
  check(await site.$eval("#note", (t) => t.value)
    === "Dear buyer, Hi! Is it still for sale? I can pick it up today for 35 euros.-- Regards",
    `inserted at the cursor: “${await site.$eval("#note", (t) => t.value)}”`);
  await site.evaluate(() => document.activeElement.blur());
  await panel.click(".msg.assistant .foot button:has-text('Insert in page')");
  await panel.waitForFunction(  // (an earlier toast may still be up)
    () => document.querySelector("#toast").textContent.includes("No text field has the focus"),
    null, { timeout: 5000 }).catch(() => null);
  check((await panel.textContent("#toast")).includes("No text field has the focus"),
    `no field focused: it says so (${await panel.textContent("#toast")})`);

  // Scripts: grouped and searched; one with its description, run with the page's address as an argument, its
  // output as it comes, how it ended, the chat it started.
  await panel.click("#scripts-btn");
  await panel.waitForSelector(".script-row");
  check((await panel.textContent("#title-text")) === "Scripts", "the scripts view");
  const groups = await panel.$$eval(".group-label", (all) => all.map((g) => g.textContent));
  check(JSON.stringify(groups) === JSON.stringify(["Private", "Public", "Relay's examples"]), `grouped: ${groups}`);
  await panel.fill(".scripts .search", "hel");
  check((await panel.$$(".script-row")).length === 1, "searched");
  await panel.click(".script-row");
  await panel.waitForSelector(".script-args");
  check((await panel.textContent(".script-about p")) === "Say hello to whoever you name.",
    "its description, from its docstring");
  check((await panel.textContent(".script-output")).includes("Not run yet"), "not run yet");
  await panel.fill(".script-args", "Ada");
  await panel.click(".script-tools button:has-text('Page address')");
  await panel.waitForFunction(() => document.querySelector(".script-args").value.includes("__page"));
  const scriptArgs = await panel.$eval(".script-args", (i) => i.value);
  check(scriptArgs === `Ada http://127.0.0.1:${port}/__page`, `the page's address as an argument: ${scriptArgs}`);
  await panel.click(".script-run");
  await panel.waitForFunction(() => document.querySelector(".output-status")?.textContent.includes("Succeeded"));
  const output = await panel.textContent("pre.output");
  check(output.includes("Hello, Ada!") && output.includes(`args: ['Ada', 'http://127.0.0.1:${port}/__page']`),
    "its output, each argument as one");
  check((await panel.textContent(".output-status")).includes("exit 0"),
    `how it ended: ${await panel.textContent(".output-status")}`);
  check((await panel.textContent(".script-made")).includes("Started by hello.py"), "the chat it started");
  requests = await log();
  check(requests.some((r) => r.method === "POST" && r.path === "/api/scripts/private%2Fhello.py/run"
    && r.json?.args === scriptArgs),
    "run on Relay with those arguments");
  await panel.screenshot({ path: path.join(shots, "panel-script.png") });

  // The chat it started opens in the panel; back in the scripts, the script and its arguments are kept.
  await panel.click(".made-row");
  await panel.waitForFunction(() => document.querySelector("#title-text").textContent === "Started by hello.py");
  check(await panel.$eval("#scripts", (e) => e.hidden)
    && await panel.$eval("#thread", (e) => getComputedStyle(e).display !== "none"),
    "the chat it started opens in the panel");
  await panel.click("#scripts-btn");
  await panel.waitForFunction(() => document.querySelector(".script-title strong")?.textContent === "hello.py");
  check(await panel.$eval(".script-args", (i) => i.value) === scriptArgs,
    "back in the scripts: the script and its arguments kept");
  await panel.waitForFunction(() => document.querySelector("pre.output")?.textContent.includes("Hello, Ada!"));
  check(true, "with its last run");

  // A slow one: its output as it comes, Stop; run again and the panel reopened meanwhile: all of it, once.
  await panel.click(".script-head .icon-btn");
  await panel.waitForSelector(".script-row");
  check(await panel.$eval(".scripts .search", (i) => i.value) === "hel", "back in the list: its search kept");
  await panel.fill(".scripts .search", "");
  await panel.click(".script-row:has-text('slow.py')");
  await panel.waitForSelector(".script-run");
  await panel.click(".script-run");
  await panel.waitForFunction(() => document.querySelector("pre.output")?.textContent.includes("3\n"));
  check((await panel.textContent(".script-run")) === "Stop", "its output as it comes; Run is Stop meanwhile");
  await panel.click(".script-run");
  await panel.waitForFunction(() =>
    document.querySelector(".output-status")?.textContent.includes("Stopped"), null, { timeout: 10000 });
  const stoppedLines = (await panel.textContent("pre.output")).trim().split("\n").length;
  check(stoppedLines < 40, `stopped part of the way (${stoppedLines} lines)`);
  await panel.click(".script-run");
  await panel.waitForFunction(() => document.querySelector("pre.output")?.textContent.includes("4\n"));
  await panel.reload();
  await panel.waitForFunction(() => document.querySelector(".script-title strong")?.textContent === "slow.py");
  await panel.waitForFunction(() =>
    document.querySelector(".output-status")?.textContent.includes("Succeeded"), null, { timeout: 30000 });
  const lines = (await panel.textContent("pre.output")).trim().split("\n");
  check(lines.length === 59 && lines[0] === "1" && lines[58] === "59",
    `reopened while it ran: all its output, once (${lines.length} lines)`);
  check((await panel.$$(".run-row")).length === 2, "its runs listed");
  await panel.click(".run-row:not(.current)");
  await panel.waitForFunction(() => document.querySelector(".output-status")?.textContent.includes("Stopped"));
  check(true, "an earlier run's output shown");

  // One that fails.
  await panel.click(".script-head .icon-btn");
  await panel.click(".script-row:has-text('fails.py')");
  await panel.waitForSelector(".script-run");
  await panel.click(".script-run");
  await panel.waitForFunction(() => document.querySelector(".output-status")?.textContent.includes("Failed"));
  check((await panel.textContent(".output-status")).includes("exit 1")
    && (await panel.textContent("pre.output")).includes("Traceback"),
    "a failing one: its output, failed, exit 1");

  // What the right-click menu sends: into the open script's arguments; else kept for the script you pick.
  await worker.evaluate(() =>
    chrome.storage.session.set({ pending: { at: Date.now(), view: "scripts", args: "'a selection'" } }));
  await panel.waitForFunction(() => document.querySelector(".script-args")?.value === "'a selection'");
  check(true, "the menu's argument goes in the open script's field");
  await panel.click(".script-head .icon-btn");
  await panel.waitForSelector(".script-row");
  await worker.evaluate(() =>
    chrome.storage.session.set({ pending: { at: Date.now(), view: "scripts", args: "https://example.com/" } }));
  await panel.waitForFunction(() =>
    document.querySelector(".scripts .notice")?.textContent.includes("https://example.com/"));
  await panel.click(".script-row:has-text('hello.py')");
  await panel.waitForFunction(() => document.querySelector(".script-args")?.value === "https://example.com/");
  check(true, "with the list shown: kept for the script you pick");
  await panel.click("#scripts-btn");
  await panel.waitForFunction(() => document.querySelector("#title-text").textContent !== "Scripts");

  // Dark.
  await panel.emulateMedia({ colorScheme: "dark" });
  await panel.click("#title");
  await panel.click("#title");
  await panel.screenshot({ path: path.join(shots, "panel-dark.png") });
  await panel.emulateMedia({ colorScheme: "light" });

  // Without settings: the setup screen.
  await worker.evaluate(() => chrome.storage.local.clear());
  await panel.waitForSelector("#setup:not([hidden])");
  check((await panel.textContent("#setup")).includes("Tell the extension where your Relay is"),
    "without settings, the panel says what to do");
  await panel.screenshot({ path: path.join(shots, "panel-setup.png") });

  const code = finish(errors);
  await context.close();
  process.exit(code);
})().catch((error) => {
  console.error(error);
  process.exit(2);
});
