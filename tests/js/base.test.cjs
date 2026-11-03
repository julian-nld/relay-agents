const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup(base, pathname) {
  const meta = base === null ? null : { content: base };
  const context = { location: { pathname },
    document: { querySelector: (selector) => (selector === 'meta[name="relay-base"]' ? meta : null) } };
  vm.createContext(context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js/base.js"), "utf8").replace(/^export /gm, "");
  vm.runInContext(`${source}\nthis.BASE = BASE;`, context);
  return context;
}

test("at a site's root Relay's paths are as they are", () => {
  const env = setup(null, "/c/abc");
  assert.equal(env.BASE, "");
  assert.equal(env.relayUrl("/api/state"), "/api/state");
  assert.equal(env.relayPath(), "/c/abc");
  assert.equal(env.storeKey("relay.draft"), "relay.draft");
});

test("under a path (a trial Relay, a proxy's /relay/) its paths have the base, and its storage is its own", () => {
  const env = setup("/trial/abc123/", "/trial/abc123/c/def");
  assert.equal(env.BASE, "/trial/abc123");
  assert.equal(env.relayUrl("/api/state"), "/trial/abc123/api/state");
  assert.equal(env.relayUrl("https://example.com/x"), "https://example.com/x");  // (not Relay's)
  assert.equal(env.relayUrl("//elsewhere/x"), "//elsewhere/x");
  assert.equal(env.relayPath(), "/c/def");
  assert.equal(env.stripBase("/trial/abc123"), "/");
  assert.equal(env.stripBase("/trial/abc1234/x"), "/trial/abc1234/x");  // (another path that only starts alike)
  assert.notEqual(env.storeKey("relay.draft"), "relay.draft");
});
