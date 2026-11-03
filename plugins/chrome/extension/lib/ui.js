// Small DOM helpers for the side panel and the settings page.

// h("button.btn.primary", { onclick, title }, "Send"): an element; children may be text, elements, arrays, null.
export function h(spec, ...rest) {
  const [tag, ...classes] = spec.split(".");
  const el = document.createElement(tag || "div");
  if (classes.length) el.className = classes.join(" ");
  const attrs = rest[0] && typeof rest[0] === "object" && !(rest[0] instanceof Node) && !Array.isArray(rest[0])
    ? rest.shift() : null;
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, rest);
  return el;
}

// An element's children replaced, as h() takes them (null and false left out, where replaceChildren writes "null").
export function fill(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(el, child);
    else el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

const PATHS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  external: '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  settings: '<path d="M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3M14 2v4M8 10v4M16 18v4"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  page:
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 '
    + '2v6h6M16 13H8M16 17H8M10 9H8"/>',
  camera:
    '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 '
    + '0-2-2h-3z"/><circle cx="12" cy="13" r="3"/>',
  send: '<path d="m5 12 7-7 7 7M12 19V5"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" stroke="none"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  tool:
    '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 '
    + '1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  link:
    '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 '
    + '5 0 0 0 7.07 7.07l1.71-1.71"/>',
  quote: '<path d="M17 6H3M21 12H8M21 18H8M3 12v6"/>',
  image:
    '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 '
    + '15-3.09-3.09a2 2 0 0 0-2.82 0L6 21"/>',
  alert: '<circle cx="12" cy="12" r="10"/><path d="M12 8v4M12 16h.01"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  back: '<path d="m15 18-6-6 6-6"/>',
  play: '<path d="M7 4v16l13-8z" fill="currentColor" stroke="none"/>',
  terminal: '<path d="m4 17 6-6-6-6M12 19h8"/>',
  sparkle: '<path d="M12 3l1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2z"/>',
  globe:
    '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 '
    + '1-4-10 15.3 15.3 0 0 1 4-10z"/>',
};

export function icon(name, size = 16) {
  const span = document.createElement("span");
  span.className = "icon";
  span.innerHTML = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" `
    + `stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${PATHS[name] || ""}</svg>`;
  return span;
}

// <span data-icon="plus"></span> in the HTML, filled in.
export function fillIcons(root = document) {
  for (const el of root.querySelectorAll("[data-icon]")) el.replaceWith(icon(el.dataset.icon,
    Number(el.dataset.size) || 16));
}

// "5 min ago" for a time Relay gives (Unix seconds or milliseconds, or ISO text); "" when it isn't one.
export function ago(when) {
  if (when === null || when === undefined || when === "") return "";
  const ms = typeof when === "number" ? (when < 1e12 ? when * 1000 : when) : Date.parse(when);
  if (!Number.isFinite(ms)) return "";
  const seconds = Math.max(0, (Date.now() - ms) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  if (seconds < 7 * 86400) return `${Math.floor(seconds / 86400)} d ago`;
  return new Date(ms).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
