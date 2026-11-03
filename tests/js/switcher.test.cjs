const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup() {
  const context = {};
  vm.createContext(context);
  const source = fs.readFileSync(path.join(root, "src/relay/static/js/switcher.js"), "utf8")
    .replace(/^import [^;]*;\n/gm, "").replace(/^export /gm, "");
  vm.runInContext(`${source}\nthis.rankChats = rankChats;`, context);
  return context;
}

const chats = [
  { id: "a", title: "Relay redesign", updated: 10 },
  { id: "b", title: "How does relay work?", updated: 30 },
  { id: "c", title: "Ryzen model", updated: 20 },
  { id: "d", title: "Unrelayed notes", updated: 40 },
  { id: "e", title: "relay: the scripts", updated: 5 },
];
const ids = (list) => list.map((conv) => conv.id).join(" ");

test("Ctrl+K ranks titles that start with it, then a word that does, then anywhere; newest first in each", () => {
  const { rankChats } = setup();
  assert.equal(ids(rankChats(chats, "relay")), "a e b d");
  assert.equal(ids(rankChats(chats, "RELAY")), "a e b d");
  assert.equal(ids(rankChats(chats, "  ryzen ")), "c");
  assert.equal(ids(rankChats(chats, "nothing like it")), "");
});

test("each word typed must be in the title, in any order", () => {
  const { rankChats } = setup();
  assert.equal(ids(rankChats(chats, "work relay")), "b");
  assert.equal(ids(rankChats(chats, "relay scripts")), "e");
});

test("with nothing typed: the most recent chats, not the one you're in, a few at a time", () => {
  const { rankChats } = setup();
  assert.equal(ids(rankChats(chats, "", { current: "d" })), "b c a e");
  assert.equal(rankChats(chats, "", { limit: 2 }).length, 2);
  assert.equal(ids(rankChats([{ id: "x", title: null }], "")), "x");
});
