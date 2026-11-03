# Third-party software

Relay itself is [MIT](LICENSE). This is what it uses from others.

## In this repository

Served to the browser, so that Relay works without internet access ([details](src/relay/static/vendor/README.md)):

| Library | Version | License | Where |
|---|---|---|---|
| [marked](https://github.com/markedjs/marked) | 15.0.12 | MIT | `src/relay/static/vendor/`, `plugins/chrome/extension/vendor/` |
| [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.16 | Apache-2.0 or MPL-2.0 | `src/relay/static/vendor/`, `plugins/chrome/extension/vendor/` |
| [highlight.js](https://github.com/highlightjs/highlight.js) and its GitHub themes | 11.12.0 | BSD-3-Clause | `src/relay/static/vendor/` |
| [xterm.js](https://github.com/xtermjs/xterm.js) | 6.0.0 | MIT | `src/relay/static/vendor/` |
| xterm.js fit addon | 0.11.0 | MIT | `src/relay/static/vendor/` |

## Installed when you build it

Pinned in [requirements.lock](requirements.lock) and installed into `vendor/` with their licenses by
`scripts/vendor.sh`: Starlette, Uvicorn, wsproto, h11, AnyIO, Textual, Rich, Pygments, pypdf and what they depend on.
The dev tools (pytest, ruff, Playwright, …) are the same, in `vendor-dev/`.

## Fetched when you use it

Not part of Relay and not in this repository. Relay or a plugin gets these when you turn on what needs them, and each
keeps its own license:

| | Used by |
|---|---|
| Debian, Python and Node.js container images and packages | Relay's images |
| [SearXNG](https://github.com/searxng/searxng) | web search |
| [gluetun](https://github.com/qdm12/gluetun) | VPNs for WorkBoxes |
| [OpenHands](https://github.com/All-Hands-AI/OpenHands) Agent Canvas | the OpenHands plugin |

The agents themselves (Claude Code, opencode, GitHub Copilot, …) are yours to install, under their own terms.
