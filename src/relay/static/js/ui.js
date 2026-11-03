// Small DOM helpers, icons, menus, modals and toasts.

const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
  bell: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0"/>',
  paperclip: '<path d="m21 11-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L15 7"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  sidebar: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 '
    + '1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 '
    + '1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 '
    + '0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 '
    + '0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 '
    + '1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  send: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  stop: '<rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" stroke="none"/>',
  play: '<path d="M8 5.5v13a1 1 0 0 0 1.5.9l10-6.5a1 1 0 0 0 0-1.7l-10-6.5A1 1 0 0 0 8 5.5Z" '
    + 'fill="currentColor" stroke="none"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 '
    + '0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  chevronRight: '<path d="m9 6 6 6-6 6"/>',
  folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
  folderPlus: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 '
    + '1-2-2Z"/><path d="M12 11v6M9 14h6"/>',
  terminal: '<path d="m4 17 6-6-6-6M12 19h8"/>',
  file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 3v6h6"/>',
  undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  fork: '<circle cx="6" cy="5" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="12" cy="19" r="2"/><path d="M6 7v1a3 3 '
    + '0 0 0 3 3h6a3 3 0 0 0 3-3V7M12 11v6"/>',
  edit: '<path d="M12 20h9M16.5 3.5a2.1 2.1 0 1 1 3 3L7 19l-4 1 1-4Z"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
  pin: '<path d="M12 17v5M9 3h6l-1 7 4 3v2H6v-2l4-3Z"/>',
  more: '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 '
    + '19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
  code: '<path d="m16 18 6-6-6-6M8 6l-6 6 6 6"/>',
  chat: '<path d="M21 12a8 8 0 0 1-11.8 7L3 21l2-6.2A8 8 0 1 1 21 12Z"/>',
  temporary: '<path d="M21 12a8 8 0 0 1-11.8 7L3 21l2-6.2A8 8 0 1 1 21 12Z" stroke-dasharray="3.2 2.6"/>',
  alert: '<circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/>',
  arrowLeft: '<path d="M19 12H5M12 19l-7-7 7-7"/>',
  plug: '<path d="M9 2v6M15 2v6M6 8h12v4a6 6 0 0 1-12 0Z"/><path d="M12 18v4"/>',
  puzzle: '<path d="M10 4h4v2.5a1.5 1.5 0 1 0 3 0V4h3v6h-2.5a1.5 1.5 0 1 0 0 3H20v7h-6v-2.5a1.5 1.5 0 1 0-3 '
    + '0V20H4v-6h2.5a1.5 1.5 0 1 0 0-3H4V4h6Z"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>',
  tool: '<path d="M14.7 6.3a4 4 0 0 0-5.6 5.3L3 17.7 6.3 21l6.1-6.1a4 4 0 0 0 5.3-5.6l-2.5 2.5-2.8-.7-.7-2.8Z"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  shield: '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6Z"/>',
  brain: '<path d="M9 4a3 3 0 0 0-3 3v.5A3 3 0 0 0 4 10.3 3 3 0 0 0 5 15a3 3 0 0 0 4 4 3 3 0 0 0 3-3V7a3 3 0 0 '
    + '0-3-3ZM15 4a3 3 0 0 1 3 3v.5a3 3 0 0 1 2 2.8A3 3 0 0 1 19 15a3 3 0 0 1-4 4 3 3 0 0 1-3-3"/>',
  hammer: '<path d="m15 12-8.4 8.4a2.1 2.1 0 1 1-3-3L12 9"/><path d="m18 15 4-4"/><path d="m21.5 11.5-1.9-1.9a2 2 0 0 '
    + '1-.6-1.4V7l-2.3-2.3a6 6 0 0 0-4.2-1.7H9l.9.8A6.2 6.2 0 0 1 12 8.4V10l2 2h1.2a2 2 0 0 1 1.4.6l1.9 1.9"/>',
  map: '<path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2Z"/><path d="M9 4v14M15 6v14"/>',
  bolt: '<path d="M13 2 4 14h7l-1 8 9-12h-7Z"/>',
  bulb: '<path d="M9 18h6M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 '
    + '2.1V18h6v-1.2c0-.8.4-1.6 1-2.1A7 7 0 0 0 12 2Z"/>',
  flask: '<path d="M9 3h6M10 3v6l-5.5 9.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3M7 15h10"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5ZM4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
  bug: '<rect x="7" y="7" width="10" height="13" rx="5"/><path d="M9 7a3 3 0 0 1 6 0M12 11v9M3 13h4M17 13h4M4 7l3 '
    + '2M20 7l-3 2M4 20l3-2M20 20l-3-2"/>',
  rocket: '<path d="M5 15c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2.1-.1-2.9a2.2 2.2 0 0 0-2.9-.1Z"/><path d="m12 '
    + '15-3-3a22 22 0 0 1 2-4A12.9 12.9 0 0 1 22 2c0 2.7-.8 7.5-6 11a22.4 22.4 0 0 1-4 2Z"/><path d="M9 12H4s.6-3 '
    + '2-4c1.6-1.1 5 0 5 0M12 15v5s3-.6 4-2c1.1-1.6 0-5 0-5"/>',
  star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3l-5.6 2.9 1.1-6.2L3 9.6l6.2-.9Z"/>',
  eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  box: '<path d="M21 8 12 3 3 8v8l9 5 9-5Z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
  compass: '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5Z"/>',
  bot: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9.5 17h5"/>',
  fileCode: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Z"/><path d="M14 '
    + '3v6h6M10 13l-2 2 2 2M14 13l2 2-2 2"/>',
  sparkles: '<path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8Z"/><path '
    + 'd="M19 3v4M17 5h4M5 17v4M3 19h4"/>',
  reply: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
};

// The icons a mode can have (Settings → Modes).
export const MODE_ICONS = ["hammer", "tool", "code", "terminal", "edit", "map", "shield", "eye", "search", "compass",
  "bolt", "rocket", "bulb", "brain", "sparkles", "chat", "bug", "flask", "target", "book", "star", "globe"];

export function icon(name, cls = "") {
  const span = document.createElement("span");
  span.innerHTML = `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
  return span.firstChild;
}

// h("div.card#main", {onclick, title}, child, "text", [more children])
export function h(spec, attrs, ...children) {
  if (attrs instanceof Node || typeof attrs === "string" || Array.isArray(attrs)) {
    children.unshift(attrs);
    attrs = {};
  }
  const [tag, ...rest] = spec.split(/(?=[.#])/);
  const el = document.createElement(tag || "div");
  for (const part of rest) {
    if (part.startsWith(".")) el.classList.add(part.slice(1));
    else el.id = part.slice(1);
  }
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className += " " + value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key === "value") el.value = value;
    else if (key in el && typeof value !== "string") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

// A changed list may move a row, but should not take away its keyboard focus.
export function clearKeepingFocus(el, ...children) {
  const active = el.contains(document.activeElement) ? document.activeElement : null;
  const key = active?.dataset.focus;
  const top = el.scrollTop;
  clear(el, children);
  if (key) [...el.querySelectorAll("[data-focus]")]
    .find((node) => node.dataset.focus === key)?.focus({ preventScroll: true });
  el.scrollTop = top;
  return el;
}

export function toast(message, kind = "info", action = null) {
  const el = h(`div.toast${kind === "error" ? ".error" : ""}`, h("span", message),
    action ? h("button.toast-action", { onclick: () => { el.remove(); action.onclick(); } }, action.label) : null);
  document.getElementById("toasts").append(el);
  setTimeout(() => el.remove(), action ? 8000 : kind === "error" ? 5000 : 2500);
}

export async function copyText(text, message = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Clipboard API needs HTTPS; fall back for plain-HTTP LAN use.
    const area = h("textarea", { value: text });
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast(message);
}

let openMenu = null;

export function closeMenu() {
  if (openMenu) {
    openMenu.close();
    openMenu = null;
  }
}

// items: [{label, icon, onclick, danger}] or "-" for a separator
// A model's or provider's health: "ok" (worked last time), "fail", or null (not known).
export function statusDot(status, title = null) {
  return h(`span.status-dot.${status}`, { title: title || (status === "ok" ? "Worked last time" : "Failed last time"),
    "aria-label": status === "ok" ? "works" : "failed" });
}

// items: [{ label, icon?, status?, title?, danger?, onclick, submenu? }] or "-". A submenu
// (a function returning items, or a promise of them) opens beside its item on hover,
// or with the › button on a touch screen.
export function showMenu(anchor, items, { align = "left", onClose, className = "", focus = true } = {}) {
  closeMenu();
  let sub = null;
  let pending = null;
  let latest = 0; // only the last submenu asked for opens
  let subAnchor = null;
  const subItems = new Map();
  const closeSub = () => { clearTimeout(pending); latest += 1; sub?.remove(); sub = null; };
  const openSub = async (button, item, focusSub = false) => {
    closeSub();
    const asked = latest;
    const entries = await item.submenu();
    if (!entries?.length) button.querySelector(".submenu-open")?.remove(); // nothing there after all
    if (!menu.isConnected || asked !== latest || !entries?.length) return;
    sub = buildMenu(entries, closeMenu);
    subAnchor = button;
    sub.classList.add("submenu");
    document.body.append(sub);
    const rect = button.getBoundingClientRect();
    const left = rect.right + sub.offsetWidth + 4 <= window.innerWidth ? rect.right + 4
      : rect.left - sub.offsetWidth - 4;
    sub.style.left = `${Math.max(8, left)}px`;
    sub.style.top = `${Math.max(8, Math.min(rect.top - 6, window.innerHeight - sub.offsetHeight - 8))}px`;
    if (focusSub) sub.querySelector("button")?.focus();
  };
  const menu = buildMenu(items, closeMenu, (button, item) => {
    button.addEventListener("mouseenter", () => {
      clearTimeout(pending);
      if (item.submenu) pending = setTimeout(() => openSub(button, item), 150);
      else closeSub();
    });
    if (item.submenu) {
      subItems.set(button, item);
      button.setAttribute("aria-haspopup", "menu");
      button.append(h("span.submenu-open",
        { title: "More", onclick: (e) => { e.stopPropagation(); openSub(button, item); } },
        icon("chevronRight", "sm")));
    }
  });
  if (className) menu.classList.add(className);
  document.body.append(menu);
  const rect = anchor.getBoundingClientRect();
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  let left = align === "right" ? rect.right - width : rect.left;
  let top = rect.bottom + 4;
  if (top + height > window.innerHeight - 8) top = rect.top - height - 4;
  left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
  menu.style.left = `${left}px`;
  menu.style.top = `${Math.max(8, top)}px`;
  const outside = (e) => { if (!menu.contains(e.target) && !sub?.contains(e.target)) closeMenu(); };
  const keyboard = (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopImmediatePropagation();
      closeMenu();
      return;
    }
    const active = document.activeElement;
    const current = sub?.contains(active) ? sub : menu.contains(active) ? menu : null;
    if (!current) return;  // Slash suggestions leave typing and completion to the composer.
    if (e.key === "Tab") { closeMenu(); return; }
    if (e.key === "ArrowRight" && subItems.has(active)) {
      e.preventDefault();
      openSub(active, subItems.get(active), true);
    } else if (e.key === "ArrowLeft" && current === sub) {
      e.preventDefault();
      closeSub();
      subAnchor?.focus();
    } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
      e.preventDefault();
      const buttons = [...current.querySelectorAll("button:not(:disabled)")];
      const index = buttons.indexOf(active);
      const next = e.key === "Home" ? 0 : e.key === "End" ? buttons.length - 1
        : (index + (e.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next]?.focus();
    }
  };
  const outsideTimer = setTimeout(() => document.addEventListener("mousedown", outside));
  document.addEventListener("keydown", keyboard);
  anchor.setAttribute("aria-expanded", "true");
  anchor.setAttribute("aria-haspopup", "menu");
  openMenu = {
    anchor,
    close() {
      const restore = menu.contains(document.activeElement) || sub?.contains(document.activeElement);
      closeSub();
      menu.remove();
      clearTimeout(outsideTimer);
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("keydown", keyboard);
      anchor.setAttribute("aria-expanded", "false");
      if (restore && anchor.isConnected) anchor.focus({ preventScroll: true });
      onClose?.();
    },
  };
  if (focus) menu.querySelector("button")?.focus();
}

function buildMenu(items, close, decorate = null) {
  return h("div.menu", { role: "menu" }, items.map((item) => {
    if (item === "-") return h("hr");
    const button = h(`button${item.danger ? ".danger" : ""}`, {
      role: "menuitem", title: item.title || null,
      onclick: (e) => { e.stopPropagation(); close(); item.onclick(); },
    }, item.icon ? icon(item.icon, "sm") : null, h("span.grow", item.label),
    item.hint ? h("span.menu-hint", item.hint) : null, item.status ? statusDot(item.status) : null);
    decorate?.(button, item);
    return button;
  }));
}

// Returns {el, body, foot, close}. close() resolves `closed`.
const modals = [];
const closers = new Map();  // (each open modal's close())

// Every dialog that's open closes (signed out: what they showed isn't the next user's)
export function closeModals() {
  for (const box of [...modals].reverse()) closers.get(box)?.();
}

export function modal(title, { wide = false, onClose } = {}) {
  const previous = document.activeElement;
  const body = h("div.modal-body");
  const foot = h("div.modal-foot");
  const box =
    h(`div.modal${wide ? ".wide" : ""}`, { role: "dialog", "aria-modal": "true", "aria-label": title, tabindex: "-1" },
      h("div.modal-head", h("h2", title),
        h("button.icon-btn", { title: "Close", onclick: () => close() }, icon("x"))),
      body, foot);
  const backdrop = h("div.modal-backdrop", box);
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
  const keyboard = (e) => {
    if (modals.at(-1) !== box || e.defaultPrevented) return;
    if (e.key === "Escape" && !openMenu) { e.preventDefault(); close(); }
    if (e.key !== "Tab") return;
    if (openMenu) closeMenu();
    const controls = [...box.querySelectorAll('button, input, textarea, select, a[href], summary, [tabindex]')]
      .filter((node) => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length);
    const first = controls[0] || box, last = controls.at(-1) || box;
    if (!box.contains(document.activeElement)
      || (e.shiftKey ? document.activeElement === first : document.activeElement === last)) {
      e.preventDefault();
      (e.shiftKey ? last : first).focus();
    }
  };
  document.addEventListener("keydown", keyboard);
  document.body.append(backdrop);
  modals.push(box);
  closers.set(box, close);
  box.querySelector("button").focus();
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    if (openMenu && box.contains(openMenu.anchor)) closeMenu();
    const restore = box.contains(document.activeElement) || document.activeElement === document.body;
    backdrop.remove();
    modals.splice(modals.indexOf(box), 1);
    closers.delete(box);
    document.removeEventListener("keydown", keyboard);
    if (restore && previous?.isConnected) previous.focus({ preventScroll: true });
    onClose?.();
  }
  return { el: box, body, foot, close };
}

// Resolves true, false (cancelled) or "alternative" when that button was chosen. With `focusCancel`, Enter doesn't
// confirm it.
export function confirmDialog(title, message,
  { confirm = "Confirm", danger = false, alternative = null, focusCancel = false } = {}) {
  return new Promise((resolve) => {
    let answered = false;
    const m = modal(title, { onClose: () => { if (!answered) resolve(false); } });
    m.body.append(h("p.dialog-text", message));
    const answer = (value) => () => { answered = true; m.close(); resolve(value); };
    const ok = h(`button.btn${danger ? ".danger.solid" : ".primary"}`, { onclick: answer(true) }, confirm);
    const cancel = h("button.btn", { onclick: () => m.close() }, "Cancel");
    // (the DOM's append shows a null as "null")
    append(m.foot, [cancel, alternative ? h("button.btn", { onclick: answer("alternative") }, alternative) : null, ok]);
    (focusCancel ? cancel : ok).focus();
  });
}

export function promptDialog(title, { label = "", value = "", placeholder = "", confirm = "Save", note = "" } = {}) {
  return new Promise((resolve) => {
    let answered = false;
    const m = modal(title, { onClose: () => { if (!answered) resolve(null); } });
    const input = h("input.input", { value, placeholder });
    const submit = () => { answered = true; m.close(); resolve(input.value); };
    // (its Enter is used up here: focus goes back to the button that opened it, which Enter would press again)
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    if (note) m.body.append(h("p.muted", note));
    m.body.append(h("label.field", label ? h("span", label) : null, input));
    m.foot.append(h("button.btn", { onclick: () => m.close() }, "Cancel"),
      h("button.btn.primary", { onclick: submit }, confirm));
    input.focus();
    input.select();
  });
}

export function relativeGroup(timestamp) {
  const date = new Date(timestamp * 1000);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.floor((today - new Date(date.getFullYear(), date.getMonth(), date.getDate())) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return date.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

export function timeAgo(timestamp) {
  const seconds = Math.max(0, Date.now() / 1000 - timestamp);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return new Date(timestamp * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export function initials(name) {
  return (name || "?").trim().slice(0, 1).toUpperCase();
}

export function shortPath(path) {
  if (!path) return "";
  const parts = path.split("/").filter(Boolean);
  return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : path;
}

// "15:00" today, "Thu 15:00" later on.
export function clock(seconds) {
  const when = new Date(seconds * 1000);
  const time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return when.toDateString() === new Date().toDateString() ? time
    : `${when.toLocaleDateString([], { weekday: "short" })} ${time}`;
}

// fn, called only once the calls have stopped coming for `ms` milliseconds.
export function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}
