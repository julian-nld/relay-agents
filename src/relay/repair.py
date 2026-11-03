"""Self-repair: another agent fixes a provider's support layer.

When a provider stops working because the program behind it changed (a new
Claude Code or opencode version with other output, flags or API), another
agent can repair Relay's support layer for it: the file or two that turn
that program into Relay's uniform events (Provider.support_files(): its folder).

Safety comes first; the repairing agent never touches Relay itself:

- it works in a folder of its own (~/.local/share/relay/repairs/<id>) with *copies* of
  just those files, plus a read-only reference copy of Relay's source;
- its commands run in that folder's WorkBox, without the program: Relay runs what it can't
  there for it (relay.delegation's repair tools): its version tested in a copy of Relay
  (repair_check), and a command in the program's own ProviderBox (repair_run);
- Relay's own source is fingerprinted when the repair starts; changes to it
  are reported, never attributed to the agent by their modification time;
- a review takes only the support files from the folder, compiles them and
  tests them in a shadow copy of Relay (the provider's check and a tiny
  prompt), without touching the running Relay;
- the user sees the diff and the test, applies it (the originals are kept),
  can undo it, and restarts Relay to use it.

The underlying program itself (an old version, a lost login) the agent can't fix: its box has
it read-only. It says so, and the user updates it or signs in again.

Who starts one: the user (Settings → Providers → its 🔧), or AgentOS's Governor. Relay sees a
provider whose last runs all failed, not on a limit or something passing (failing_providers),
and wakes the Governor (relay.scheduler); it may start a repair (provider.repair). A repair has
a page of its own (relay.creations), as adding a provider does: what's wrong, its agent's plan,
Relay's whole check of it and a trial Relay with it. Applying stays the user's: it changes the
running Relay.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from relay import config, health
from relay.db import Database
from relay.providers import KINDS, Registry, has_shell

SOURCE = Path(__file__).resolve().parent.parent  # src/: relay/ and opencode_manager/
PACKAGES = ("relay", "opencode_manager")
# In the reference copy (all of Relay's source), the files the brief points to
REFERENCE = ("relay/providers/base.py", "relay/providers/bridge_run.py", "opencode_manager/bridge/events.py",
             "opencode_manager/bridge/billing.py")
RUN_TIMEOUT = 120  # seconds a repair_run command gets by default (at most RUN_LONGEST)
RUN_LONGEST = 600
RUN_OUTPUT = 20000  # characters of its output (the end)
TEST_TIMEOUT = 240
FULL_TIMEOUT = 900  # seconds Relay's whole check of a kind being made may take (a few real turns)
IN_A_ROW = 3  # failed runs in a row that make a provider look broken (failing_providers)
# Failures that pass, or are the provider's business rather than Relay's support layer's.
PASSING = re.compile(r"usage limit|rate.?limit|\b429\b|\b50[234]\b|unavailable|not available in|overloaded|"
                     r"timed? ?out|timeout|connection|network|quota|credits?\b|billing|try again|was stopped|"
                     r"interrupted", re.I)
# A lost login (expired and not renewed, signed out, a key refused): the user's to sign in again, not a repair
LOGIN_LOST = re.compile(r"failed to authenticate|oauth|not (logged|signed) in|(log|sign) ?in again|please run /login|"
                        r"authentication_error|invalid (x-)?api.?key|\b401\b|unauthori[sz]ed", re.I)


# What a repair is while its agent may change it: a repair's, and a new provider's of relay.creations
WORKING = ("working", "reviewed", "planning", "planned", "building", "trying")


class RepairError(Exception):
    pass


def repairs_dir() -> Path:
    return config.data_dir() / "repairs"


def fingerprint() -> dict[str, str]:
    """Relay's own source: path -> sha256."""
    found = {}
    for package in PACKAGES:
        for path in sorted((SOURCE / package).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                found[str(path.relative_to(SOURCE))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def failing_providers(db: Database) -> dict[str, list[str]]:
    """Providers whose last IN_A_ROW finished runs all failed, none of them on a limit or something
    that passes (PASSING): {provider: those runs' errors, the latest first}. Maybe its support layer
    broke (the program behind it changed); maybe not: whoever is told looks."""
    found = {}
    for row in db.all("SELECT DISTINCT provider_id FROM runs WHERE provider_id IS NOT NULL"):
        runs = db.all("SELECT status, error FROM runs WHERE provider_id = ? AND status IN ('succeeded', 'failed') "
                      "ORDER BY COALESCE(finished, created) DESC LIMIT ?", (row["provider_id"], IN_A_ROW))
        errors = [r["error"] or "it failed without saying why" for r in runs]
        if len(runs) == IN_A_ROW and all(r["status"] == "failed" for r in runs) \
                and not any(PASSING.search(e) for e in errors):
            found[row["provider_id"]] = errors
    return found


class Repairs:
    def __init__(self, db: Database, registry: Registry) -> None:
        self.db = db
        self.registry = registry
        self.lock = threading.RLock()
        self.boxes: Any = None  # Relay's ProviderBoxes (relay.boxes): its tests run in a box of theirs (shadow_test)

    # Starting one

    def prepare_folder(self, repair_id: str, template: Any, note: str = "") -> str:
        """A repair's folder: copies of the provider's support files, Relay's source to read, the originals kept (for
        comparison and undo), and REPAIR.md; its first commit, which is returned. The caller (relay.creations: a
        repair's page) starts its agent there."""
        files = KINDS[template.kind].support_files()
        if not files:
            raise RepairError(f"{template.name} has no support layer Relay can have repaired")
        folder = repairs_dir() / repair_id
        backup = _backup(repair_id)
        for relative in files:
            _copy(SOURCE / relative, folder / relative)
        _reference(folder)
        for package in PACKAGES:
            shutil.copytree(SOURCE / package, backup / package, ignore=shutil.ignore_patterns("__pycache__"))
        (backup / "fingerprint.json").write_text(json.dumps(fingerprint()))
        for path in backup.rglob("*"):
            if path.is_file():
                path.chmod(0o444)  # the way back stays as it was
        (folder / "REPAIR.md").write_text(self._brief(template, files, note))
        _git(folder, "init", "-q")
        _git(folder, "add", "-A")
        _git(folder, "-c", "user.name=Relay", "-c", "user.email=relay@localhost", "-c", "commit.gpgsign=false",
             "commit", "-q", "--no-verify", "-m", "Before the repair")
        return _git(folder, "rev-parse", "HEAD").strip()

    def trouble(self, template: Any) -> dict[str, Any]:
        """What's wrong with a provider, as Relay sees it: its check (or how it fails), its models' statuses and its
        latest errors; whether the latest says its login is lost (LOGIN_LOST)."""
        try:
            check = {"ok": True, "message": self.registry.provider(template.id).check()}
        except Exception as exc:  # noqa: BLE001 - that's what we're here for
            check = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        failures = self.db.all(
            "SELECT error, finished FROM runs WHERE provider_id = ? AND status = 'failed' AND error IS NOT NULL "
            "ORDER BY finished DESC LIMIT 5", (template.id,))
        return {"check": check, "models": health.statuses(self.db, template.id),
                "errors": [{"error": f["error"][:1000], "when": f["finished"]} for f in failures],
                "login_lost": bool(failures) and bool(LOGIN_LOST.search(failures[0]["error"]))}

    def _brief(self, template, files: tuple[str, ...], note: str) -> str:
        found = self.trouble(template)
        programs = getattr(KINDS[template.kind], "PROGRAMS", ()) or ()
        program = programs[0] if programs else "<its program>"
        lines = [f"# Repair: {template.name} ({template.kind})", "",
                 f"Relay talks to {template.name} through a support layer that turns the program's own output",
                 "into Relay's uniform events. It seems broken, maybe because the program changed. Find out what's",
                 "wrong and hand in a plan to fix it first; once the user approves it, fix it.", "",
                 "## What's wrong", ""]
        if note:
            lines += [f"The user says: {note}", ""]
        lines += [f"Relay's check of the provider: {'it says' if found['check']['ok'] else 'it fails'}: "
                  f"{found['check']['message']}", ""]
        for key, entry in found["models"].items():
            lines.append(f"- {key or 'default model'}: {'worked' if entry['ok'] else 'failed'} "
                         f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(entry['checked']))}"
                         + (f": {entry['message']}" if entry.get("message") else ""))
        if found["errors"]:
            lines += ["", "Its latest errors:"] + [f"- {f['error'][:400]}" for f in found["errors"]]
        lines += ["", "## What you may change", "",
                  "Only these files, in this folder (copies; Relay takes them over after checks):", ""]
        lines += [f"- `{f}`" for f in files]
        if template.home:
            lines += ["", f"Its program is installed in its own home (`{template.home}`, its box's HOME), with its",
                      "login. If the program itself is the problem (an old version, a lost login), updating it or",
                      "logging in again is the user's: they have a shell in that home on the repair's page. Say so,",
                      "with what you found."]
        elif has_shell(template):
            sign_in = getattr(KINDS[template.kind], "SIGN_IN", None)
            lines += ["", "If the program behind the provider itself is the problem (an old version, a lost login),",
                      "you can't fix that from here: say so, with what you found. Signing in again is the user's, in",
                      "a shell in its box: on the repair's page (and in Settings → Providers)"
                      + (f", with `{sign_in}`." if sign_in else "."),
                      "`repair_run` has no terminal: it can't sign in."]
        else:
            lines += ["", "If the program behind the provider itself is the problem (an old version, a lost login),",
                      "you can't fix that from here (its program is this machine's): say so, with what you found, and",
                      "the user updates it or signs in again."]
        lines += ["", "## What you may not change", "",
                  "Anything else: Relay's own code, and the files in `reference/`. Relay only takes the files",
                  "above from this folder. Changes to Relay itself are reported, not automatically undone.", "",
                  "## How it fits together", "",
                  "`reference/` is all of Relay's source as it runs now (read-only): how the files above are used.",
                  "`reference/docs/ADDING-A-PROVIDER.md` is the guide to a provider's layer.", "",
                  "- `reference/relay/providers/base.py`: the provider interface (Provider, ProviderRun, Context).",
                  "- `reference/opencode_manager/bridge/events.py`: the uniform events a run yields.", "",
                  "## Where things run", "",
                  "Your commands run in a container of this folder's: the program isn't in it. Relay's tools for a",
                  "repair do what needs more, outside it (MCP tools, or `relay repair …` in a shell):", "",
                  f"- `repair_run` (`relay repair run -- {program} --help`): a command where the program runs, in",
                  "  its own box with its login, as Relay runs it: its version, its --help, a tiny prompt to see what",
                  "  it outputs now. Keep prompts few and short: they may cost the user.",
                  "- `provider_plan` (`relay repair plan`, the plan on stdin): hand in your plan.",
                  "- `repair_check` (`relay repair check`): your version of the files above, tested in a copy of",
                  "  Relay: the provider's check and a tiny prompt, in a box of its own with the program, its login",
                  "  and what the provider's box_mounts() names (not the machine's keys or Relay's data).", "",
                  "## First: your plan", "",
                  "Find what's wrong (its errors above; the program as it is now, with repair_run). Then hand in your",
                  "plan with `provider_plan`, for the user: short and plain, what's wrong and what you'll change. Then",
                  "stop: the user approves it, asks for another, or cancels.", "",
                  "## Then: the repair (once approved)", "",
                  "Change only the files above, and test with `repair_check` until it passes. Say briefly what was",
                  "wrong and what you changed. Relay then checks it fully and starts a throwaway Relay with it, for",
                  "the user to try before they apply it; if they find something wrong, they tell you here."]
        return "\n".join(lines) + "\n"

    # The record

    def get(self, user_id: int, repair_id: str) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM repairs WHERE id = ? AND user_id = ?", (repair_id, user_id))
        if row is None:
            raise RepairError("No such repair")
        return {**row, "review": json.loads(row["review"]) if row.get("review") else None}

    def for_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM repairs WHERE conversation_id = ?", (conversation_id,))
        return {**row, "review": json.loads(row["review"]) if row.get("review") else None} if row else None

    def attach(self, repair_id: str, conversation_id: str) -> None:
        self.db.execute("UPDATE repairs SET conversation_id = ? WHERE id = ?", (conversation_id, repair_id))

    def working(self, conversation_id: str) -> dict[str, Any] | None:
        """The repair a conversation's agent works on, while it may still change it (its repair tools): a new
        provider's (relay.creations) while it's planned, built and tried."""
        found = self.for_conversation(conversation_id)
        return found if found and found["status"] in WORKING else None

    # What its agent can't do in its WorkBox (relay.delegation: repair_check, repair_run)

    def check(self, repair: dict[str, Any]) -> dict[str, Any]:
        """Its version of the support files tested as a review does (nothing recorded): in a copy of Relay, in a
        test box."""
        if repair.get("creates"):
            folder = Path(repair["folder"])
            if not repair["target_id"] or not _layer(folder, repair["target_id"]):
                raise RepairError("There's nothing to check yet: hand in your plan first (provider_plan)")
            return shadow_test(folder, _layer(folder, repair["target_id"]), None, boxes=self.boxes,
                               new=repair["target_id"], home=_home(repair))
        return shadow_test(Path(repair["folder"]), KINDS[repair["kind"]].support_files(), repair["target_id"],
                           providers_dir=self.registry.directory, boxes=self.boxes)

    def run(self, repair: dict[str, Any], command: str, boxes: Any, timeout: Any = None) -> str:
        """A command in the provider's own box, with its program and login, as Relay runs it: its output (the end)
        and how it ended."""
        from relay import guard
        from relay.boxes import BoxLaunch
        from relay.containers import SCRATCH
        command = str(command or "").strip()
        if not command:
            raise RepairError("Say the command, e.g. its --version")
        blocked = guard.check(command)
        if blocked:
            raise RepairError(f"Relay blocked this command: {blocked}")
        try:
            seconds = max(1, min(int(timeout or RUN_TIMEOUT), RUN_LONGEST))
        except (TypeError, ValueError):
            seconds = RUN_TIMEOUT
        if repair.get("creates") and repair.get("home"):  # (its program where the user installed it: its setup box)
            if boxes is None:
                raise RepairError("Running it needs Relay's boxes (podman), which this Relay hasn't")
            home = Path(repair["home"])
            launch = BoxLaunch(boxes.setup(f"new-{repair['id']}", home))
            return _ran(launch, launch.wrap(["/usr/lib/relay/bash.real", "-c", command], home, {}), seconds)
        if repair.get("creates"):  # (no box of its own yet: a test box with its program and its mounts so far)
            if boxes is None:
                raise RepairError("Running it needs Relay's boxes (podman), which this Relay hasn't")
            folder = Path(repair["folder"])
            return box_run(boxes, folder, _layer(folder, repair["target_id"]), repair["target_id"], repair["program"],
                           command, seconds)
        provider = self.registry.provider(repair["target_id"])
        key = provider.box_key() if boxes is not None else None
        if not key:
            raise RepairError(f"{provider.template.name}'s program doesn't run in a box of Relay's here")
        mounts, home = provider.box_mounts(), provider.box_home()
        launch = BoxLaunch(boxes.ensure(key, mounts, None, busy=True, image=provider.box_image(), home=home))
        # Its program where it's mounted (the machine's path), or in its home, on PATH; its shells are Relay's
        # (they hand a command to a WorkBox): the real bash for this one. A box of the WorkBox image has its own.
        env = {**provider.env(), "PATH": _path_for(mounts, home)}
        SCRATCH.mkdir(exist_ok=True)
        return _ran(launch, launch.wrap([provider.box_shell(), "-c", command], SCRATCH, env), seconds)

    # Reviewing, applying, undoing

    def review(self, user_id: int, repair_id: str, model: str | None = None,
               status: str = "reviewed", full: bool = False) -> dict[str, Any]:
        """`status`: what it is once reviewed (on its page, relay.creations: as it was); `full`: tested by Relay's
        whole check of it (relay.providers.conformance) rather than its check and a prompt."""
        with self.lock:
            return self._review(user_id, repair_id, model, status, full)

    def _review(self, user_id: int, repair_id: str, model: str | None = None,
                status: str = "reviewed", full: bool = False) -> dict[str, Any]:
        repair = self.get(user_id, repair_id)
        if repair["status"] not in WORKING:
            raise RepairError(f"This repair is {repair['status']}")
        if repair.get("creates"):
            return self._review_new(repair, model, status)
        folder = Path(repair["folder"])
        files = KINDS[repair["kind"]].support_files()
        left = self._source_changes(repair_id)
        # Read each candidate once: diff, compilation, testing and apply all use
        # these same bytes, even if the agent keeps editing the working folder.
        snapshot_id = uuid.uuid4().hex
        snapshot = _reviewed(repair_id, snapshot_id)
        for relative in files:
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((folder / relative).read_bytes())
            target.chmod(0o444)
        changed = [f for f in files if (snapshot / f).read_bytes() != (_backup(repair_id) / f).read_bytes()]
        # Relay commits every turn, so compare with where the repair started.
        touched = set(_git(folder, "diff", "--name-only", repair["base_commit"]).split("\n")) | \
            set(_git(folder, "ls-files", "--others", "--exclude-standard").split("\n"))
        others = sorted(t for t in touched if t and t not in files)
        diff = "".join(
            "".join(difflib.unified_diff((_backup(repair_id) / f).read_text().splitlines(True),
                                         (snapshot / f).read_text().splitlines(True), f"a/{f}", f"b/{f}"))
            for f in changed)
        compiled = []
        for relative in (f for f in changed if f.endswith(".py")):  # (a provider's script may be another language)
            try:
                compile((snapshot / relative).read_text(), relative, "exec")
            except SyntaxError as exc:
                compiled.append(f"{relative}, line {exc.lineno}: {exc.msg}")
        test = (shadow_test(snapshot, files, repair["target_id"], model, self.registry.directory, self.boxes,
                            full=full)
                if changed and not compiled else None)
        review = {"changed": changed, "ignored": others, "restored": [], "left_alone": left, "diff": diff,
                  "compile_errors": compiled, "test": test, "when": time.time(),
                  "snapshot": snapshot_id,
                  "fingerprints": {f: hashlib.sha256((snapshot / f).read_bytes()).hexdigest() for f in changed}}
        self.db.execute("UPDATE repairs SET review = ?, status = ? WHERE id = ?",
                        (json.dumps(review), status, repair_id))
        return self.get(user_id, repair_id)

    def _review_new(self, repair: dict[str, Any], model: str | None, status: str = "reviewed") -> dict[str, Any]:
        """A kind being made: its folder's files (all new), snapshotted as a repair's are, compiled, and tested
        in a test box by Relay's whole check of it."""
        folder, name = Path(repair["folder"]), repair["target_id"]
        files = _layer(folder, name)
        snapshot_id = uuid.uuid4().hex
        snapshot = _reviewed(repair["id"], snapshot_id)
        for relative in files:
            target = snapshot / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((folder / relative).read_bytes())
            target.chmod(0o444)
        touched = set(_git(folder, "diff", "--name-only", repair["base_commit"]).split("\n")) | \
            set(_git(folder, "ls-files", "--others", "--exclude-standard").split("\n"))
        # (outside its folder; what its agent removed of the example's isn't "not taken over")
        others = sorted(t for t in touched if t and not t.startswith(f"relay/providers/{name}/"))
        diff, compiled = "", []
        for relative in files:
            try:
                text = (snapshot / relative).read_text()
            except UnicodeDecodeError:
                continue  # (not text: listed, not shown)
            diff += "".join(difflib.unified_diff([], text.splitlines(True), "/dev/null", f"b/{relative}"))
            if relative.endswith(".py"):
                try:
                    compile(text, relative, "exec")
                except SyntaxError as exc:
                    compiled.append(f"{relative}, line {exc.lineno}: {exc.msg}")
        test = (shadow_test(snapshot, files, None, model, boxes=self.boxes, new=name, home=_home(repair))
                if files and not compiled else None)
        review = {"changed": list(files), "ignored": others, "restored": [],
                  "left_alone": self._source_changes(repair["id"]), "diff": diff, "compile_errors": compiled,
                  "test": test, "when": time.time(), "snapshot": snapshot_id, "new": name,
                  "kind": (test or {}).get("kind"), "mounts": (test or {}).get("mounts"),
                  "template": (test or {}).get("template"),
                  "fingerprints": {f: hashlib.sha256((snapshot / f).read_bytes()).hexdigest() for f in files}}
        self.db.execute("UPDATE repairs SET review = ?, status = ?, kind = ? WHERE id = ?",
                        (json.dumps(review), status, review["kind"] or "", repair["id"]))
        return self.get(repair["user_id"], repair["id"])

    def _source_changes(self, repair_id: str) -> list[str]:
        """Report changes, including deletions; timestamps cannot identify who made them."""
        before = json.loads((_backup(repair_id) / "fingerprint.json").read_text())
        now = fingerprint()
        return sorted(f for f in before.keys() | now.keys() if before.get(f) != now.get(f))

    def _snapshot(self, repair: dict[str, Any]) -> Path:
        review = repair["review"]
        if not review.get("snapshot") or not review.get("fingerprints"):
            raise RepairError("Check the changes again to record the reviewed version")
        snapshot = _reviewed(repair["id"], review["snapshot"])
        for relative in review["changed"]:
            path = snapshot / relative
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != review["fingerprints"][relative]:
                raise RepairError("The reviewed snapshot changed or is missing; check the changes again")
        return snapshot

    def apply(self, user_id: int, repair_id: str, anyway: bool = False) -> dict[str, Any]:
        with self.lock:
            return self._apply(user_id, repair_id, anyway)

    def _apply(self, user_id: int, repair_id: str, anyway: bool = False) -> dict[str, Any]:
        repair = self.get(user_id, repair_id)
        review = repair["review"]
        if repair["status"] not in ("reviewed", "trying") or not review:  # (trying: a new provider, tried)
            raise RepairError("Check the changes first")
        if not review["changed"]:
            raise RepairError("There's nothing to apply")
        if review["compile_errors"]:
            raise RepairError("The changed files don't compile")
        if not (review["test"] or {}).get("ok") and not anyway:
            raise RepairError("The test didn't pass; apply it anyway only if you're sure")
        snapshot = self._snapshot(repair)
        if repair.get("creates"):
            home = SOURCE / "relay" / "providers" / repair["target_id"]
            if home.exists():
                raise RepairError(f"relay/providers/{repair['target_id']}/ is in Relay meanwhile; left it alone")
            for relative in review["changed"]:
                _copy(snapshot / relative, SOURCE / relative)
            self.db.execute("UPDATE repairs SET status = 'applied', applied = ? WHERE id = ?", (time.time(), repair_id))
            return self.get(user_id, repair_id)
        backup = _backup(repair_id)
        for relative in review["changed"]:
            current = SOURCE / relative
            if not current.is_file() or current.read_bytes() != (backup / relative).read_bytes():
                raise RepairError(f"{relative} changed in Relay meanwhile; start a new repair")
        for relative in review["changed"]:
            _copy(snapshot / relative, SOURCE / relative)
        self.db.execute("UPDATE repairs SET status = 'applied', applied = ? WHERE id = ?", (time.time(), repair_id))
        return self.get(user_id, repair_id)

    def undo(self, user_id: int, repair_id: str) -> dict[str, Any]:
        with self.lock:
            return self._undo(user_id, repair_id)

    def _undo(self, user_id: int, repair_id: str) -> dict[str, Any]:
        repair = self.get(user_id, repair_id)
        if repair["status"] != "applied":
            raise RepairError("Only an applied repair can be undone")
        review = repair["review"]
        if not review.get("fingerprints"):
            raise RepairError("This old repair has no record of the applied version; undo it manually")
        # Check all files before restoring any, so a conflict leaves everything alone.
        for relative in review["changed"]:
            current = SOURCE / relative
            if (not current.is_file()
                    or hashlib.sha256(current.read_bytes()).hexdigest() != review["fingerprints"][relative]):
                raise RepairError(f"{relative} changed in Relay after applying the repair; left it alone")
        if repair.get("creates"):  # (its files out again, and its folder when nothing else is in it)
            home = SOURCE / "relay" / "providers" / repair["target_id"]
            for relative in review["changed"]:
                (SOURCE / relative).unlink()
            for cache in sorted(home.rglob("__pycache__"), reverse=True):
                shutil.rmtree(cache, ignore_errors=True)
            for empty in sorted((p for p in [home, *home.rglob("*")] if p.is_dir()), key=lambda p: -len(p.parts)):
                if not any(empty.iterdir()):
                    empty.rmdir()
        else:
            for relative in review["changed"]:
                _copy(_backup(repair_id) / relative, SOURCE / relative)
        self.db.execute("UPDATE repairs SET status = 'undone' WHERE id = ?", (repair_id,))
        return self.get(user_id, repair_id)

    def discard(self, user_id: int, repair_id: str) -> dict[str, Any]:
        with self.lock:
            return self._discard(user_id, repair_id)

    def _discard(self, user_id: int, repair_id: str) -> dict[str, Any]:
        repair = self.get(user_id, repair_id)
        if repair["status"] == "applied":
            raise RepairError("Undo it first")
        self.db.execute("UPDATE repairs SET status = 'discarded' WHERE id = ?", (repair_id,))
        return self.get(user_id, repair_id)


def _reference(folder: Path) -> None:
    """Relay as it is, to read (`reference/`, read-only): all of it, as its agent can't read Relay's own folder (it
    isn't in its WorkBox), and four files of it said too little (a repair's agent searched Relay's source in vain);
    and the guide to adding a provider."""
    for package in PACKAGES:
        shutil.copytree(SOURCE / package, folder / "reference" / package,
                        ignore=shutil.ignore_patterns("__pycache__", "static"))
    guide = SOURCE.parent / "docs" / "ADDING-A-PROVIDER.md"
    if guide.is_file():
        _copy(guide, folder / "reference" / "docs" / guide.name)
    for path in (folder / "reference").rglob("*"):
        if path.is_file():
            path.chmod(0o444)


def _layer(folder: Path, name: str) -> tuple[str, ...]:
    """A kind being made's files (relative to its folder: relay/providers/<name>/…), its tests too."""
    layer = folder / "relay" / "providers" / name
    return tuple(sorted(str(path.relative_to(folder)) for path in layer.rglob("*")
                        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"))


def _backup(repair_id: str) -> Path:
    return repairs_dir() / f"{repair_id}.relay"


def _reviewed(repair_id: str, snapshot_id: str) -> Path:
    return repairs_dir() / f"{repair_id}.reviewed" / snapshot_id


def _ran(launch: Any, argv: list[str], seconds: int) -> str:
    """A command in a box, run: its output (the end) and how it ended; ended in its box when it took too long."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=seconds, stdin=subprocess.DEVNULL,
                              check=False)
    except subprocess.TimeoutExpired as exc:
        launch.end()
        output, ended = _text(exc.stdout) + _text(exc.stderr), f"stopped after {seconds} s"
    else:
        output, ended = done.stdout + done.stderr, f"exit {done.returncode}"
    said = (("[…]\n" if len(output) > RUN_OUTPUT else "") + output[-RUN_OUTPUT:]).rstrip()
    return f"{said}\n[{ended}]" if said else f"[{ended}]"


def _home(repair: dict[str, Any]) -> Path | None:
    """A new provider's home (relay.creations): where the user installed its program."""
    return Path(repair["home"]) if repair.get("home") else None


def _text(output: str | bytes | None) -> str:
    return output.decode(errors="replace") if isinstance(output, bytes) else output or ""


def _copy(source: Path, target: Path, read_only: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.chmod(0o644)
    shutil.copyfile(source, target)
    if read_only:
        target.chmod(0o444)


def _git(folder: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=folder, capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise RepairError(f"git {args[0]}: {done.stderr.strip()}")
    return done.stdout


def shadow_test(folder: Path, files: tuple[str, ...], target_id: str | None, model: str | None = None,
                providers_dir: Path | None = None, boxes: Any = None, new: str | None = None,
                home: Path | None = None, full: bool = False) -> dict[str, Any]:
    """The provider's check and a tiny prompt, with the folder's files in a copy of Relay, in a test box
    (Boxes.test): the files are an agent's, so their code runs in no other place. First the box says what the
    provider would have mounted; what a test may not mount (boxes.refused_mount) ends it there. Its template is
    the only one there (not the user's others, with their keys).
    `new`: a kind being made (a creation: relay/providers/<new>/ among the files), tested with its EXAMPLE as its
    template, by Relay's whole check of it (relay.providers.conformance) rather than the check and a prompt; `home`:
    the home its program was installed in (Template.home), in that template. `full`: the whole check for a provider
    Relay has too (a repair on its page)."""
    failed = {"ok": False, "check": None, "prompt": None, "mounts": []}
    if boxes is None:
        return {**failed, "error": "Testing it needs Relay's boxes (podman), which this Relay hasn't"}
    with _shadow(folder, files) as (shadow, scratch):
        if new is None:
            template = Path(providers_dir or config.providers_dir()) / f"{target_id}.json"
            shutil.copyfile(template, scratch / "providers" / template.name)
        asked = _asked(boxes, shadow, scratch, target_id, new, home)
        if asked.get("error"):
            return {**failed, "mounts": asked.get("shown", []), "error": asked["error"]}
        full = full or new is not None
        script = SHADOW_TEST.format(providers=str(scratch / "providers"), target=asked["target"],
                                    model=json.dumps(model or ""), scratch=str(scratch / "run"), full=full)
        timeout = FULL_TIMEOUT if full else TEST_TIMEOUT
        done = _in_box(boxes, script, asked["mounts"], asked["env"], scratch, asked.get("image"), timeout)
        return {**done, "mounts": asked["shown"], "kind": asked.get("kind"), "template": asked.get("template")}


def box_run(boxes: Any, folder: Path, files: tuple[str, ...], new: str, program: str, command: str,
            timeout: int, home: Path | None = None) -> str:
    """A command where a kind being made would run its program (a creation's repair_run): in a test box with the
    program and what its layer's box_mounts() names so far (checked as its tests are). Its output (the end) and how
    it ended."""
    with _shadow(folder, files) as (shadow, scratch):
        asked = _asked(boxes, shadow, scratch, None, new, home)
        mounts, env = asked.get("mounts") or _base(shadow, scratch), asked.get("env") or _env(shadow, scratch)
        found = shutil.which(program)
        if found and not any(path == Path(found) for path, _ in mounts):
            mounts = [*mounts, (Path(found), False)]
            env["PATH"] = ":".join(dict.fromkeys([str(Path(found).parent), *env.get("PATH", "").split(":")]))
        noted = "" if not asked.get("error") else f"[its layer doesn't load yet, so only its program is here: " \
                                                  f"{asked['error'][:300]}]\n"
        try:
            done = boxes.test(["bash", "-c", command], mounts, env, scratch, asked.get("image"), timeout)
        except subprocess.TimeoutExpired as exc:
            output, ended = _text(exc.stdout) + _text(exc.stderr), f"stopped after {timeout} s"
        else:
            output, ended = done.stdout + done.stderr, f"exit {done.returncode}"
    said = (("[…]\n" if len(output) > RUN_OUTPUT else "") + output[-RUN_OUTPUT:]).rstrip()
    return noted + (f"{said}\n[{ended}]" if said else f"[{ended}]")


@contextlib.contextmanager
def _shadow(folder: Path, files: tuple[str, ...]):
    """A copy of Relay with the folder's files, and a scratch folder (with an empty providers folder) beside it."""
    with tempfile.TemporaryDirectory(prefix="relay-repair-") as shadow, \
            tempfile.TemporaryDirectory(prefix="relay-test-") as scratch:
        shadow, scratch = Path(shadow), Path(scratch)
        for package in PACKAGES:
            shutil.copytree(SOURCE / package, shadow / package, ignore=shutil.ignore_patterns("__pycache__"))
        for relative in files:
            (shadow / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(folder / relative, shadow / relative)
        (scratch / "providers").mkdir()
        yield shadow, scratch


def _base(shadow: Path, scratch: Path) -> list[tuple[Path, bool]]:
    return [(shadow, False), (SOURCE.parent / "vendor", False), (scratch, True)]


def _env(shadow: Path, scratch: Path) -> dict[str, str]:
    return {"HOME": str(Path.home()), "RELAY_HOME": str(config.data_dir()), "XDG_CACHE_HOME": str(scratch / "cache"),
            "PYTHONPATH": os.pathsep.join([str(shadow), str(SOURCE.parent / "vendor")]), "PYTHONDONTWRITEBYTECODE": "1",
            "PATH": "/usr/local/bin:/usr/bin:/bin"}


def _asked(boxes: Any, shadow: Path, scratch: Path, target_id: str | None, new: str | None,
           home: Path | None = None) -> dict[str, Any]:
    """What the provider would have mounted, asked in a test box, and checked: {mounts (the test's, with the copy
    of Relay), env, image, target, kind, shown (its own mounts)}, or {error}. For a kind being made, its EXAMPLE is
    written as its template first (in the box), with `home`. A provider's own home (Provider.box_home) is mounted
    and made its HOME, as in its box."""
    from relay.boxes import home_env, refused_mount
    env = _env(shadow, scratch)
    script = MOUNTS_ASKED.format(providers=str(scratch / "providers"), target=target_id, new=new,
                                 home=str(home) if home else "")
    asked = _in_box(boxes, script, _base(shadow, scratch), env, scratch, None, TEST_TIMEOUT)
    if "mounts" not in asked:
        return {"error": asked.get("error") or "It said nothing of its box"}
    mounts = []
    for path, writable in asked["mounts"]:
        path = Path(path)
        if not path.is_absolute():  # (a program by its name: the box hasn't this machine's PATH to find it on)
            found = shutil.which(str(path))
            if found is None:
                continue  # (not installed: its test says so)
            path = Path(found)
        mounts.append((path, bool(writable)))
    own = Path(asked["home"]) if asked.get("home") else None
    if own is not None and own.is_dir():
        mounts.append((own, True))
    shown = [[str(path), writable] for path, writable in mounts]
    refused = [f"{path} ({why})" for path, writable in mounts
               if (why := refused_mount(path, writable, boxes.settings))]
    if refused:
        return {"shown": shown, "error": "It asks to have mounted what its test may not: " + "; ".join(refused)}
    # Its programs where they're mounted (the machine's paths), or in its home, on PATH, as in its box
    if own is not None:
        env.update(home_env(own))
    env["PATH"] = _path_for(mounts, own)
    return {"mounts": _base(shadow, scratch) + mounts, "env": env, "image": asked.get("image"), "shown": shown,
            "target": asked["target"], "kind": asked.get("kind"), "template": asked.get("template")}


def _path(programs: list[str], home: Path | None) -> str:
    """PATH in a provider's box (or its test box): its programs' folders, then its home's (relay.boxes.home_env)."""
    from relay.boxes import home_env
    rest = home_env(home)["PATH"].split(":") if home is not None else ["/usr/local/bin", "/usr/bin", "/bin"]
    return ":".join(dict.fromkeys([*programs, *rest]))


def _path_for(mounts: list[tuple[Path, bool]], home: Path | None) -> str:
    """PATH in a provider's box, of what it mounts (Provider.box_mounts): its programs where they're mounted."""
    return _path([str(path.parent) for path, _ in mounts if path.is_file() and os.access(path, os.X_OK)], home)


def _in_box(boxes: Any, script: str, mounts: list[tuple[Path, bool]], env: dict[str, str], cwd: Path,
            image: str | None, timeout: int) -> dict[str, Any]:
    """One of the scripts below, in a test box: what it printed last, as JSON."""
    try:
        done = boxes.test(["python3", "-c", script], mounts, env, cwd, image, timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"No result within {timeout} s"}
    except Exception as exc:  # noqa: BLE001 - its box couldn't be made: said, as a test that failed
        return {"ok": False, "error": f"Its test box: {exc}"}
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if not lines:
        return {"ok": False, "error": (done.stderr or done.stdout)[-1500:] or f"It ended with {done.returncode}"}
    return json.loads(lines[-1])


# In the test box: what the provider would have mounted in its box, its box's image and its own home. A kind being
# made (new): its folder's kind, with its EXAMPLE written as its template (with the home its program is in).
MOUNTS_ASKED = '''
import json
from pathlib import Path
from relay.providers import BROKEN, KINDS, Registry, parse_template
try:
    providers, target, new, home = Path({providers!r}), {target!r}, {new!r}, {home!r}
    if new:
        if f"providers/{{new}}" in BROKEN:
            raise RuntimeError(f"relay/providers/{{new}}/ can't be loaded: {{BROKEN['providers/' + new]}}")
        kinds = [kind for kind in KINDS.values() if kind.__module__.startswith(f"relay.providers.{{new}}.")]
        if not kinds:
            raise RuntimeError(f"relay/providers/{{new}}/ adds no kind (KINDS in its __init__.py)")
        template = parse_template(dict(kinds[0].EXAMPLE))
        target = template.id
        (providers / f"{{target}}.json").write_text(json.dumps({{**template.to_dict(), "enabled": True,
                                                                **({{"home": home}} if home else {{}})}}))
    provider = Registry(providers).provider(target)
    print(json.dumps({{"mounts": [[str(path), bool(writable)] for path, writable in provider.box_mounts()],
                      "image": provider.box_image(), "home": str(provider.box_home() or ""), "target": target,
                      "kind": provider.KIND, "template": template.to_dict() if new else None}}))
except Exception as exc:
    print(json.dumps({{"error": f"{{type(exc).__name__}}: {{exc}}"}}))
'''


SHADOW_TEST = '''
import json
from pathlib import Path
from relay import health
from relay.providers import Registry, conformance
result = {{"ok": False, "check": None, "prompt": None, "error": None}}
try:
    provider = Registry(Path({providers!r})).provider({target!r})
    if {full}:  # (a kind being made: Relay's whole check of it)
        result["results"] = conformance.check(provider, {model}, Path({scratch!r}))
        result["ok"] = all(scenario["ok"] is not False for scenario in result["results"])
    else:
        try:
            result["check"] = {{"ok": True, "message": provider.check()}}
        except Exception as exc:
            result["check"] = {{"ok": False, "message": f"{{type(exc).__name__}}: {{exc}}"}}
        result["prompt"] = health.test(provider, {model}, Path({scratch!r}))
        result["ok"] = result["check"]["ok"] and result["prompt"]["ok"]
except Exception as exc:
    result["error"] = f"{{type(exc).__name__}}: {{exc}}"
print(json.dumps(result))
'''
