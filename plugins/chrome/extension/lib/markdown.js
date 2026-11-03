// Markdown to safe HTML, as Relay's own pages make it (the same marked and DOMPurify, vendor/).

let relayAddress = null;  // (parseAddress's: links to Relay's pages and to servers on Relay's machine)
const LOOPBACK = /^(localhost|127(\.\d{1,3}){3}|0\.0\.0\.0|\[::1?\])$/i;

export function setRelay(address) {
  relayAddress = address;
}

// A link an agent wrote: /c/… is one of Relay's pages; http://localhost:5173/ a server on Relay's machine,
// which you reach at /port/5173/ on Relay's address (as Relay's pages show it).
function better(href) {
  if (!relayAddress) return null;
  if (href.startsWith("/") && !href.startsWith("//")) return `${relayAddress.base}${href}`;
  let url;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (!/^https?:$/.test(url.protocol) || !url.port || !LOOPBACK.test(url.hostname)) return null;
  const rest = `${url.pathname}${url.search}${url.hash}`;
  return Number(url.port) === relayAddress.port ? `${relayAddress.base}${rest}`
    : `${relayAddress.base}/port/${url.port}${rest}`;
}

let configured = false;

function configure() {
  if (configured) return;
  window.marked.setOptions({ gfm: true, breaks: false });
  window.DOMPurify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName !== "A") return;
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
    const href = node.getAttribute("href") || "";
    const found = better(href);
    if (found) {
      if (node.textContent.trim() === href) node.textContent = found;
      node.setAttribute("href", found);
    }
  });
  configured = true;
}

export function renderMarkdown(target, text) {
  if (!window.marked || !window.DOMPurify) {
    target.textContent = text;
    return;
  }
  configure();
  target.innerHTML = window.DOMPurify.sanitize(window.marked.parse(text), {
    FORBID_TAGS: ["style", "form", "input", "button", "iframe"],
    FORBID_ATTR: ["style"],
  });
  for (const code of target.querySelectorAll("pre > code")) {
    const pre = code.parentElement;
    const language = (code.className.match(/language-([\w+#-]+)/) || [])[1] || "text";
    const copy = document.createElement("button");
    copy.className = "copy";
    copy.textContent = "Copy";
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText(code.textContent);
      copy.textContent = "Copied";
      setTimeout(() => { copy.textContent = "Copy"; }, 1500);
    });
    const head = document.createElement("div");
    head.className = "code-head";
    const name = document.createElement("span");
    name.textContent = language;
    head.append(name, copy);
    const wrap = document.createElement("div");
    wrap.className = "code";
    pre.replaceWith(wrap);
    wrap.append(head, pre);
  }
  for (const table of target.querySelectorAll("table")) {
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    table.replaceWith(wrap);
    wrap.append(table);
  }
}
