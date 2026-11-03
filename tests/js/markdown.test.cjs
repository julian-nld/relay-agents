const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup() {
  const context = { URL };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "src/relay/static/js/markdown.js"), "utf8")
    .replace(/^import .*;\n/gm, "").replace(/^export /gm, ""), context);
  return context;
}

const away = { origin: "https://relay.example", hostname: "relay.example", port: 8420, addresses: ["192.168.1.5"] };

test("links to a server on this machine open through Relay at /port/<n>/", () => {
  const { serverLink } = setup();
  assert.equal(serverLink("http://localhost:5173/", away), "https://relay.example/port/5173/");
  assert.equal(serverLink("http://127.0.0.1:3000/api/x?y=1#z", away), "https://relay.example/port/3000/api/x?y=1#z");
  assert.equal(serverLink("http://0.0.0.0:8000", away), "https://relay.example/port/8000/");
  assert.equal(serverLink("http://[::1]:4000/a", away), "https://relay.example/port/4000/a");
  assert.equal(serverLink("http://192.168.1.5:5173/", away), "https://relay.example/port/5173/");
});

test("Relay's own port is Relay, and other places stay as they are", () => {
  const { serverLink } = setup();
  assert.equal(serverLink("http://localhost:8420/c/abc", away), "https://relay.example/c/abc");
  assert.equal(serverLink("http://localhost/", away), null);  // (no port: not a server of an agent's)
  assert.equal(serverLink("https://example.org:8443/", away), null);
  assert.equal(serverLink("http://192.168.1.1:8080/", away), null);  // (another machine on the network)
  assert.equal(serverLink("mailto:me@example.org", away), null);
  assert.equal(serverLink("not a link", away), null);
});

test("on the network itself, the same address with another port goes through Relay too", () => {
  const { serverLink } = setup();
  const lan = { origin: "http://192.168.1.5:8420", hostname: "192.168.1.5", port: 8420, addresses: [] };
  assert.equal(serverLink("http://192.168.1.5:5173/", lan), "http://192.168.1.5:8420/port/5173/");
});
