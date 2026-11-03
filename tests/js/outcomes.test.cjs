const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const root = path.resolve(__dirname, "../..");

function setup() {
  const context = { clock: () => "15:00" };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, "src/relay/static/js/outcomes.js"), "utf8")
    .replace(/^import .*;\n/gm, "").replace(/^export /gm, ""), context);
  return context;
}

test("review location describes existing files and explicitly isolated branches", () => {
  const { reviewLocation } = setup();
  assert.match(reviewLocation({ mode: "direct" }).detail, /does not apply or commit/);
  assert.match(reviewLocation({ mode: "none" }).detail, /checkpoints/);
  const branch = reviewLocation({ mode: "worktree", branch: "task", base_branch: "main" });
  assert.equal(branch.label, "On a separate branch");
  assert.match(branch.detail, /task.*merge.*main/);
});

test("recovery explains actual automatic plans rather than offering a second silent retry", () => {
  const { recoveryNotice } = setup();
  assert.equal(recoveryNotice({}), "No automatic retry is scheduled.");
  assert.match(recoveryNotice({ resume_at: 1 }), /resume automatically at 15:00.*replaces that plan/);
  assert.match(recoveryNotice({ resume_at: 1, limit: { loop: { name: "Backup" }, restart: {} } }),
    /restart fallback loop “Backup”/);
  assert.match(recoveryNotice({ resume_at: 1, limit: { loop: { name: "Backup" } } }), /resume automatically/);
  assert.match(recoveryNotice({ limit: { gave_up: true } }), /stopped automatic retries/);
  assert.match(recoveryNotice({ running_run: "r", resume_at: 1 }), /already working/);
});

test("stop information belongs to its attempt, not the interrupted fallback", () => {
  const { currentLimit, recoveryNotice } = setup();
  const original = { run_id: "old", provider_id: "first", model: "one", gave_up: true };
  const last = { role: "assistant", status: "interrupted", run_id: "new", run_provider: "second", run_model: "two" };
  assert.equal(currentLimit({ limit: original, messages: [last] }), null);
  // (stopped by the user: nothing to say about retries; a simulated user found that line noise)
  assert.equal(recoveryNotice({ limit: original, messages: [last] }), "");
  assert.equal(recoveryNotice({ messages: [{ ...last, status: "failed" }] }), "No automatic retry is scheduled.");
  // A model that can't be used: another one is the way on
  const unusable = { run_id: "new", provider_id: "second", model: "two", unusable: true, gave_up: true };
  assert.equal(recoveryNotice({ limit: unusable, messages: [{ ...last, status: "failed" }] }),
    "This model can't be used here: choose another one.");
  assert.equal(currentLimit({ limit: original, messages: [{ ...last, run_id: "old" }] }), original);
  assert.equal(currentLimit({ limit: original,
    messages: [{ ...last, status: "failed", run_provider: "first", run_model: "one" }] }), null);
  const legacy = { provider_id: "second", model: "two" };
  assert.equal(currentLimit({ limit: legacy, messages: [last] }), null);
  assert.equal(currentLimit({ limit: legacy, messages: [{ ...last, status: "failed" }] }), legacy);
  assert.equal(currentLimit({ limit: { ...legacy, fell_back: {} }, messages: [{ ...last, status: "failed" }] }), null);
});

test("the question being answered is the running run's, whatever the chat is set to since", () => {
  const { answeringAgent } = setup();
  const answered = { role: "assistant", run_provider: "claude-code", run_model: "opus", run_mode: null };
  const conv = { provider_id: "claude-code", model: null, agent_mode: null,
    messages: [{ role: "user" }, answered, { role: "user" }] };
  // Answered: by its answer's run
  assert.deepEqual({ ...answeringAgent(conv, 0) }, { provider: "claude-code", model: "opus", mode: null });
  // Being answered (the model was changed meanwhile, to the default): by the run that's going (a user saw
  // "Now with Claude Code" above a turn that went on with opus)
  const going = { ...conv, running_run: "r2", running_agent: { provider: "claude-code", model: "opus", mode: null } };
  assert.deepEqual({ ...answeringAgent(going, 2) }, { provider: "claude-code", model: "opus", mode: null });
  // Not answered, nothing going: what the chat is set to now (the next turn's)
  assert.deepEqual({ ...answeringAgent(conv, 2) }, { provider: "claude-code", model: null, mode: null });
});

test("a long thread opens from its last messages, a whole turn at least", () => {
  const { firstShown } = setup();
  const turn = (events) => [{ role: "user" }, { role: "assistant", events: Array(events).fill({}) }];
  const short = [...turn(5), ...turn(5)];
  assert.equal(firstShown(short), 0);  // (all of it)
  // 300 turns of 100 events: the last ones up to about 3000, from a question on
  const long = Array.from({ length: 300 }, () => turn(100)).flat();
  const from = firstShown(long);
  assert.equal(long[from].role, "user");
  assert.ok(from > 0 && long.length - from <= 60 && long.length - from >= 50, String(from));
  // One answer bigger than all of it (a code task of 20,000 events): still its turn, whole
  const huge = [...turn(10), ...turn(20000)];
  assert.equal(firstShown(huge), 2);
  // The part before it, as the button shows it
  assert.equal(firstShown(long.slice(0, from)) < from, true);
});
