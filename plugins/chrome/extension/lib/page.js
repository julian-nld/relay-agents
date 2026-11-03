// What goes along from the browser: a page's text, a selection, a link, a screenshot.

// Runs in the page itself (chrome.scripting), so it may use nothing from outside.
function readPage(limit) {
  const selection = String(window.getSelection() || "").trim();
  const main = document.querySelector("main, article, [role=main]");
  const root = main && main.innerText.trim().length > 500 ? main : document.body;
  let text = (root ? root.innerText : "").replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  const truncated = text.length > limit;
  if (truncated) text = text.slice(0, limit);
  const description = document.querySelector('meta[name="description"]')?.content || "";
  return { url: location.href, title: document.title, description, selection, text, truncated };
}

// A tab's page, read. Its error says `blocked` when the page may be read once allowed, `never` when it can't be.
export async function readTab(tabId, limit) {
  try {
    const [found] = await chrome.scripting.executeScript({ target: { tabId }, func: readPage, args: [limit] });
    if (!found?.result) throw new Error("The page gave nothing back");
    return found.result;
  } catch (error) {
    throw blocked(error);
  }
}

export async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab || null;
}

export async function screenshot(windowId) {
  try {
    const url = await chrome.tabs.captureVisibleTab(windowId, { format: "png" });
    return await (await fetch(url)).blob();
  } catch (error) {
    throw blocked(error);
  }
}

function blocked(error) {
  const message = String(error?.message || error);
  const made = new Error(message);
  // Chrome's own pages, the Web Store and the like are never readable; another page is, once allowed.
  made.never =
    /chrome:\/\/|chrome-extension:\/\/|extensions gallery|webstore|devtools:|edge:\/\/|about:/i.test(message);
  made.blocked = !made.never && /permission|cannot access|host|activeTab|all_urls/i.test(message);
  return made;
}

// Whether the extension may read any page (asked for once, from a click: Chrome wants a user's gesture).
export function mayReadPages() {
  return chrome.permissions.contains({ origins: ["<all_urls>"] });
}

export function askToReadPages() {
  return chrome.permissions.request({ origins: ["<all_urls>"] });
}

// The message as it goes to the agent: what was asked, then what came along, each in a tag of its own.
export function compose(text, contexts) {
  const blocks = contexts.map(block).filter(Boolean);
  if (!blocks.length) return text;
  return `${text.trim()}\n\nFrom my browser:\n\n${blocks.join("\n\n")}`;
}

function block(context) {
  const attrs = (pairs) => pairs.filter(([, v]) => v).map(([k, v]) => ` ${k}="${escape(v)}"`).join("");
  switch (context.kind) {
    case "page":
      return `<page${attrs([["title", context.title], ["url", context.url],
        ["truncated", context.truncated && "true"]])}>\n`
        + `${context.description ? `${context.description}\n\n` : ""}`
        + `${context.text || "(its text couldn't be read)"}\n</page>`;
    case "selection":
      return `<selection${attrs([["page", context.title], ["url", context.url]])}>\n${context.text}\n</selection>`;
    case "link":
      return `<link${attrs([["url", context.url], ["text", context.text], ["on", context.pageUrl]])} />`;
    default:
      return null;  // (a screenshot goes as an attachment)
  }
}

const ENTITIES = { "&": "&amp;", '"': "&quot;", "<": "&lt;", ">": "&gt;" };

function escape(value) {
  return String(value).replace(/[&"<>]/g, (c) => ENTITIES[c]);
}

export function unescape(value) {
  return String(value).replace(/&(amp|quot|lt|gt);/g, (_, name) => ({ amp: "&", quot: '"', lt: "<", gt: ">" })[name]);
}

// A value as one argument, as a shell (and Relay's Scripts page, which splits them so) reads it.
export function shellQuote(value) {
  const text = String(value);
  return /^[\w@%+=:,./-]+$/.test(text) ? text : `'${text.replace(/'/g, "'\"'\"'")}'`;
}
