// The panel's scripts against a real Relay: the list, one script, run, its output as it comes, compared with
// what Relay kept of the run. It runs one script (by default Relay's example room_left.py without arguments,
// which only reads your usage limits).
//
//   RELAY_URL=http://127.0.0.1:8420 RELAY_APP_PASSWORD=relay_… node real_scripts.cjs [script] [args]

const path = require("path");
const { launch, check, finish } = require("./harness.cjs");

const relay = (process.env.RELAY_URL || "http://127.0.0.1:8420").replace(/\/+$/, "");
const password = process.env.RELAY_APP_PASSWORD;
const script = process.argv[2] || "public/examples/room_left.py";
const args = process.argv[3] || "";
const { shots } = require("./harness.cjs");

async function api(route) {
  const response =
    await fetch(relay + route, { headers: { Authorization: `Bearer ${password}`, "X-Relay-Request": "1" } });
  return response.json();
}

(async () => {
  if (!password) throw new Error("RELAY_APP_PASSWORD isn't set");
  const { context, worker, id, errors, watch } = await launch();
  await worker.evaluate(([url, appPassword]) =>
    chrome.storage.local.set({ settings: { url, appPassword } }), [relay, password]);
  const panel = await context.newPage();
  watch(panel, "panel");
  await panel.goto(`chrome-extension://${id}/sidepanel.html`);
  await panel.waitForSelector("#thread .empty, #thread .msg", { timeout: 30000 });

  await panel.click("#scripts-btn");
  await panel.waitForSelector(".script-row", { timeout: 30000 });
  const listed = await panel.$$eval(".script-row", (rows) => rows.map((r) => r.title));
  const known = (await api("/api/scripts")).scripts.map((s) => s.name);
  check(JSON.stringify([...listed].sort()) === JSON.stringify([...known].sort()),
    `the list has your ${known.length} scripts`);
  await panel.click(`.script-row[title="${script}"]`);
  await panel.waitForSelector(".script-args");
  check((await panel.textContent(".script-title strong")) === script.split("/").pop(), `${script} opened`);
  console.log(`     its description: “${(await panel.textContent(".script-about p").catch(() => ""))
    .trim().slice(0, 90)}…”`);
  await panel.fill(".script-args", args);
  await panel.click(".script-run");
  await panel.waitForFunction(() => !/Running/.test(document.querySelector(".output-status")?.textContent || "Running"),
    null, { timeout: 5 * 60000 });
  const shown = await panel.textContent("pre.output");
  const ended = await panel.textContent(".output-status");
  check(/Succeeded · exit 0/.test(ended), `it ended: ${ended}`);
  const { runs } = await api(`/api/scripts/${encodeURIComponent(script)}`);
  const run = await api(`/api/scripts/runs/${runs[0].id}`);
  check(run.args === args && run.status === "succeeded",
    `Relay has the run: ${run.status}, args ${JSON.stringify(run.args)}`);
  check(shown === run.output && shown.length > 0,
    `the output as it came is Relay's, to the character (${shown.length})`);
  console.log(shown.split("\n").slice(0, 8).map((line) => `     | ${line}`).join("\n"));
  await panel.screenshot({ path: path.join(shots, "real-script.png") });

  // Opened again: that run, as Relay has it.
  await panel.reload();
  await panel.waitForFunction((n) => document.querySelector(".script-title strong")?.textContent === n,
    script.split("/").pop(), { timeout: 30000 });
  await panel.waitForFunction(() =>
    document.querySelector("pre.output")?.textContent.length > 0, null, { timeout: 30000 });
  check((await panel.textContent("pre.output")) === run.output, "reopened: the same output");

  const code = finish(errors);
  await context.close();
  process.exit(code);
})().catch((error) => {
  console.error(error);
  process.exit(2);
});
