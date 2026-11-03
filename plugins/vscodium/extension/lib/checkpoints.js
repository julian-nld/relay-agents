// A folder's files before an agent's turn, to show what it changed and to put them back: what Relay's checkpoints do
// for a code task (relay/checkpoints.py), here for a computer task, whose folder is on this computer. A git repository
// of the extension's own looks at the folder from outside: the folder gets no .git, a repository it is already part
// of is left alone, its .gitignore files count. Nothing of VSCodium's here: node --test tries it.
"use strict";

const { execFile } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

// Never worth keeping (as Relay's git.DEPENDENCIES and checkpoints.EXCLUDE)
const EXCLUDE = ["node_modules/", "bower_components/", "jspm_packages/", ".venv/", "venv/", "__pycache__/", "*.pyc",
  "*.egg-info/", ".eggs/", "site-packages/", ".tox/", ".nox/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
  ".cache/", ".gradle/", ".next/", ".nuxt/", ".svelte-kit/", ".angular/", ".turbo/", ".parcel-cache/", ".dart_tool/",
  ".bundle/", "**/vendor/bundle/", "Pods/", "dist/", "build/", "target/", ".DS_Store"];
const MAX_FILES = 20000;  // a folder with more files than this gets no checkpoints
const MAX_FILE_BYTES = 50 * 1024 * 1024;  // a bigger file isn't kept (a video would fill the disk turn by turn)
const IDENTITY = { GIT_AUTHOR_NAME: "Relay", GIT_AUTHOR_EMAIL: "relay@localhost", GIT_COMMITTER_NAME: "Relay",
  GIT_COMMITTER_EMAIL: "relay@localhost" };
const STATUS = { A: "added", M: "modified", D: "deleted", R: "renamed", C: "added", T: "modified" };

class CheckpointError extends Error {}

// (what's being done to each folder's repository: one thing at a time, whichever Checkpoints asks)
const turns = new Map();

function oneAtATime(key, work) {
  const done = (turns.get(key) || Promise.resolve()).catch(() => {}).then(work);
  turns.set(key, done);
  return done;
}

class Checkpoints {
  // `store`: the extension's own folder (its global storage), where the repository is
  constructor(folder, store) {
    this.folder = path.resolve(folder);
    this.repo = path.join(store, "checkpoints",
      `${crypto.createHash("sha256").update(this.folder).digest("hex").slice(0, 16)}.git`);
  }

  git(args, { index, binary = false, ok = false } = {}) {
    const env = { ...process.env, ...IDENTITY, GIT_DIR: this.repo, GIT_WORK_TREE: this.folder,
      ...(index ? { GIT_INDEX_FILE: path.join(this.repo, index) } : {}) };
    return new Promise((resolve, reject) => {
      execFile("git", ["-c", "core.quotepath=false", "-c", "core.autocrlf=false", ...args],
        { cwd: this.folder, env, maxBuffer: 512 * 1024 * 1024, encoding: binary ? "buffer" : "utf8" },
        (error, stdout, stderr) => {
          if (error && !ok) {
            reject(new CheckpointError(error.code === "ENOENT" ? "git isn't installed on this computer: VSCodium "
              + "needs it to keep what the agent changed" : String(stderr || error.message).trim().split("\n")[0]));
          } else {
            resolve(error ? null : stdout);
          }
        });
    });
  }

  async ready() {
    if (!fs.existsSync(this.folder)) throw new CheckpointError(`No folder ${this.folder}`);
    if (fs.existsSync(path.join(this.repo, "relay-too-big"))) {
      throw new CheckpointError(
        `${this.folder} has more than ${MAX_FILES} files: VSCodium doesn't keep checkpoints of it`);
    }
    if (fs.existsSync(path.join(this.repo, "HEAD"))) return;
    fs.mkdirSync(path.dirname(this.repo), { recursive: true });
    await this.git(["init", "-q"]);
    fs.mkdirSync(path.join(this.repo, "info"), { recursive: true });
    fs.writeFileSync(path.join(this.repo, "info", "exclude"), `${EXCLUDE.join("\n")}\n`);
    const listed = await this.git(["ls-files", "-z", "--others", "--exclude-standard"]);
    if (listed.split("\0").length > MAX_FILES) {
      fs.writeFileSync(path.join(this.repo, "relay-too-big"), `More than ${MAX_FILES} files\n`);
      await this.ready();
    }
  }

  // The folder's files as they are now, kept under `ref`; the checkpoint (the last one when nothing changed)
  take(label, ref = "refs/relay/checkpoints", index = "index") {
    return oneAtATime(this.repo, () => this.record(label, ref, index));
  }

  async record(label, ref, index) {
    await this.ready();
    await this.skipBig(index);
    await this.git(["add", "-A", "--", ".",
      ...(await this.newRepositories(index)).map((r) => `:(exclude,literal)${r}`)],
      { index });
    const tree = (await this.git(["write-tree"], { index })).trim();
    const parent = ((await this.git(["rev-parse", "-q", "--verify", `${ref}^{commit}`], { ok: true })) || "").trim();
    if (parent && (await this.git(["rev-parse", `${parent}^{tree}`])).trim() === tree) return parent;
    const commit = (await this.git(["commit-tree", tree, "-m", String(label || "checkpoint").slice(0, 200),
      ...(parent ? ["-p", parent] : [])])).trim();
    await this.git(["update-ref", ref, commit]);
    return commit;
  }

  // Folders that are repositories of their own without a commit yet (git init in a subproject): git can't add them,
  // and one such folder made the whole add fail (as Relay's git.untracked leaves them out)
  async newRepositories(index) {
    const listed = await this.git(["ls-files", "-z", "--others", "--exclude-standard"], { index });
    const found = [];
    for (const relative of listed.split("\0").filter((p) => p.endsWith("/"))) {
      const head = await new Promise((resolve) => execFile("git", ["-C", path.join(this.folder, relative), "rev-parse",
        "-q", "--verify", "HEAD"], { env: Object.fromEntries(Object.entries(process.env)
        .filter(([key]) => !key.startsWith("GIT_"))) }, (error) => resolve(!error)));
      if (!head) found.push(relative);
    }
    return found;
  }

  // Files over MAX_FILE_BYTES left out from now on
  async skipBig(index) {
    const listed = await this.git(["ls-files", "-z", "--others", "--modified", "--exclude-standard"], { index });
    const big = listed.split("\0").filter(Boolean).filter((relative) => {
      try {
        return fs.statSync(path.join(this.folder, relative)).size > MAX_FILE_BYTES;
      } catch {
        return false;
      }
    });
    if (!big.length) return;
    const exclude = path.join(this.repo, "info", "exclude");
    const known = fs.readFileSync(exclude, "utf8").split("\n");
    const lines = big.map((b) => `/${b.replace(/[*?[\]!#\\]/g, (c) => `\\${c}`)}`).filter((l) => !known.includes(l));
    if (lines.length) fs.appendFileSync(exclude, `# too big to keep\n${lines.join("\n")}\n`);
  }

  // What differs from checkpoint `base` now: [{path, from, status, additions, deletions, binary}]
  changes(base) {
    return oneAtATime(this.repo, () => this.compare(base));
  }

  async compare(base) {
    const now = await this.record("Now", "refs/relay/looks", "look-index");
    if (now === base) return { now, files: [] };
    const names = (await this.git(["diff", "--name-status", "-z", "-M", base, now])).split("\0");
    const files = [];
    for (let i = 0; i < names.length - 1;) {
      const code = names[i++];
      const kind = STATUS[code[0]] || "modified";
      const from = kind === "renamed" ? names[i++] : null;
      files.push({ path: names[i++], from, status: kind, additions: 0, deletions: 0, binary: false });
    }
    const numbers = (await this.git(["diff", "--numstat", "-z", "-M", base, now])).split("\0");
    for (let i = 0; i < numbers.length - 1;) {
      const [added, deleted, named] = numbers[i++].split("\t");
      let file = named;
      if (!named) {  // (a rename: its old and new path follow)
        i += 1;
        file = numbers[i++];
      }
      const found = files.find((f) => f.path === file);
      if (found) {
        found.binary = added === "-";
        found.additions = Number(added) || 0;
        found.deletions = Number(deleted) || 0;
      }
    }
    return { now, files };
  }

  // A file's unified diff from `base` to the last look (changes())
  async diff(base, file) {
    return this.git(["diff", "-M", base, "refs/relay/looks", "--", ...(file.from ? [file.from] : []), file.path]);
  }

  // A file as it was at `base`; null when it wasn't there
  async original(base, relative) {
    return this.git(["show", `${base}:${relative}`], { binary: true, ok: true });
  }

  // Files as they were at `base` (all those changed since, or `files` of changes()): the files just before
  // are kept first, so this can be undone too; that checkpoint
  restore(base, files = null) {
    return oneAtATime(this.repo, () => this.putBack(base, files));
  }

  async putBack(base, files) {
    const listed = files || (await this.compare(base)).files;
    const undo = await this.record("Before putting files back", "refs/relay/undo", "undo-index");
    for (const file of listed) {
      for (const [relative, was] of [[file.path, file.status !== "added"], ...(file.from ? [[file.from, true]] : [])]) {
        const place = path.join(this.folder, relative);
        const old = was ? await this.original(base, relative) : null;
        if (old === null) {
          fs.rmSync(place, { force: true });
        } else {
          fs.mkdirSync(path.dirname(place), { recursive: true });
          fs.writeFileSync(place, old);
        }
      }
    }
    return undo;
  }
}

module.exports = { CheckpointError, Checkpoints, EXCLUDE, MAX_FILES };
