// Markdown to safe HTML, with highlighted code blocks and copy buttons.

import { BASE } from "./base.js";
import { copyText, h, icon } from "./ui.js";

let configured = false;
let server = { port: null, addresses: [] };  // (Relay's port and this machine's addresses: /api/state)
const LOOPBACK = /^(localhost|127(\.\d{1,3}){3}|0\.0\.0\.0|\[::1?\])$/i;

export function setServer(found) {
  if (found) server = { port: found.port, addresses: found.addresses || [] };
}

// A link to a server on this machine (an agent's http://localhost:5173/) where the user can open it:
// /port/<n>/ on Relay's own address (relay.forward); Relay's own port is Relay. Null: leave it.
export function serverLink(href, here) {
  let url;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (!/^https?:$/.test(url.protocol) || !url.port) return null;
  const host = url.hostname;
  if (!LOOPBACK.test(host) && !(here.addresses || []).includes(host) && host !== here.hostname) return null;
  const rest = `${url.pathname}${url.search}${url.hash}`;
  return Number(url.port) === here.port ? `${here.origin}${rest}` : `${here.origin}/port/${url.port}${rest}`;
}

function here() {
  return { origin: location.origin + BASE, hostname: location.hostname, ...server };  // (Relay's address: its base too)
}

function configure() {
  if (configured || !window.marked || !window.DOMPurify) return;
  window.marked.setOptions({ gfm: true, breaks: false });
  window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "A") {
      node.setAttribute("target", "_blank");
      node.setAttribute("rel", "noopener noreferrer");
      const href = node.getAttribute("href") || "";
      const better = serverLink(href, here());
      if (better) {
        if (node.textContent.trim() === href) node.textContent = better;  // (a bare link: its text too)
        node.setAttribute("href", better);
      }
    }
  });
  configured = true;
}

// `http://localhost:5173` written as code: a link too, where the user can open it.
function serverCode(root) {
  for (const code of root.querySelectorAll("code")) {
    if (code.closest("pre, a")) continue;
    const text = code.textContent.trim();
    const better = /^https?:\/\/\S+$/.test(text) ? serverLink(text, here()) : null;
    if (!better) continue;
    const link = h("a", { href: better, target: "_blank", rel: "noopener noreferrer", title: better });
    code.replaceWith(link);
    link.append(code);
  }
}

export function renderMarkdown(target, text) {
  configure();
  if (!window.marked || !window.DOMPurify) {
    target.textContent = text;
    return;
  }
  const html = window.DOMPurify.sanitize(window.marked.parse(text), {
    FORBID_TAGS: ["style", "form", "input", "button", "iframe"],
    FORBID_ATTR: ["style"],
  });
  target.innerHTML = html;
  enhanceCode(target);
  serverCode(target);
  // A table fits its cells to the column (wrapping them) and scrolls only when it can't.
  for (const table of target.querySelectorAll("table")) {
    const wrap = h("div.table-wrap");
    table.replaceWith(wrap);
    wrap.append(table);
    const more = () => wrap.classList.toggle("more-right", wrap.scrollLeft + wrap.clientWidth < wrap.scrollWidth - 2);
    wrap.addEventListener("scroll", more, { passive: true });
    new ResizeObserver(more).observe(wrap);
  }
}

function enhanceCode(root) {
  for (const code of root.querySelectorAll("pre > code")) {
    const pre = code.parentElement;
    const language = (code.className.match(/language-([\w+#-]+)/) || [])[1] || "";
    if (window.hljs) {
      try {
        if (language && window.hljs.getLanguage(language)) window.hljs.highlightElement(code);
      } catch { /* leave it plain */ }
    }
    const copy = h("button.copy-btn", { onclick: () => copyText(code.textContent) },
      icon("copy", "sm"), "Copy");
    const block = h("div.codeblock", h("div.codeblock-head", h("span", language || "text"), copy));
    pre.replaceWith(block);
    block.append(pre);
  }
}
