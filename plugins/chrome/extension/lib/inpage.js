// What an agent does in a page (agent.js has it run there, in the extension's own world of that page): read it with
// a number for each element it can use, and click, fill in or scroll by that number. And what the side panel writes
// there for you: a field's text rewritten, an answer inserted. A classic script, not a module:
// chrome.scripting runs it as a file, and it leaves globalThis.__relayPage. The numbers stay with their elements
// for as long as the page does (in this world's memory: nothing is written into the page).

(() => {
  if (globalThis.__relayPage) return;

  const refs = { next: 1, byElement: new WeakMap(), byRef: new Map() };
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "CANVAS", "HEAD", "META", "LINK", "IFRAME",
    "OBJECT", "EMBED"]);
  const BLOCK =
    new Set(["ADDRESS", "ARTICLE", "ASIDE", "BLOCKQUOTE", "BR", "DD", "DETAILS", "DIALOG", "DIV", "DL", "DT",
      "FIELDSET", "FIGCAPTION", "FIGURE", "FOOTER", "FORM", "H1", "H2", "H3", "H4", "H5", "H6", "HEADER", "HR", "LI",
      "MAIN", "NAV", "OL", "P", "PRE", "SECTION", "TABLE", "TBODY", "THEAD", "TR", "UL"]);
  const ROLES = new Set(["button", "link", "checkbox", "radio", "switch", "tab", "menuitem", "menuitemcheckbox",
    "menuitemradio", "option", "textbox", "searchbox", "combobox", "slider", "treeitem"]);
  const LONG_NAME = 100;  // a link or button with more text than this: marked, and its text read as text

  const flat = (text) => String(text || "").replace(/\s+/g, " ").trim();
  const clip = (text, n) => (text.length > n ? `${text.slice(0, n - 1)}…` : text);
  const quote = (text) => `“${text}”`;

  function ref(el) {
    let n = refs.byElement.get(el);
    if (!n) {
      n = refs.next++;
      refs.byElement.set(el, n);
      refs.byRef.set(n, new WeakRef(el));
    }
    return n;
  }

  function find(n) {
    const el = refs.byRef.get(Number(n))?.deref();
    if (!el || !el.isConnected) {
      throw new Error(`There's no element [${n}] on the page now: read it again (chrome_read) for its numbers`);
    }
    return el;
  }

  function shown(el) {
    if (el.checkVisibility?.({ checkOpacity: true, checkVisibilityCSS: true })) return true;
    const style = getComputedStyle(el);
    if (style.display === "contents") return true;  // (no box of its own: what's in it may well show)
    return !el.checkVisibility && style.display !== "none" && style.visibility !== "hidden";
  }

  // What it is for an agent: a link, a button, a textbox…; null: nothing to use.
  function roleOf(el) {
    const role = el.getAttribute("role");
    switch (el.tagName) {
      case "A": if (el.hasAttribute("href")) return "link"; break;
      case "BUTTON": case "SUMMARY": return "button";
      case "TEXTAREA": return "textbox";
      case "SELECT": return "select";
      case "INPUT": {
        const type = (el.getAttribute("type") || "text").toLowerCase();
        if (type === "hidden") return null;
        if (["button", "submit", "reset", "image"].includes(type)) return "button";
        if (type === "checkbox" || type === "radio") return type;
        if (type === "range") return "slider";
        return "textbox";
      }
      default: break;
    }
    if (role && ROLES.has(role)) return role;
    if (el.isContentEditable && !el.parentElement?.isContentEditable) return "textbox";
    if (el.hasAttribute("onclick")) return "clickable";
    return null;
  }

  function nameOf(el, role) {
    const labelled = el.getAttribute("aria-labelledby");
    let name = el.getAttribute("aria-label")
      || (labelled && labelled.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" "));
    if (!name && el.labels?.length) name = [...el.labels].map((label) => label.innerText).join(" ");
    if (!name && ["textbox", "select", "searchbox", "combobox"].includes(role) && el.tagName !== "SELECT"
      && !el.isContentEditable) {
      name = el.getAttribute("placeholder") || el.getAttribute("title") || el.getAttribute("name");
    }
    if (!name && el.tagName === "INPUT") name = el.value || el.getAttribute("alt");
    if (!name && el.tagName !== "SELECT" && !el.isContentEditable) name = el.innerText;
    if (!name) name = el.getAttribute("title") || el.querySelector?.("img[alt]")?.getAttribute("alt") || "";
    return flat(name);
  }

  function href(el) {
    try {
      const url = new URL(el.getAttribute("href"), location.href);
      if (url.protocol === "javascript:") return "";
      return clip(url.origin === location.origin ? url.pathname + url.search + url.hash : url.href, 100);
    } catch {
      return "";
    }
  }

  // An element as the agent reads it: [12 button “Send”] (without its name, `named` false: [14 link → /item/3]).
  function describe(el, named = true) {
    const role = roleOf(el) || el.tagName.toLowerCase();
    const n = ref(el);
    const name = named ? clip(nameOf(el, role), LONG_NAME) : "";
    let more = "";
    if (role === "link") {
      const to = href(el);
      if (to) more = ` → ${to}`;
    } else if (role === "textbox" || role === "searchbox" || role === "combobox") {
      const type = el.tagName === "INPUT" ? (el.getAttribute("type") || "text").toLowerCase() : "";
      const value = type === "password" ? (el.value ? "•••" : "")
        : flat(el.isContentEditable ? el.innerText : el.value);
      more = `${type && type !== "text" ? ` type=${type}` : ""}${value ? `: ${quote(clip(value, 200))}` : ""}`;
      if (el.disabled || el.readOnly) more += " (can't be changed)";
    } else if (role === "select") {
      const chosen = el.selectedOptions?.[0];
      const options = [...el.options].map((o) => flat(o.text)).filter(Boolean);
      more = `${chosen ? `: ${quote(flat(chosen.text))}` : ""} (options: ${clip(options.join(" | "), 300)})`;
    } else if (role === "checkbox" || role === "radio" || role === "switch") {
      const checked = el.checked ?? el.getAttribute("aria-checked") === "true";
      more = checked ? " ✓" : " ☐";
    }
    if (el.disabled && role === "button") more += " (disabled)";
    return `[${n} ${role}${name ? ` ${quote(name)}` : ""}${more}]`;
  }

  // The page as text, its usable elements marked; from `offset`, `limit` characters of it.
  function read(offset = 0, limit = 20000) {
    const out = [];
    let line = "";
    const flush = () => {
      const text = line.replace(/[ \t]+/g, " ").trim();
      if (text) out.push(text);
      line = "";
    };
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          line += child.nodeValue;
          continue;
        }
        if (child.nodeType !== Node.ELEMENT_NODE || SKIP.has(child.tagName) || !shown(child)) continue;
        if (child.getAttribute("aria-hidden") === "true") continue;
        const role = roleOf(child);
        const block = BLOCK.has(child.tagName);
        if (block) flush();
        if (/^H[1-6]$/.test(child.tagName)) line += `${"#".repeat(Number(child.tagName[1]))} `;
        else if (child.tagName === "LI") line += "- ";
        if (role) {
          // (a long link or button, a whole card say: marked, then its text read as text)
          const long = (role === "link" || role === "button" || role === "clickable")
            && !child.getAttribute("aria-label") && flat(child.innerText).length > LONG_NAME;
          line += ` ${describe(child, !long)} `;
          if (long) walk(child);
        } else {
          if (child.tagName === "IMG" && flat(child.getAttribute("alt")).length > 2) line +=
            ` [image ${quote(flat(child.alt))}] `;
          walk(child);
          if (child.shadowRoot) walk(child.shadowRoot);
          if (child.tagName === "TD" || child.tagName === "TH") line += " | ";
        }
        if (block) flush();
      }
    };
    walk(document.body || document.documentElement);
    flush();
    const text = out.join("\n");
    const part = text.slice(offset, offset + limit);
    const rest = text.length - offset - part.length;
    return { text: part, total: text.length, rest, next: offset + part.length };
  }

  function center(el) {
    el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
    const box = el.getBoundingClientRect();
    return { clientX: box.left + box.width / 2, clientY: box.top + box.height / 2 };
  }

  function click(n) {
    const el = find(n);
    const said = describe(el);
    if (el.disabled) throw new Error(`${said} is disabled: it can't be clicked now`);
    const at = { bubbles: true, cancelable: true, composed: true, view: window, button: 0, ...center(el) };
    el.dispatchEvent(new PointerEvent("pointerover", at));
    el.dispatchEvent(new PointerEvent("pointerdown", { ...at, isPrimary: true }));
    el.dispatchEvent(new MouseEvent("mousedown", at));
    el.focus?.({ preventScroll: true });
    el.dispatchEvent(new PointerEvent("pointerup", { ...at, isPrimary: true }));
    el.dispatchEvent(new MouseEvent("mouseup", at));
    el.click();  // (the click itself, and what it does: a link followed, a box checked, a form sent)
    return { did: `Clicked ${said}` };
  }

  function type(n, text, submit) {
    const el = find(n);
    const role = roleOf(el);
    const said = describe(el);
    if (role === "checkbox" || role === "radio" || role === "button" || role === "link") {
      throw new Error(`${said} isn't a field to type in: click it (chrome_click)`);
    }
    if (el.disabled || el.readOnly) throw new Error(`${said} can't be changed`);
    center(el);
    el.focus?.({ preventScroll: true });
    if (el.tagName === "SELECT") {
      const wanted = flat(text).toLowerCase();
      const options = [...el.options];
      const option = options.find((o) => flat(o.text).toLowerCase() === wanted || o.value === text)
        || options.find((o) => flat(o.text).toLowerCase().includes(wanted));
      if (!option) throw new Error(`${said} has no option ${quote(text)}`);
      el.value = option.value;
    } else if (el.isContentEditable) {
      const range = document.createRange();
      range.selectNodeContents(el);
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      if (!document.execCommand("insertText", false, text)) el.textContent = text;
    } else if ("value" in el) {
      const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(el, text);
    } else {
      throw new Error(`${said} isn't a field to type in`);
    }
    if (!el.isContentEditable) el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, data: text }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    let did = `Typed ${quote(clip(flat(text), 80))} into ${said.replace(/: “.*”\]$/, "]")}`;
    if (submit) {
      const key =
        { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true, composed: true };
      const go = el.dispatchEvent(new KeyboardEvent("keydown", key));
      el.dispatchEvent(new KeyboardEvent("keypress", key));
      el.dispatchEvent(new KeyboardEvent("keyup", key));
      // (Enter in a form's field sends the form, as a real Enter does, unless the page took the key itself)
      if (go && el.form && el.tagName === "INPUT") el.form.requestSubmit();
      did += ", and pressed Enter";
    }
    return { did };
  }

  // The part that scrolls under the window's middle (a chat's messages, say), else the page.
  function scroller() {
    let el = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
    while (el && el !== document.body && el !== document.documentElement) {
      const style = getComputedStyle(el);
      if (/(auto|scroll|overlay)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 10) return el;
      el = el.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  }

  function scroll(direction, n) {
    if (n) {
      const el = find(n);
      center(el);
      return { did: `Scrolled ${describe(el)} into view` };
    }
    const el = scroller();
    const page = el === document.scrollingElement || el === document.documentElement;
    const step = (page ? innerHeight : el.clientHeight) * 0.8;
    if (direction === "up") el.scrollBy({ top: -step, behavior: "instant" });
    else if (direction === "top") el.scrollTo({ top: 0, behavior: "instant" });
    else if (direction === "bottom") el.scrollTo({ top: el.scrollHeight, behavior: "instant" });
    else el.scrollBy({ top: step, behavior: "instant" });
    const max = Math.max(0, el.scrollHeight - el.clientHeight);
    const where = max ? `${Math.round((el.scrollTop / max) * 100)}% of the way down` : "it doesn't scroll";
    return { did: `Scrolled ${direction} ${page ? "the page" : "the part under the window's middle"} (${where})` };
  }

  // What an element is, for asking the user (without making it a number of its own).
  function about(n) {
    try {
      return describe(find(n));
    } catch {
      return `[${n}]`;
    }
  }

  // Writing on the page for you (the side panel's Rewrite and Insert in page): the field you're in, its text taken
  // and put back, or an answer put in where your cursor is.
  const fields = new Map();  // id: { el (a WeakRef), start, end (an input's), range (an editor's) }

  // The field that has the focus: a text box, a text area, or an editor (contenteditable, as a whole).
  function focused() {
    let el = document.activeElement;
    while (el?.shadowRoot?.activeElement) el = el.shadowRoot.activeElement;
    if (!el || el.disabled || el.readOnly) return null;
    if (el.isContentEditable) {
      while (el.parentElement?.isContentEditable) el = el.parentElement;
      return el;
    }
    if (el.tagName === "TEXTAREA") return el;
    if (el.tagName === "INPUT" && /^(text|search|email|url|tel)$/.test(el.type)) return el;
    return null;
  }

  function editorRange(el) {
    const selection = getSelection();
    const range = selection.rangeCount ? selection.getRangeAt(0) : null;
    return range && !range.collapsed && el.contains(range.commonAncestorContainer) ? range.cloneRange() : null;
  }

  // The focused field's text (what's selected in it, else all of it), kept as `id` to put text back into.
  function field(id) {
    const el = focused();
    if (!el) throw new Error("No text field has the focus: right-click in one");
    if (el.isContentEditable) {
      let range = editorRange(el);
      const selected = Boolean(range);
      if (!range) {
        range = document.createRange();
        range.selectNodeContents(el);
      }
      fields.set(id, { el: new WeakRef(el), range });
      return { text: selected ? range.toString() : el.innerText, selected };
    }
    const selected = el.selectionStart !== el.selectionEnd;
    const [start, end] = selected ? [el.selectionStart, el.selectionEnd] : [0, el.value.length];
    fields.set(id, { el: new WeakRef(el), start, end });
    return { text: el.value.slice(start, end), selected };
  }

  // `text` where the selection (or caret) is, as typing it would: undone with Ctrl+Z, and the page told of it.
  function put(el, text) {
    if (document.execCommand("insertText", false, text)) return;
    if (el.isContentEditable) {
      const range = getSelection().getRangeAt(0);
      range.deleteContents();
      range.insertNode(document.createTextNode(text));
    } else {
      el.setRangeText(text, el.selectionStart, el.selectionEnd, "end");
      el.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, data: text }));
    }
  }

  // The field kept as `id`: what was taken from it, replaced by `text` (once).
  function replace(id, text) {
    const kept = fields.get(id);
    const el = kept?.el.deref();
    if (!el || !el.isConnected) throw new Error("That field isn't on the page any more");
    fields.delete(id);
    el.focus({ preventScroll: true });
    if (el.isContentEditable) {
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(kept.range);
    } else {
      const length = el.value.length;
      el.setSelectionRange(Math.min(kept.start, length), Math.min(kept.end, length));
    }
    put(el, text);
    return { did: "Replaced in the field" };
  }

  // `text` where the cursor is in the field that has the focus (in place of what's selected there).
  function insert(text) {
    const el = focused();
    if (!el) throw new Error("No text field has the focus in the page: click into one, then insert");
    el.focus({ preventScroll: true });
    put(el, text);
    return { did: "Inserted in the field" };
  }

  globalThis.__relayPage = { read, click, type, scroll, about, field, replace, insert };
})();
