// Paths from two machines: Relay's have "/", this computer's may have "\" and a drive letter (Windows, where case
// doesn't count either). Nothing of VSCodium's here: node --test tries it.
"use strict";

const os = require("os");

function plain(place) {
  const slashed = String(place).replace(/\\/g, "/").replace(/(.)\/+$/, "$1");
  return /^[a-z]:/i.test(slashed) ? slashed.toLowerCase() : slashed;
}

// Whether `child` is `parent` or in it
function inside(child, parent) {
  if (!child || !parent) return false;
  const [c, p] = [plain(child), plain(parent)];
  return c === p || c.startsWith(p.endsWith("/") ? p : `${p}/`);
}

// Whether one of the two is in the other (a task in a folder around the open one, or in it)
function near(a, b) {
  return inside(a, b) || inside(b, a);
}

// This computer's name as Relay names a computer: letters, digits and dashes
function hostName(host = os.hostname()) {
  return host.split(".")[0].toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "laptop";
}

// Whether Relay's address is this machine (or, with Remote - SSH, the one VSCodium's folders are on)
function sameMachine(url, host = os.hostname()) {
  try {
    const name = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(url) ? url : `http://${url}`).hostname.replace(/^\[|\]$/g, "");
    return ["localhost", "127.0.0.1", "::1", host.toLowerCase(), hostName(host)].includes(name.toLowerCase());
  } catch {
    return true;
  }
}

module.exports = { hostName, inside, near, plain, sameMachine };
