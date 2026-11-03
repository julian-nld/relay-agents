// A provider's page (relay/creations.py): one Relay doesn't know, added with an agent (/providers/new/<id>), or one
// it has, repaired (/providers/repair/<id>). Three steps: install it and log in (a shell in its box), or what's wrong
// with it; its agent's plan (Approve, Redraft, Deny); and a trial Relay to try it in before Add or Apply.

import { api } from "./api.js";
import { relayUrl, storeKey } from "./base.js";
import { renderMarkdown } from "./markdown.js";
import { restartRelay } from "./restart.js";
import { eligibleProviders, modelsFor } from "./state.js";
import { inItsBox, mountTerminal } from "./terminal.js";
import { clear, confirmDialog, h, icon, timeAgo, toast } from "./ui.js";

const POLL = 2500;  // ms between looks while something goes on (the agent, Relay's check)
const BUILDER = "relay.newProviderBuilder";  // the agent chosen last, to integrate or repair the next one

// What each kind of page says (a new provider's: `creates`; a repair's)
function words(c) {
  const name = c.provider?.name || c.target_id;
  return c.creates ? {
    heading: ["plus", "Add a provider with an agent"],
    intro: "So, you've decided to add a new provider with an agent. Here's what you will have to do, and what Relay "
      + "will do for you:",
    steps: ["Install and log into your provider in a new ProviderBox", "Integration", "Finalisation"],
    planning: "An agent of yours studies the program in its box and drafts a plan to add it, for you to approve.",
    planned: `The agent has drafted a plan to add your provider${c.target_id ? ` (${c.target_id})` : ""}. See the plan `
      + "below:",
    waiting: "Approved, the agent builds it and Relay checks it fully, in a box of its own. Then you try it in a "
      + "throwaway Relay with it, before it's added.",
    working: "The agent is building it.",
    nothing: "The agent is done, without building it: its answer is in its conversation.",
    trying: "The agent has added your provider in a throwaway Relay instance. Please visit it and determine if adding "
      + "your provider was successful. If so, click Add below. The throwaway Relay will be deleted and your provider "
      + "will be added.",
    last: ["add", "Add", "Added: Relay restarts to load it"],
    cancel: ["Cancel adding it", "Throw this new provider away?", "Its box, its home (what you installed there, your "
      + "login), the agent's work and the throwaway Relay go."],
    undone: "Taken out again.",
    legacy: "This new provider was made the old way, before Relay had this page: discard it, and add it again with an "
      + "agent.",
  } : {
    heading: ["tool", `Repair ${name}`],
    intro: `So, ${name} doesn't work as it should. Here's what you will have to do, and what Relay will do for you:`,
    steps: ["What's wrong", "The plan", "Finalisation"],
    planning: `An agent of yours looks into it (${name}'s program, as Relay runs it) and drafts a plan to repair it, `
      + "for you to approve.",
    planned: "The agent has drafted a plan to repair it. See the plan below:",
    waiting: "Approved, the agent repairs it and Relay checks it fully, in a box of its own. Then you try it in a "
      + "throwaway Relay with it, before it's applied.",
    working: "The agent is repairing it.",
    nothing: "The agent is done, without changing anything: its answer is in its conversation.",
    trying: `The agent has repaired ${name} in a throwaway Relay instance. Please visit it and determine if the repair `
      + "was successful. If so, click Apply below. The throwaway Relay will be deleted and the repair applied.",
    last: ["apply", "Apply", "Applied: Relay restarts to use it"],
    cancel: ["Cancel the repair", "Throw this repair away?", `The agent's work and the throwaway Relay go; ${name} `
      + "stays as it is."],
    undone: "Undone: its originals are back.",
    legacy: "This repair was made the old way, before Relay had this page: discard it, and repair it again from "
      + "Settings → Providers.",
  };
}

// The page with no new provider yet: what it is, the ones going on, and a start (with `about`: the Governor's words)
export function newProviderStart({ about = "", onOpen }) {
  const el = h("div.newprovider-view");
  const list = h("div.newprovider-going");
  const start = h("button.btn.primary", { onclick: async () => {
    start.disabled = true;
    try {
      const made = await api.post("/api/provider-creations", {});
      onOpen(made.id, about);
    } catch (error) {
      toast(error.message, "error");
      start.disabled = false;
    }
  } }, icon("plus", "sm"), "Start");
  const say = words({ creates: true });
  clear(el, heading(say), h("p.newprovider-intro", say.intro),
    about ? h("p.newprovider-about", h("strong", "AgentOS's Governor says: "), about) : null,
    list, h("div.newprovider-actions", start));
  api.get("/api/provider-creations").then((going) => {
    if (!going.length) return;
    clear(list, h("p", "Going on:"), h("ul", going.map((c) => h("li",
      h("a", { href: relayUrl(`/providers/new/${c.id}`), onclick: (e) => { e.preventDefault(); onOpen(c.id); } },
        c.target_id || (c.about ? c.about.slice(0, 80) : "A new provider")), ` · ${stepOf(c)}`))));
  }).catch(() => {});
  return { el, close() {} };
}

// A provider to repair, before its repair: what's wrong with it, its repair going on (if any), and a start
export function repairStart(providerId, { onOpen }) {
  const el = h("div.newprovider-view");
  let shell = null;
  clear(el, h("div.spinner"));
  api.get(`/api/providers/${encodeURIComponent(providerId)}/trouble`).then((found) => {
    const say = words({ creates: false, provider: found.provider });
    const start = h("button.btn.primary", { onclick: async () => {
      start.disabled = true;
      try {
        onOpen((await api.post("/api/provider-repairs", { provider_id: found.provider.id })).id);
      } catch (error) {
        toast(error.message, "error");
        start.disabled = false;
      }
    } }, icon("tool", "sm"), found.repair ? "Open its repair" : "Start");
    const p = found.provider;
    clear(el, heading(say), h("p.newprovider-intro", say.intro), trouble(found),
      found.login_lost && (p.shell || p.sign_in || p.home) ? signIn(p, found) : null,
      found.repair ? h("p", "A repair of it is going on already.") : null, h("div.newprovider-actions", start));
  }).catch((error) => clear(el, h("p.error-text", error.message)));
  return { el, close() { shell?.end(); } };

  // Its latest error says its login is lost: that's no repair, but signing in again, in its shell (Settings →
  // Providers' Shell), then a test of the model that failed last
  function signIn(p, found) {
    const status = h("span.shell-status", { role: "status" }, "Connecting to its box…");
    const host = h("div.shell-terminal", { "aria-label": `Shell in ${p.name}'s box` });
    shell = mountTerminal(host, `/api/providers/${encodeURIComponent(p.id)}/terminal`, status, {
      connected: p.home ? "A shell in a box with its home: log in again here." : inItsBox(p),
      onEnd: () => { shell = null; } });
    const failed = Object.values(found.models || {}).filter((e) => !e.ok).sort((a, b) => b.checked - a.checked)[0];
    const said = h("span.muted", { role: "status" });
    const test = h("button.btn.small", { onclick: async () => {
      test.disabled = true;
      clear(said, "Testing…");
      try {
        const result =
          await api.post(`/api/providers/${encodeURIComponent(p.id)}/test`, { model: failed?.model || "" });
        clear(said, result.ok ? "It works again: " : "It still fails: ", result.message);
      } catch (error) {
        clear(said, error.message);
      } finally {
        test.disabled = false;
      }
    } }, "Test again");
    return h("section.newprovider-signin",
      h("p", "Its login looks lost: its latest error says so. That's no repair: sign it in again here",
        p.sign_in ? [" with ", h("code", p.sign_in)] : "", ", then test it.", p.shell_hint ? ` ${p.shell_hint}` : ""),
      h("div.newprovider-shell", host, h("div.service-foot", status, shell.keys)),
      h("div.newprovider-actions", test, said),
      h("p.muted", "If it isn't the login, an agent of yours repairs Relay's support for it:"));
  }
}

// What's wrong with a provider, as Relay sees it: its check, its models, its latest errors
function trouble(found) {
  const models = Object.entries(found.models || {});
  return h("div.newprovider-trouble",
    h("p", h(`span.${found.check.ok ? "ok" : "fail"}`, found.check.ok ? "Its check passes: " : "Its check fails: "),
      found.check.message),
    models.length ? h("ul.repair-facts", models.map(([model, entry]) => h(`li.${entry.ok ? "ok" : "fail"}`,
      `${model || "Its default model"}: ${entry.ok ? "worked" : "failed"} ${timeAgo(entry.checked)}`,
      entry.message ? ` (${entry.message})` : ""))) : null,
    found.errors?.length ? h("details", h("summary", `Its latest errors (${found.errors.length})`),
      h("ul.repair-facts", found.errors.map((e) => h("li.fail", e.error)))) : null);
}

// One page, step by step. `kind`: "new" or "repair" (its address's)
export function providerPage(id, { kind = "new", about = "", onOpenConversation }) {
  const base = kind === "repair" ? "/api/provider-repairs" : "/api/provider-creations";
  const el = h("div.newprovider-view");
  const steps = [h("section.newprovider-step"), h("section.newprovider-step"), h("section.newprovider-step")];
  const foot = h("div.newprovider-foot");
  const status = h("span.shell-status", { role: "status" }, "Connecting to its box…");
  const shellHost = h("div.shell-terminal", { "aria-label": "Shell in the provider's box" });
  const top = h("div");
  let shell = null;
  let creation = null;
  let found = null;  // (a repair's: what's wrong with its provider)
  let shown = "";
  let closed = false;
  let timer = null;
  let draft = about;  // (what the user is writing in step 1, kept across redraws)
  let wanted = "";  // (a redraft's or a fix's words)
  clear(el, top, ...steps, foot);

  const act = (button, run) => async () => {
    button.disabled = true;
    try {
      await run();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
    }
  };
  const post = async (action, body = {}) => {
    creation = await api.post(`${base}/${id}/${action}`, body);
    draw(true);
  };

  function openShell(c) {
    if (shell) return;
    shell = mountTerminal(shellHost, `${base}/${id}/terminal`, status, {
      connected: c.creates ? "A shell in its box, in its home: what you install and log in to here stays."
        : c.home
        ? "A shell in a box with its home: update its program or log in again here; what you change there stays."
          : inItsBox(c.provider || {}),
      onEnd: () => { shell = null; } });
  }

  // The agent that does it: a provider with tools (not the one repaired) and its model, the last choice preselected
  function chooser(c, label) {
    const builders = eligibleProviders("code").filter((p) => c.creates || p.id !== c.target_id);
    const kept =
      (() => { try { return JSON.parse(localStorage.getItem(storeKey(BUILDER)) || "{}"); } catch { return {}; } })();
    const builder =
      h("select.select", builders.map((p) => h("option", { value: p.id, selected: p.id === kept.provider },
        p.name)));
    const model = h("select.select");
    const models = async () => {
      const listed = await modelsFor(builder.value);
      clear(model, h("option", { value: "" }, "Its default model"), (listed || []).filter((m) => m.id).map((m) =>
        h("option", { value: m.id, selected: m.id === kept.model }, m.name || m.id)));
    };
    builder.addEventListener("change", () => { kept.model = ""; models(); });
    if (builders.length) models();
    return {
      el: h("div.newprovider-builder",
        h("label.field", h("span", label), builders.length ? builder
          : h("p.muted", "No other provider with tools: add one first (Settings → Providers).")),
        h("label.field", h("span", "Model"), model)),
      ready: builders.length > 0,
      chosen() {
        try { localStorage.setItem(storeKey(BUILDER),
          JSON.stringify({ provider: builder.value, model: model.value })); }
        catch { /* (only a convenience) */ }
        return { provider_id: builder.value, model: model.value };
      },
    };
  }

  // 1. Install and log in (a new provider), or what's wrong (a repair)
  function stepOne(c, say) {
    const active = c.status === "setup";
    const parts = [h("h2", number(1, !active), say.steps[0])];
    const going = ["planning", "planned", "building", "trying"].includes(c.status);
    const inBox = c.provider?.shell || c.provider?.sign_in;  // (a shell in its own box)
    const shelled = c.home || inBox;
    const again = () =>
      h("details.newprovider-shell-again", { ontoggle: (e) => { if (e.currentTarget.open) openShell(c); } },
        h("summary", "Its shell (to log in again, say)"), shellHost, h("div.service-foot", status, shell?.keys));
    if (c.creates && active) {
      openShell(c);
      const text = h("textarea.textarea", { rows: 3, value: draft, placeholder: "e.g. Gemini CLI (npm i -g "
        + "@google/gemini-cli), logged in with my Google account", oninput: () => { draft = text.value; } });
      const who = chooser(c, "Integrated by");
      const go = h("button.btn.primary", { disabled: !who.ready }, "Continue");
      go.onclick = act(go, () => post("start", { about: text.value, ...who.chosen() }));
      parts.push(
        h("p", "Please install the provider of your choice and log in to your account, in the shell below. Its box "
          + "keeps what you install in its home: npm i -g, pip install --user, or the program's own installer. When "
          + "you're done, tell Relay what provider it is. A Relay agent will figure out the rest."),
        h("div.newprovider-shell", shellHost, h("div.service-foot", status, shell?.keys)),
        h("label.field", h("span", "What did you install? Explain briefly, so the agent can integrate your provider "
          + "into Relay."), text),
        who.el, h("div.newprovider-actions", go));
    } else if (c.creates) {
      parts.push(h("p.muted", "You installed: ", c.about || "–"), going ? again() : null);
    } else if (active) {
      if (shelled) openShell(c);
      const text =
        h("textarea.textarea", { rows: 3, value: draft, placeholder: "e.g. its answers come back empty since "
          + "its update (optional)", oninput: () => { draft = text.value; } });
      const who = chooser(c, "Repaired by");
      const go = h("button.btn.primary", { disabled: !who.ready }, "Continue");
      go.onclick = act(go, () => post("start", { about: text.value, ...who.chosen() }));
      parts.push(found ? trouble(found) : h("div.spinner"),
        c.home
          ? [h("p", "Its program and its login live in its own home. If the program needs an update or a new login, "
            + "do that in the shell below (then Settings → Providers → its shield checks it again); if it's Relay's "
            + "support for it that broke, an agent of yours repairs that."),
          h("div.newprovider-shell", shellHost, h("div.service-foot", status, shell?.keys))]
          : inBox
            ? [h("p", "Its program and its login are this machine's. "
              + "If its login is lost (its errors say it isn't signed "
              + "in, or can't authenticate), sign in again in the shell below",
              c.provider.sign_in ? [" with ", h("code", c.provider.sign_in)] : "",
              " (then Settings → Providers → "
              + "Test checks it again); if it's Relay's support for it that broke, an agent of yours repairs that.",
              c.provider.shell_hint ? ` ${c.provider.shell_hint}` : ""),
            h("div.newprovider-shell", shellHost, h("div.service-foot", status, shell?.keys))]
            : h("p.muted", "Its program and its login are this machine's: an old version or a lost login is fixed "
              + "there. If it's Relay's support for it that broke, an agent of yours repairs that."),
        h("label.field", h("span", "What's wrong, in your words"), text), who.el, h("div.newprovider-actions", go));
    } else {
      parts.push(h("p.muted", "You said: ", c.about || "nothing more"), going && shelled ? again() : null);
    }
    return parts;
  }

  // 2. Its agent's plan
  function stepTwo(c, say) {
    const parts = [h("h2", number(2, ["building", "trying", "applied", "undone"].includes(c.status)), say.steps[1])];
    const chat = c.conversation_id ? h("a", { href: relayUrl(`/c/${c.conversation_id}`), onclick: (e) => {
      e.preventDefault(); onOpenConversation(c.conversation_id); } }, "its conversation") : null;
    if (c.status === "setup") {
      parts.push(h("p.muted", say.planning));
    } else if (c.status === "planning" && c.working) {
      parts.push(h("p", h("span.spinner.sm"), " The agent is looking into it and drafting a plan (", chat, ")."));
    } else if (c.status === "planning") {
      parts.push(h("p", "The agent stopped without handing in a plan: its answer is in ", chat, ". Tell it what "
        + "you'd like:"), redraft("Ask again"));
    } else if (c.status === "planned") {
      const plan = h("div.md.newprovider-plan");
      renderMarkdown(plan, c.plan || "");
      const approve = h("button.btn.primary", {}, icon("check", "sm"), "Approve");
      approve.onclick = act(approve, () => post("approve"));
      const deny = h("button.btn", {}, "Deny");
      deny.onclick = act(deny, () => cancel(say));
      parts.push(h("p", say.planned), plan,
        h("div.newprovider-actions", approve, h("button.btn", { onclick: () => {
          clear(redrafting, redraft("Send"));
        } }, "Redraft"), deny), redrafting);
    } else if (c.plan) {
      parts.push(h("details", h("summary", "The plan you approved"), (() => {
        const plan = h("div.md.newprovider-plan");
        renderMarkdown(plan, c.plan);
        return plan;
      })()));
    }
    return parts;
  }
  const redrafting = h("div");

  function redraft(label) {
    const text = h("textarea.textarea", { rows: 3, value: wanted, placeholder: "What should be different?",
      oninput: () => { wanted = text.value; } });
    const send = h("button.btn.primary", {}, label);
    send.onclick = act(send, async () => {
      await post("redraft", { text: text.value });
      wanted = "";
      clear(redrafting);
    });
    return h("div.newprovider-redraft", text, h("div.newprovider-actions", send));
  }

  // 3. Finalisation: Relay's check, the trial Relay, Add or Apply
  function stepThree(c, say) {
    const parts = [h("h2", number(3, c.status === "applied"), say.steps[2])];
    const review = c.review;
    if (["setup", "planning", "planned"].includes(c.status)) {
      parts.push(h("p.muted", say.waiting));
    } else if (c.status === "building" && c.working) {
      parts.push(h("p", h("span.spinner.sm"), ` ${say.working}`));
    } else if (c.status === "building" && c.checking) {
      parts.push(h("p", h("span.spinner.sm"), " Relay is checking it in a box of its own (a few real turns), "
        + "then starts a throwaway Relay with it."));
    } else if (c.status === "building") {
      const fix = h("button.btn.primary", {}, "Let the agent fix it");
      fix.onclick = act(fix, () => post("fix"));
      const anyway = h("button.btn", {}, "Try it anyway");
      anyway.onclick = act(anyway, () => post("check", { anyway: true }));
      const again = h("button.btn", {}, "Check again");
      again.onclick = act(again, () => post("check"));
      parts.push(review ? checked(review) : h("p.muted", say.nothing),
        h("div.newprovider-actions", review ? fix : null, review ? anyway : null, again));
    } else if (c.status === "trying") {
      const visit = h("a.btn.primary", { href: c.trial?.url, target: "_blank", rel: "noopener" }, icon("globe", "sm"),
        "Open the throwaway Relay");
      visit.onclick = async (e) => {  // (up again first: Relay restarted, say)
        e.preventDefault();
        const tab = window.open("", "_blank");
        try {
          creation = await api.post(`${base}/${id}/trial`, {});
          if (tab) tab.location = creation.trial.url;
        } catch (error) {
          tab?.close();
          toast(error.message, "error");
        }
      };
      const [action, label, done] = say.last;
      const finish = h("button.btn.primary", {}, icon(c.creates ? "plus" : "check", "sm"), label);
      finish.onclick = act(finish, async () => {
        await post(action);
        toast(done);
        await restartRelay();
      });
      const wrong = h("textarea.textarea", { rows: 2, value: wanted, placeholder: "What went wrong in the trial?",
        oninput: () => { wanted = wrong.value; } });
      const tell = h("button.btn", {}, "Tell the agent");
      tell.onclick = act(tell, async () => {
        await post("fix", { text: wrong.value });
        wanted = "";
      });
      parts.push(
        h("p", say.trying),
        h("div.newprovider-actions", visit),
        h("p.newprovider-url", h("code", `${location.origin}${c.trial?.url || ""}`)),
        review?.diff
          ? h("details.repair-diff", h("summary", c.creates ? "Its files" : "The change"), h("pre", review.diff))
          : null,
        h("div.newprovider-actions", finish),
        h("details.newprovider-wrong", h("summary", "Something's wrong"), wrong, h("div.newprovider-actions", tell)));
    } else if (c.status === "applied") {
      const undo = h("button.btn", {}, c.creates ? "Take it out again" : "Undo");
      undo.onclick = act(undo, async () => {
        const [title, text, confirm] = c.creates
          ? ["Take it out again?", "Its folder, its provider and its home (the program you installed, your login) go; "
            + "Relay restarts without it.", "Take it out"]
          : ["Undo the repair?", "Its originals come back; Relay restarts with them.", "Undo it"];
        if (await confirmDialog(title, text, { confirm, danger: true })) {
          await post("undo");
          await restartRelay();
        }
      });
      parts.push(c.creates
        ? (c.loaded
          ? h("p", h("strong", `Added: ${(review?.template?.name) || c.target_id}`), " is one of your providers now "
            + "(Settings → Providers).")
          : h("p", "Added. Relay loads it when it restarts."))
        : h("p", h("strong", "Applied."), ` Relay uses the repaired support for ${c.provider?.name || c.target_id} `
          + "once it restarted."),
      h("div.newprovider-actions", c.creates && !c.loaded
        ? h("button.btn.primary", { onclick: restartRelay }, "Restart Relay") : null, undo));
    } else {
      parts.push(h("p.muted", c.status === "undone" ? say.undone : "Cancelled: nothing of it is left."));
    }
    return parts;
  }

  async function cancel(say) {
    const [, title, text] = say.cancel;
    if (!await confirmDialog(title, text, { confirm: "Throw it away", danger: true })) return;
    await post("discard");
  }

  function draw(force = false) {
    const c = creation;
    // (drawn anew only when it changed: what's typed, focus and the shell stay)
    const now = JSON.stringify([c.status, c.working, c.checking, c.plan, c.trial, c.loaded, c.review?.when, !!found]);
    if (now === shown && !force) return;
    shown = now;
    const say = words(c);
    clear(top, heading(say), h("p.newprovider-intro", say.intro));
    if (c.legacy) {
      const discard = h("button.btn", {}, "Discard");
      discard.onclick = act(discard, () => post("discard"));
      clear(steps[0], h("p", say.legacy), ["discarded", "applied", "undone"].includes(c.status) ? null
        : h("div.newprovider-actions", discard));
      clear(steps[1]);
      clear(steps[2]);
      clear(foot);
      return;
    }
    clear(steps[0], stepOne(c, say));
    clear(steps[1], stepTwo(c, say));
    clear(steps[2], stepThree(c, say));
    steps.forEach((step, i) => step.classList.toggle("current", i === stepIndex(c)));
    const going = ["setup", "planning", "planned", "building", "trying"].includes(c.status);
    const stop = h("button.btn.ghost.small", {}, say.cancel[0]);
    stop.onclick = act(stop, () => cancel(say));
    clear(foot, going ? stop : null);
  }

  async function refresh() {
    try {
      creation = await api.get(`${base}/${id}`);
    } catch (error) {
      clear(el, h("p.error-text", error.message));
      return;
    }
    if (!creation.creates && creation.status === "setup" && !found) {  // (a repair's first step: what's wrong)
      api.get(`/api/providers/${encodeURIComponent(creation.target_id)}/trouble`)
        .then((answer) => { found = answer; if (!closed) draw(); }).catch(() => {});
    }
    if (!closed) draw();
  }

  refresh();
  timer = setInterval(() => { if (!document.hidden && !closed) refresh(); }, POLL);
  return {
    el,
    refresh,
    close() {
      closed = true;
      clearInterval(timer);
      shell?.close();
    },
  };
}

function heading(say) {
  const [iconName, title] = say.heading;
  return h("div.pane-head", h("div.grow", h("h1", icon(iconName), title)));
}

function number(n, done) {
  return h(`span.newprovider-number${done ? ".done" : ""}`, done ? icon("check", "sm") : String(n));
}

// Relay's check of it, as it went
function checked(review) {
  const test = review.test || {};
  return h("div",
    h("p", test.ok ? "Relay's check of it passed." : "Relay's check of it failed:"),
    test.results?.length ? h("ul.repair-facts", test.results.map((r) =>
      h(`li.${r.ok ? "ok" : r.ok === false ? "fail" : "muted"}`,
        `${r.ok ? "✓" : r.ok === false ? "✗" : "–"} ${r.name}: `
        + `${r.detail || ""}`))) : null,
    test.error || review.trouble ? h("p.error-text", test.error || review.trouble) : null,
    review.compile_errors?.length
      ? h("p.error-text", `It doesn't compile: ${review.compile_errors.join("; ")}`) : null);
}

function stepIndex(c) {
  return c.status === "setup" ? 0 : ["planning", "planned"].includes(c.status) ? 1 : 2;
}

function stepOf(c) {
  return ["Installing it", "Its plan", "Building and trying it"][stepIndex(c)];
}
