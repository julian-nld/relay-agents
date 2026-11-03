// Relay's Perplexity: the user's own perplexity.ai account (a Pro subscription, no API key) as a
// provider. It runs in Perplexity's ProviderBox (relay/providers/perplexity/provider.py), a box of the WorkBox
// image for its Chromium: the site in a headless browser, signed in with the user's session cookie,
// asks the site's own endpoint (/rest/sse/perplexity_ask) from the page itself, so it's the site
// asking, with its own cookies, and reads the answer as it streams.
//
//   node perplexity.cjs      questions on stdin, a JSON line each: {question, session, model}
//   env PERPLEXITY_PROFILE   the browser's profile folder (kept: the site's other cookies stay), with
//                            the user's session cookie in its file "cookie" (its value, or a whole
//                            "name=value; …" Cookie header), from Relay's settings
//
// One browser for all of a turn's questions (Relay's tool loop asks again with the results). It
// prints JSON lines: {"type": "ready", "signed_in"} once the site is open; then per question
// Relay's uniform events (opencode_manager/bridge/events.py) as they come (the search as a tool call
// and result, the answer as text) and {"type": "answered", text, sources, session, model, error}:
// the whole answer, and the thread to go on in. It ends when its input does.

"use strict";
const crypto = require("crypto");
const fs = require("fs");

const { chromium } = require("/usr/local/lib/node_modules/playwright-core");

const SITE = "https://www.perplexity.ai";
const TIME_LIMIT = 10 * 60 * 1000;
const AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36";
// What the site says it can show; the answer comes as these blocks.
const BLOCKS = ["answer_modes", "media_items", "inline_entity_cards", "place_widgets", "finance_widgets",
  "sports_widgets", "news_widgets", "shopping_widgets", "jobs_widgets", "search_result_widgets", "inline_images",
  "inline_assets", "placeholder_cards", "diff_blocks", "entity_group_v2", "refinement_filters", "canvas_mode",
  "maps_preview", "answer_tabs", "price_comparison_widgets", "preserve_latex", "generic_onboarding_widgets",
  "in_context_suggestions", "pending_followups", "inline_claims", "unified_assets", "workflow_steps",
  "workflow_widgets", "navigation_results", "background_agents"];

const send = (event) => process.stdout.write(JSON.stringify(event) + "\n");

function cookies(text) {
  text = (text || "").trim();
  if (!text) return [];
  const pairs = text.includes("=")
    ? text.split(";").map((part) => part.trim()).filter(Boolean)
      .map((part) => [part.slice(0, part.indexOf("=")).trim(), part.slice(part.indexOf("=") + 1).trim()])
    : [["__Secure-next-auth.session-token", text]];
  // (a __Host- cookie may name no domain: it's the site's own)
  return pairs.filter(([name]) => name).map(([name, value]) => ({
    name, value, ...(name.startsWith("__Host-") ? { url: SITE + "/" } : { domain: ".perplexity.ai", path: "/" }),
    secure: true, httpOnly: true, sameSite: "Lax",
  }));
}

// JSON Patch (add, replace, remove), as the site sends its blocks' changes.
function patch(document, patches) {
  for (const change of patches || []) {
    const parts = change.path.split("/").slice(1).map((p) => p.replace(/~1/g, "/").replace(/~0/g, "~"));
    if (!parts.length) {
      document = change.value;
      continue;
    }
    if (document === undefined || document === null) document = /^\d+$|^-$/.test(parts[0]) ? [] : {};
    let at = document;
    for (let i = 0; i < parts.length - 1; i++) {
      if (at[parts[i]] === undefined || at[parts[i]] === null) at[parts[i]] = /^\d+$|^-$/.test(parts[i + 1]) ? [] : {};
      at = at[parts[i]];
    }
    const last = parts[parts.length - 1];
    if (change.op === "remove") {
      if (Array.isArray(at)) at.splice(Number(last), 1);
      else delete at[last];
    } else if (Array.isArray(at) && last === "-") {
      at.push(change.value);
    } else if (Array.isArray(at) && change.op === "add") {
      at.splice(Number(last), 0, change.value);
    } else {
      at[last] = change.value;
    }
  }
  return document;
}

async function main() {
  const folder = process.env.PERPLEXITY_PROFILE || "/tmp/perplexity-profile";
  const context = await chromium.launchPersistentContext(folder, {
    headless: true, executablePath: "/usr/bin/chromium", userAgent: AGENT, locale: "en-US",
    viewport: { width: 1280, height: 900 }, args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  // The site's cookies, session ones too (Chromium drops those when it closes): a thread stays the
  // same visitor's, or the same signed-in one's, from one question to the next.
  const kept = `${folder}/relay-cookies.json`;
  try {
    let saved = [];
    try { saved = JSON.parse(fs.readFileSync(kept, "utf8")); } catch { /* (none yet) */ }
    for (const cookie of saved) {  // (one by one: one Chromium won't take back doesn't cost the rest)
      try { await context.addCookies([cookie]); } catch (error) { console.error("cookie", cookie.name, error.message); }
    }
    let cookie = "";
    try { cookie = fs.readFileSync(`${folder}/cookie`, "utf8"); } catch { /* (none) */ }
    const given = cookies(cookie);
    if (given.length) await context.addCookies(given);
    const page = context.pages()[0] || await context.newPage();
    let current = null;  // the question being answered: its blocks and what's shown of it
    await page.exposeFunction("relayChunk", (chunk) => current && current.chunk(chunk));
    await page.exposeFunction("relayFailed", (status, body) => { if (current) current.failed = { status, body }; });
    const open = async () => {
      await page.goto(SITE + "/", { waitUntil: "domcontentloaded", timeout: 60000 });
      await page.waitForLoadState("networkidle", { timeout: 20000 }).catch(() => {});  // (the site sets itself up)
    };
    await open();
    const user = await page.evaluate(async () => {
      try { return await (await fetch("/api/auth/session")).json(); } catch { return {}; }
    });
    const signedIn = Boolean(user && user.user);
    send({ type: "ready", signed_in: signedIn, cookie: given.length > 0 });
    const lines = require("readline").createInterface({ input: process.stdin });
    for await (const line of lines) {
      if (!line.trim()) continue;
      let asked;
      try { asked = JSON.parse(line); } catch { continue; }
      const timer = setTimeout(() => {
        send({ type: "answered", text: "", sources: [], session: asked.session || "",
          error: "Perplexity didn't finish in time" });
        process.exit(1);
      }, TIME_LIMIT);
      try {
        send(await answer(page, open, asked, signedIn, (q) => { current = q; }));
      } catch (error) {
        send({ type: "answered", text: "", sources: [], session: asked.session || "",
          error: String(error && error.message || error).slice(0, 500) });
      } finally {
        clearTimeout(timer);
        current = null;
      }
    }
  } finally {
    try {
      fs.writeFileSync(kept,
        JSON.stringify((await context.cookies()).filter((c) => c.domain.endsWith("perplexity.ai"))),
        { mode: 0o600 });
    } catch { /* (only a convenience) */ }
    await context.close().catch(() => {});
  }
}

// One question in the page: its events as they come, and the answered line.
async function answer(page, open, asked, signedIn, follow) {
  const question = String(asked.question || "");
  const model = String(asked.model || "");
  let thread = {};
  try { thread = asked.session ? JSON.parse(asked.session) : {}; } catch { thread = {}; }
  const blocks = {};
  let buffer = "", shown = "", searched = false, last = null, refused = null;
  const state = { failed: null };
  function text() {
    let found = "";
    for (const step of blocks.workflow_root?.workflow_block?.steps || []) {
      for (const item of step.items || []) {
        const said = item.payload?.text_payload?.text;
        if (typeof said === "string" && said) found = said;
      }
    }
    const markdown = blocks.ask_text?.markdown_block?.answer;  // (another shape the site has used)
    const whole = typeof markdown === "string" && markdown.length > found.length ? markdown : found;
    // (citation markup the page renders itself: private-use characters, a stray "cite" before [n])
    return whole.replace(/[\ue000-\uf8ff]/g, "").replace(/\s?cite(?=\[\d)/g, "");
  }
  function queries() {
    const found = [];
    for (const step of blocks.workflow_root?.workflow_block?.steps || []) {
      for (const item of step.items || []) found.push(...(item.payload?.queries_payload?.queries || []));
    }
    return found;
  }
  function sources() {
    const results = blocks.web_results?.web_result_block?.web_results
      || blocks.sources_answer_mode?.sources_mode_block?.web_results || [];
    return results.filter((r) => r && r.url).map((r) => ({ name: r.name || r.url, url: r.url }));
  }
  function take(message) {
    if (message.error_code) refused = message;  // (e.g. INVALID_MODEL_SELECTION: a model it doesn't take)
    if (message.backend_uuid) last = message;  // (end_of_stream's is empty)
    for (const block of message.blocks || []) {
      const usage = block.intended_usage;
      if (!usage) continue;
      blocks[usage] = blocks[usage] || {};
      for (const [key, value] of Object.entries(block)) {
        if (key === "intended_usage") continue;
        if (key === "diff_block") blocks[usage][value.field] = patch(blocks[usage][value.field], value.patches);
        else blocks[usage][key] = value;
      }
    }
    const looked = queries();
    if (!searched && looked.length) {
      searched = `search-${crypto.randomUUID().slice(0, 8)}`;
      send({ type: "tool_call", id: searched, name: "web_search", input: { queries: looked } });
    }
    const now = text();
    if (now.startsWith(shown) && now.length > shown.length) {
      send({ type: "text", delta: now.slice(shown.length) });
      shown = now;
    }
  }
  follow({
    chunk(chunk) {
      buffer += chunk.replace(/\r/g, "");  // (its lines end in \r\n; in its JSON a \r is escaped)
      let end;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const event = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const data = event.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
        if (!data) continue;
        try { take(JSON.parse(data)); } catch { /* (not one of its messages) */ }
      }
    },
    set failed(value) { state.failed = value; },
  });
  const followup = Boolean(thread.b && thread.t);
  const body = {
    params: {
      attachments: [], language: "en-US",
      timezone: process.env.TZ || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      search_focus: "internet", sources: ["web"], frontend_uuid: crypto.randomUUID(), mode: "copilot",
      model_preference: model || (signedIn ? "pplx_pro" : "turbo"), is_related_query: false, is_sponsored: false,
      prompt_source: "user",
      query_source: followup ? "followup" : "home", is_incognito: false, local_search_enabled: false,
      use_schematized_api: true, send_back_text_in_streaming_api: false, supported_block_use_cases: BLOCKS,
      client_coordinates: null, mentions: [], skip_search_enabled: true,
      is_nav_suggestions_disabled: false, source: "default", always_search_override: false,
      browser_agent_allow_once_from_toggle: false,
      override_no_search: false, should_ask_for_mcp_tool_confirmation: true, supports_tool_approval_modal: true,
      force_enable_browser_agent: false, supported_features: ["browser_agent_permission_banner_v1.1"],
      extended_context: false, local_workspace_directories: [], is_local_browser_available: false, version: "2.18",
      rum_session_id: crypto.randomUUID(), time_from_first_type: 800,
      // (a follow-up names the thread's last answer; a new one has a context and the query as dsl_query)
      ...(followup ? { last_backend_uuid: thread.b, read_write_token: thread.t, followup_source: "link" }
        : { frontend_context_uuid: crypto.randomUUID(), dsl_query: question }),
    },
    query_str: question,
  };
  const ask = () => page.evaluate(async (body) => {
    const response = await fetch("/rest/sse/perplexity_ask", { method: "POST", body: JSON.stringify(body),
      headers: { "content-type": "application/json", accept: "text/event-stream" } });
    if (!response.ok) {
      await window.relayFailed(response.status, (await response.text()).slice(0, 500));
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      await window.relayChunk(decoder.decode(value, { stream: true }));
    }
    await window.relayChunk("\n\n");
  }, body);
  await ask();
  if (state.failed && state.failed.status === 403 && /Just a moment/.test(state.failed.body)) {
    // Cloudflare's check: the page passes it by loading again (its cookies then say so); once more
    state.failed = null;
    await open();
    await ask();
  }
  const session = last && last.backend_uuid
    ? JSON.stringify({ b: last.backend_uuid, t: last.read_write_token || thread.t,
        s: last.thread_url_slug || thread.s })
    : asked.session || "";
  if (state.failed) {
    const failed = state.failed;
    const why = failed.status === 429 ? "Perplexity's limit for now was reached (HTTP 429)"
      : failed.status === 401 || failed.status === 403 ? `Perplexity refused (HTTP ${failed.status}): sign in again `
        + "(a new session cookie in Relay's Settings → Providers)"
        : `Perplexity answered HTTP ${failed.status}: ${failed.body}`;
    return { type: "answered", text: "", sources: [], session, error: why };
  }
  const said = text();
  if (!said.startsWith(shown)) send({ type: "text", delta: "\n\n" + said });  // (rewritten as it went)
  else if (said.length > shown.length) send({ type: "text", delta: said.slice(shown.length) });
  const found = sources();
  // (searched: the web_search tool call's id, whose result, the sources, Relay adds)
  return { type: "answered", text: said, sources: found, searched: searched || null, session,
    model: last && last.display_model,
    error: said ? null
      : refused && refused.error_code === "INVALID_MODEL_SELECTION" ? `Perplexity doesn't take the model "${model}" `
        + "on this account. Its ids are as the site sends them (on perplexity.ai, DevTools → Network → "
        + "perplexity_ask → model_preference); Settings → Providers → Perplexity → Models lists the ones to offer."
        : refused ? `Perplexity refused: ${refused.text || refused.error_code}`
          : "Perplexity gave no answer" + (model ? ` (is ${model} one of its models?)` : "") };
}

main().catch((error) => {
  send({ type: "answered", text: "", sources: [], session: "",
    error: String(error && error.message || error).slice(0, 500) });
  process.exitCode = 1;
});
