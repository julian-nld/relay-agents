# Vendored frontend libraries

Served locally so Relay works without internet access (e.g. on a home server).

| File | Library | Version | License |
|------|---------|---------|---------|
| `marked.min.js` | [marked](https://github.com/markedjs/marked) | 15.0.12 | MIT |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.4.16 | Apache-2.0 / MPL-2.0 |
| `highlight.min.js` | [highlight.js](https://github.com/highlightjs/highlight.js) (common languages) | 11.12.0 | BSD-3-Clause |
| `hljs-light.min.css`, `hljs-dark.min.css` | highlight.js GitHub themes | 11.12.0 | BSD-3-Clause |
| `xterm.mjs`, `xterm.css` | [xterm.js](https://github.com/xtermjs/xterm.js) | 6.0.0 | MIT (`xterm.LICENSE`) |
| `xterm-fit.mjs` | xterm.js fit addon | 0.11.0 | MIT (`xterm-fit.LICENSE`) |

xterm.js and its fit addon provide the on-demand shell's ANSI/PTY terminal,
keyboard input, selection and responsive sizing. These files are copied from
the pinned npm tarballs (SHA-512 verified); no CDN or runtime npm install is used.
