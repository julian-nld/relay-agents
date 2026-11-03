// Permission control in VSCodium (relay.permissions): before an agent runs a command or changes a file on this
// computer (a connector's job), the user is asked here, one question at a time, with Allow, Allow for this answer and
// Deny. Without an answer in time it's denied, before Relay stops waiting for the job; a command's own time is what's
// left of it. Nothing of VSCodium's here: node --test tries it (permissions.js asks in VSCodium).
"use strict";

const MOST = 90;  // seconds a question waits for its answer
const SLACK = 15;  // seconds kept from Relay's wait for the job (its time-out + 25 s) for the job itself to answer

const REFUSED = "The user refused this in VSCodium: it wasn't done";
const LATE = "The user didn't answer in VSCodium in time, so it wasn't done";

class Gate {
  // `mode()`: "ask" | "commands" | "off"; `about(job)`: {id, run, title} of its conversation, or null;
  // `ask(job, about, seconds, signal)`: "once" | "answer" | "denied"
  constructor({ mode, about = async () => null, ask, now = () => Date.now() }) {
    Object.assign(this, { mode, about, ask, now });
    this.allowed = new Set();  // (runs allowed everything for their answer)
    this.turn = Promise.resolve();
  }

  needed(job) {
    const mode = this.mode();
    return job.kind !== "read" && mode !== "off" && !(mode === "commands" && job.kind !== "run");
  }

  // {ok: true, timeout?} to go ahead (a command's time-out shortened by the asking), or {ok: false, error}
  async allow(job) {
    if (!this.needed(job)) return { ok: true };
    const started = this.now();
    const about = await this.about(job).catch(() => null);
    const deadline = started + 1000 * Math.min(MOST, Math.max(10, (Number(job.timeout) || 120) + 25 - SLACK));
    // (one question at a time: the one before may allow this answer's)
    const waited = this.turn.then(() => this.decide(job, about, deadline));
    this.turn = waited.catch(() => {});
    const decision = await waited;
    if (decision === "denied") return { ok: false, error: REFUSED };
    if (decision === "timeout") return { ok: false, error: LATE };
    if (decision === "answer" && about?.run) this.allowed.add(about.run);
    const spent = Math.round((this.now() - started) / 1000);
    return job.kind === "run" && spent > 0 ? { ok: true, timeout: Math.max(5, (Number(job.timeout) || 120) - spent) }
      : { ok: true };
  }

  async decide(job, about, deadline) {
    if (about?.run && this.allowed.has(about.run)) return "once";
    const left = deadline - this.now();
    if (left <= 0) return "timeout";
    const stop = new AbortController();
    let timer;
    const late = new Promise((resolve) => { timer = setTimeout(() => resolve("timeout"), left); });
    try {
      return await Promise.race([this.ask(job, about, Math.round(left / 1000), stop.signal), late]);
    } finally {
      clearTimeout(timer);
      stop.abort();  // (the question is taken away where it was asked)
    }
  }
}

module.exports = { Gate, LATE, MOST, REFUSED };
