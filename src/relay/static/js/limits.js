// Usage limits (GET /api/usage/limits): what's left of each provider's windows, asked with its own login or as
// its last run told. Settings → Usage draws them all; the activity bar's ring shows the tightest, and a click on
// it all of them.

import { api } from "./api.js";
import { clear, clock, h, icon, timeAgo } from "./ui.js";

const EVERY = 5 * 60 * 1000;  // (the server keeps each provider's answer 2 minutes)
const SVG = "http://www.w3.org/2000/svg";

// What's left of a window, 0–1: one that reset since what was told has all of it again
function windowLeft(w, now = Date.now() / 1000) {
  return w.resets_at && w.resets_at <= now ? 1 : Math.max(0, Math.min(1, 1 - (w.used ?? 0)));
}

// The window with the least left across all logins, as {row, window, left}; null when none tells one
function tightest(rows, now = Date.now() / 1000) {
  let found = null;
  for (const row of rows || []) {
    for (const w of row.windows || []) {
      const left = windowLeft(w, now);
      if (!found || left < found.left) found = { row, window: w, left };
    }
  }
  return found;
}

function tone(left) {
  return left <= 0 ? "bad" : left < 0.2 ? "warn" : "ok";
}

function resets(moment) {
  if (!moment) return null;
  const now = Date.now() / 1000;
  if (moment <= now) return "reset since";
  return `resets ${moment - now < 6 * 86400 ? clock(moment)
    : new Date(moment * 1000).toLocaleDateString([], { day: "numeric", month: "short" })}`;
}

function leftText(w) {
  return w.left ? `${w.left} left` : `${Math.round(windowLeft(w) * 100)}% left`;
}

function meter(w) {
  const percent = Math.round(windowLeft(w) * 100);
  const fill = h("span");
  fill.style.width = `${percent}%`;  // CSSOM: allowed by the CSP
  return h(`div.limit-window.${tone(windowLeft(w))}`,
    h("div.limit-label", h("span", w.label),
      h("span.muted", [leftText(w), resets(w.resets_at)].filter(Boolean).join(" · "))),
    h("div.limit-meter",
      { role: "meter", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(percent),
        "aria-label": `${w.label}: ${percent}% left` }, fill));
}

function said(r) {
  return r.error ? r.error
    : r.source === "last run" ? `as its last run told, ${timeAgo(r.checked)}${r.note ? ` (now: ${r.note})` : ""}`
      : `asked ${timeAgo(r.checked)}`;
}

// One login's windows, as meters (Settings → Usage and the ring's popover)
export function limitRow(r) {
  return h("div.limit-row",
    h("div.limit-head", h("strong", r.name),
      r.service || r.plan ? h("span.badge", [r.service, r.plan].filter(Boolean).join(" ")) : null,
      r.also.length ? h("span.muted", `also ${r.also.join(", ")}`) : null, h("span.muted.limit-when", said(r))),
    r.windows.length ? h("div.limit-windows", r.windows.map(meter))
      : r.error ? null : h("p.muted", "Its plan has no limits it tells."));
}

function svg(tag, attrs) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  return el;
}

// The activity bar's ring: what's left of the tightest window (hidden while no provider tells one). It looks at
// the start, every few minutes while the tab is in view, when it comes back into view, and when refresh() is
// called (a run ended). A click opens every login's windows beside it.
export function usageIndicator({ onOpenUsage } = {}) {
  const arc = svg("circle", { class: "usage-arc", cx: "12", cy: "12", r: "9", pathLength: "100",
    transform: "rotate(-90 12 12)" });
  const ring = svg("svg", { class: "usage-ring", viewBox: "0 0 24 24", "aria-hidden": "true" });
  ring.append(svg("circle", { class: "usage-track", cx: "12", cy: "12", r: "9" }), arc);
  const el = h("button.bar-item.usage-item", { type: "button", hidden: true, title: "Usage limits",
    "aria-label": "Usage limits", "aria-haspopup": "dialog", "aria-expanded": "false",
    onclick: () => (popover ? close() : open()) }, ring);
  let rows = [];
  let asked = 0;
  let loading = null;
  let asking = 0;
  let failed = null;
  let popover = null;
  let body = null;

  function draw() {
    const found = tightest(rows);
    el.hidden = !found;
    el.classList.remove("ok", "warn", "bad");
    if (found) {
      const percent = Math.round(found.left * 100);
      el.classList.add(tone(found.left));
      arc.setAttribute("stroke-dasharray", `${percent} 100`);
      arc.toggleAttribute("hidden", percent === 0);
      const label = `Usage limits: ${found.row.name} · ${found.window.label}: `
        + [leftText(found.window), resets(found.window.resets_at)].filter(Boolean).join(", ");
      el.title = label;
      el.setAttribute("aria-label", label);
    }
    if (body) {
      clear(body, failed ? h("p.error-text", failed)
        : !asked ? h("p.muted", "Asking the providers…")
          : rows.length ? rows.map(limitRow) : h("p.muted", "None of your providers tells its limits."));
    }
  }

  function refresh(fresh = false) {
    if (loading && !fresh) return loading;
    const mine = ++asking;  // (Refresh while it asks: the answer to the last question counts)
    loading = (async () => {
      let found;
      let error = null;
      try {
        found = await api.get(`/api/usage/limits${fresh ? "?fresh=1" : ""}`);
      } catch (trouble) {
        error = trouble.message;  // (the ring keeps what it showed)
      }
      if (mine !== asking) return;
      if (found) rows = found;
      failed = error;
      asked = Date.now();
      loading = null;
      draw();
    })();
    return loading;
  }

  function place() {
    const rect = el.getBoundingClientRect();
    const left = Math.min(rect.right + 8, window.innerWidth - popover.offsetWidth - 8);
    const top = Math.min(rect.bottom - popover.offsetHeight, window.innerHeight - popover.offsetHeight - 8);
    popover.style.left = `${Math.max(8, left)}px`;
    popover.style.top = `${Math.max(8, top)}px`;
  }

  const outside = (e) => { if (!popover?.contains(e.target) && !el.contains(e.target)) close(); };
  const keyboard = (e) => {
    if (e.key !== "Escape") return;
    e.preventDefault();
    e.stopImmediatePropagation();
    close();
    el.focus({ preventScroll: true });
  };

  function open() {
    body = h("div.usage-limits");
    const again = h("button.btn.small", { type: "button", title: "Ask the providers anew",
      onclick: async () => { again.disabled = true; await refresh(true); again.disabled = false; } },
    icon("refresh", "sm"), "Refresh");
    popover = h("div.usage-popover", { role: "dialog", "aria-label": "Usage limits" },
      h("div.limit-tools", h("h4", "Usage limits"), again), body,
      h("button.link-btn.usage-more", { type: "button", onclick: () => { close(); onOpenUsage?.(); } },
        "All of it in Settings → Usage"));
    draw();
    document.body.append(popover);
    place();
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", keyboard, true);
    el.setAttribute("aria-expanded", "true");
    el.classList.add("active");
    again.focus({ preventScroll: true });
    if (Date.now() - asked > 60_000) refresh().then(() => popover && place());
  }

  function close() {
    if (!popover) return;
    popover.remove();
    popover = body = null;
    document.removeEventListener("mousedown", outside);
    document.removeEventListener("keydown", keyboard, true);
    el.setAttribute("aria-expanded", "false");
    el.classList.remove("active");
  }

  const timer = setInterval(() => { if (!document.hidden) refresh(); }, EVERY);
  const shown = () => { if (!document.hidden && Date.now() - asked > 60_000) refresh(); };
  document.addEventListener("visibilitychange", shown);
  refresh();
  return {
    el,
    refresh: () => refresh(),
    stop() {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", shown);
      close();
    },
  };
}
