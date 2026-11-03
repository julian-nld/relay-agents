# Chrome

Relay in Chrome: your agents in Chrome's side panel, with the page you're on, a selection or a screenshot going along
with your question, your scripts run from there, and agents that use your own tabs, each step allowed by you.

The extension is `extension/` (Manifest V3, Chrome 116 or later). This plugin gives it to Relay's page as a zip, and
gives agents the tools they use your browser with.

## Installing

1. **The plugin:** link this folder into Relay's plugins folder and restart Relay (Settings → Plugins → Restart Relay):

   ```sh
   ln -s ~/relay/plugins/chrome ~/.local/share/relay/plugins/chrome
   ```

2. **The extension:** Settings → Chrome → **Download**. Unzip it, open `chrome://extensions`, turn on Developer mode,
   choose **Load unpacked** and pick the `relay-chrome` folder.
   - **A later version:** unzip it over that folder, then click the extension's reload button there.
3. **Signing in:** Settings → Chrome → **Make one** gives the extension an app password of its own. In the extension's
   settings (they open when it's installed), paste Relay's address and that password, then **Save and connect**.
   Chrome asks once whether the extension may reach that address.

Without an app password the extension uses Chrome's own sign-in to Relay (its cookie), but the app password is the way
that's tested.

## What it does

- **The side panel** (the toolbar button, or Alt+Shift+R):
  - Chat with any of Relay's agents and follow the answer as it comes: text, tool calls, permissions to answer, Stop.
  - Pick the agent and model. Your conversations are listed and searchable.
  - **The queue:** send while it answers and it's queued, shown above the message box as on Relay's pages. After an
    answer you stopped (or one that failed) it waits, with **Send now**, and anything queued can be taken back.
  - **Open in Relay** opens the chat in Relay itself.
- **Along with a question:**
  - the page you're on (its main text, up to 40,000 characters);
  - what's selected on it. A selection that already went along doesn't go again with the page.
  - a screenshot of the tab, as an attachment.
- **Scripts** (the panel's `>_` button): your scripts on Relay's Scripts page, grouped and searchable, each with its
  description.
  - **Running one:** with arguments, as typed on the Scripts page. **Page address** and **Selection** add those as an
    argument, quoted.
  - **Following it:** its output as it's written, Stop, how it ended, the chats it started, and its earlier runs.
  - It runs on Relay, signed in as you, as the page's Run does.
- **Right-click menu:**
  - "Ask Relay about “…”" for a selection, "Ask Relay about this page", "Summarize this page with Relay", and "Ask
    Relay about this link";
  - "Run a Relay script with …" the selection, the page's address or the link;
  - "Rewrite with Relay" in a text field (below).
- **Writing on the page:**
  - **Rewrite with Relay** (right-click in a text field): improve it, make it shorter or more formal, translate it to
    English or Dutch, or say yourself what to do ("In your own words…"). The field's text (what's selected in it, else
    all of it) goes along in a new chat, which asks for only the new text. **Replace in field**, under the answer, puts
    it back in that field, in place of what was taken (once; Ctrl+Z undoes it).
  - **Insert in page**, under any answer: its text, plain (no Markdown signs), where your cursor is in the page's
    focused field.
  - Both write as typing would (the page hears of it, and undo works), into text boxes, text areas and editors
    (contenteditable). Chrome asks once whether the extension may use pages, if it can't yet.
- **Address bar:** `relay`, a space and a question starts a chat and opens it in Relay.

It uses Relay's API as the Python SDK does:
- an app password as `Authorization: Bearer`, and `X-Relay-Request: 1` on what changes something;
- answers from `/api/runs/<id>/events`, and a script's output from `/api/scripts/runs/<id>/output?after=…`;
- Markdown rendered with the same marked and DOMPurify as Relay's pages (`extension/vendor/`).

## Agents in your browser

The agents of every chat and code task get these tools:

| Tool | What it does | Asks you |
|---|---|---|
| `chrome_tabs` | the tabs you have open: id, title, address | once per answer |
| `chrome_read` | a page's text, what can be used numbered: `[12 button “Send”]` | per site, once per answer |
| `chrome_click` | clicks element 12 | each time |
| `chrome_type` | fills in a field (or picks an option in a list), with `submit` presses Enter | each time |
| `chrome_open` | opens an address in a new tab (or a given one) and reads it; `back` goes back | each time |
| `chrome_scroll` | scrolls up, down, to the top or bottom, or to element 12 | per site, once per answer |
| `chrome_screenshot` | a screenshot of a tab, saved in the chat's folder (`.relay-chrome/`, the last 10) | per site, once per answer |

**Asking you:** each step is a card. It says which chat asks, the page, its site, the element, and the text it would
type. You choose **Deny**, **Allow**, or **Always on <site>**.
- **Where:** in the side panel when it's open. Otherwise Chrome shows a notification, and clicking it opens a small
  window with the card.
- What's always allowed is listed in the extension's settings, where you take it back or switch agents off.
- Opening a page in a tab of yours also needs the site it leaves allowed.

**It works while Chrome runs** with the extension signed in. Without that, the tools tell the agent the browser isn't
connected. With the extension in two Chromes (two computers, say), the one where you allow a step does it, once.

**What a page can do:** an agent reads what's on the pages it's allowed to, and a page can hold text written to talk an
agent into something. That's why clicking, typing and opening ask each time unless you chose **Always** for that site.
Keep that for sites you trust, and read what a card says before you allow it.

### How it works

- **A tool's call** becomes a command, kept in memory while the tool waits (5 minutes at most, for you and the
  browser).
  - At most 2 wait at once: each holds one of the plugin's four threads, and the extension needs one too.
- **The extension's service worker** (`background.js`, `lib/agent.js`):
  - asks for commands (`GET /api/plugins/chrome/commands`, held open up to 20 seconds when there's nothing new);
  - shows them as cards, and takes the one it carries out (`POST …/commands/<id>/claim`);
  - answers it (`POST …/commands/<id>/result`). The tool gives that answer to the agent.
  - **Staying up:** Chrome stops an idle service worker. An extension API call with each ask keeps it going, and an
    alarm every 30 seconds starts it again if Chrome stopped it all the same.
  - **The cards** go to the pages that show them (the side panel, `approve.html`) over a port, and your answers come
    back that way. Chrome asks for its permission to use pages in your click there, since it can't from the worker.
- **A run that ends** (stopped, say) takes its commands with it, and their cards go.
- **It keeps nothing** but those commands, in memory, and screenshots in the chat's folder. It makes no network calls.
- **The other routes:** `status` (whether a panel asked lately), `version` and `extension` (the zip, for Settings →
  Chrome).

## Files

- `__init__.py`: the plugin: the tools, the extension's routes, the zip.
- `static/`: its tab in Settings.
- `extension/`:
  - `background.js`: the service worker: side panel, right-click menu, address bar, and agents in this browser.
  - `sidepanel.*`: the panel. `scripts.js`: its scripts. `options.*`: the settings page.
  - `lib/relay.js`: Relay's API. `lib/page.js`: reading a page, screenshots, how it goes along in the message.
    `lib/markdown.js`, `lib/ui.js`: rendering.
  - `lib/agent.js`: agents in this browser: the plugin's commands, asking you, carrying them out.
  - `lib/cards.js`: their cards, in the side panel and in `approve.*`, the window a notification opens.
  - `lib/inpage.js`: what's run in a page: for agents, reading it with its elements numbered, clicking, typing,
    scrolling; for you, a field's text taken and put back, and an answer inserted.

## Testing

- **`tests/relay/test_plugin_chrome.py`:** the plugin on Relay's own plugin host, and the zip.
- **`tests/relay/test_ui_chrome.py`:** the extension in Chromium, against `tests/chrome/mock_relay.py`, a stand-in for
  Relay's API with this plugin loaded by Relay's host. It runs `tests/chrome/e2e.cjs` and `e2e_agents.cjs`.
- **By hand, against a real Relay and agent** (Claude Code · Haiku by default; each makes one chat, a few cents):
  - `real.cjs`: chat, page, screenshot, the queue, Stop;
  - `real_scripts.cjs`: a script run from the panel;
  - `real_agents.cjs`: an agent finds a tab, reads, types, clicks, is denied, takes a screenshot, and is stopped
    while a step waits.

  ```sh
  export CHROMIUM=~/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome
  export PLAYWRIGHT_CORE=~/relay/vendor-dev/playwright/driver/package
  RELAY_URL=http://127.0.0.1:8420 RELAY_APP_PASSWORD=relay_… node tests/chrome/real_agents.cjs [provider] [model]
  ```

`CHROMIUM` is Python Playwright's full Chromium, since its headless shell loads no extensions. Screenshots go to
`RELAY_CHROME_SHOTS`, else a folder in the system's temp. The test's Chromium profile, which keeps the extension's app
password, is removed when the script ends.
