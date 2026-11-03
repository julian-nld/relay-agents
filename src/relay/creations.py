"""Adding a provider Relay doesn't know, with an agent (Settings → Add provider → "A provider Relay doesn't know" →
Add it with an agent; AgentOS's Governor sends the user there: installing and logging in are theirs). Three steps, on
a page of its own (/providers/new/<id>):

1. **Install and log in.** The user installs the program in a box of its own (Boxes.setup: Relay's box image, its
   shells real ones) and logs in, in a shell on the page; the provider's home (Template.home) keeps both. Then they
   say what they installed, and choose the agent that integrates it.
2. **Integration.** That agent studies the program there (repair_run) and hands in a plan (provider_plan): the user
   approves it, asks for another (Redraft) or cancels (Deny).
3. **Finalisation.** Approved, it builds the layer (relay/providers/<name>/, in a folder of its own: a creation of
   relay.repair's) and tests it (repair_check) until it passes. Relay checks it fully in a test box, and starts a
   trial Relay with it (Trial): a Relay of its own, at /trial/<id>/ of this one (relay.forward), with only the new
   provider, for the user to try. Add puts it in Relay (its folder, its template with the home); Relay restarts.

    setup ─Continue─▶ planning ─plan─▶ planned ─Approve─▶ building ─checked─▶ trying ─Add─▶ applied ─▶ undone
                         ▲  Redraft ◀──────┘                  ▲   Something's wrong ◀┘
    Deny, Cancel: discarded (its boxes, home, folder and trial gone)

**Repairing a provider Relay has** (Settings → its 🔧; AgentOS's Governor's provider.repair) goes the same way, on
its own page (/providers/repair/<id>): 1. what's wrong (Relay's view of it, a shell in a box with its home when it has
one, the user's words); 2. its agent's plan; 3. the repair (copies of its support files: relay.repair), Relay's whole
check of it with its own template, a trial Relay with it, and Apply (its originals kept, for Undo).

Each is a row of `repairs` (`creates`: a new provider; `page`: made on its page, not the old way). The trial Relay
runs the new or repaired layer as the user, outside a box, as this Relay does once it's added (SECURITY.md); it starts
only once the layer passed Relay's whole check in a test box.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from relay import config
from relay.repair import (
    PACKAGES,
    SOURCE,
    RepairError,
    _backup,
    _git,
    _layer,
    _path_for,
    _reference,
    _reviewed,
    fingerprint,
    repairs_dir,
)

if TYPE_CHECKING:
    from relay.app import Relay

NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
GOING = ("setup", "planning", "planned", "building", "trying")  # (one on its page, not ended yet)
TRIAL_START = 90  # seconds a trial Relay may take to answer
PLAN_LONGEST = 20000  # characters of a plan


def homes_dir() -> Path:
    """Providers' own homes (Template.home): what the user installed in their boxes, and their logins."""
    return config.data_dir() / "provider-homes"


def trial_dir(creation_id: str) -> Path:
    """A trial Relay's code, data and cache: short (its sockets' paths have a limit), thrown away with it."""
    return Path(tempfile.gettempdir()) / f"relay-trial-{creation_id}"


class Creations:
    def __init__(self, relay: Relay) -> None:
        self.relay = relay
        self.lock = threading.RLock()
        self.trials: dict[str, subprocess.Popen] = {}  # (this process's: one from before a restart has its pid)
        self.checking: set[str] = set()  # creations Relay is checking (the review, then the trial's start)
        self.launch = _launch  # (how a trial Relay is started: tests start something smaller)

    # The record

    def get(self, user_id: int, creation_id: str) -> dict[str, Any]:
        return self.relay.repairs.get(user_id, creation_id)

    def view(self, user_id: int, creation_id: str) -> dict[str, Any]:
        """What its page shows: its record (not the trial's sign-in), its agent's state, the trial's address; a
        repair's provider."""
        found = self.get(user_id, creation_id)
        trial = json.loads(found["trial"]) if found.get("trial") else None
        conversation = found.get("conversation_id")
        from relay.providers import KINDS, TemplateError, has_shell
        provider = None
        if not found.get("creates"):
            try:
                template = self.relay.registry.get(found["target_id"])
                provider = {"id": template.id, "name": template.name, "kind": template.kind, "home": template.home,
                            "sign_in": KINDS[template.kind].SIGN_IN if template.kind in KINDS else None,
                            "shell_hint": KINDS[template.kind].SHELL_HINT if template.kind in KINDS else None,
                            "shell": has_shell(template)}
            except TemplateError:
                provider = {"id": found["target_id"], "name": found["target_id"], "kind": found["kind"], "gone": True}
        return {**{k: v for k, v in found.items() if k != "trial"},
                "legacy": not found.get("page"),
                "provider": provider,
                "page_path": _page(found),
                "working": bool(conversation and self.relay.runs.active_run_for(conversation)),
                "checking": creation_id in self.checking,
                "trial": {"url": f"{self.relay.settings.base_path}/trial/{creation_id}/"} if trial else None,
                "loaded": bool(found.get("kind")) and found["kind"] in KINDS,
                "setup_box": self.relay.boxes.setup_name(_key(found)) if self.relay.boxes and found.get("home")
                else None}

    # 1. Install and log in

    def begin(self, user_id: int) -> dict[str, Any]:
        """A new one: its record, its folder and its home (the setup box comes with its shell)."""
        creation_id = uuid.uuid4().hex[:12]
        folder, home = repairs_dir() / creation_id, homes_dir() / creation_id
        folder.mkdir(parents=True)
        home.mkdir(parents=True)
        self.relay.db.execute(
            "INSERT INTO repairs (id, user_id, target_id, kind, folder, base_commit, status, created, creates, "
            "program, home, page) VALUES (?, ?, '', '', ?, '', 'setup', ?, 1, '', ?, 1)",
            (creation_id, user_id, str(folder), time.time(), str(home)))
        return self.get(user_id, creation_id)

    def begin_repair(self, user_id: int, provider_id: str) -> dict[str, Any]:
        """A repair of a provider Relay has, on its page: its record (its folder comes at Continue). The one going
        already, when there is one."""
        template = self.relay.registry.get(provider_id)
        placeholders = ",".join("?" * len(GOING))
        going = self.relay.db.one(f"SELECT id FROM repairs WHERE user_id = ? AND creates = 0 AND page = 1 AND "
                                  f"target_id = ? AND status IN ({placeholders})", (user_id, template.id, *GOING))
        if going:
            return self.get(user_id, going["id"])
        repair_id = uuid.uuid4().hex[:12]
        self.relay.db.execute(
            "INSERT INTO repairs (id, user_id, target_id, kind, folder, base_commit, status, created, creates, home, "
            "page) VALUES (?, ?, ?, ?, ?, '', 'setup', ?, 0, ?, 1)",
            (repair_id, user_id, template.id, template.kind, str(repairs_dir() / repair_id), time.time(),
             template.home or None))
        return self.get(user_id, repair_id)

    def setup_box(self, creation: dict[str, Any]) -> str:
        """Its setup box with its home, running: the shell's, and where a new provider's repair_run runs."""
        if self.relay.boxes is None:
            raise RepairError("Its shell needs Relay's boxes (podman), which this Relay hasn't")
        return self.relay.boxes.setup(_key(creation), Path(creation["home"]))

    def shell(self, user_id: int, creation_id: str):
        """A terminal in its setup box, in its home (relay.terminal.Terminal): a new provider's, or a repaired one's
        with a home of its own; a repaired one without, in its own box (_in_its_box)."""
        from relay.production import Shell
        from relay.providers import KINDS
        from relay.terminal import Terminal
        creation = self.get(user_id, creation_id)
        if creation["status"] not in GOING:
            raise RepairError(f"This {_what(creation)} is {creation['status']}")
        if not creation.get("home"):
            template = self.relay.registry.get(creation["target_id"])
            return self._in_its_box(KINDS[template.kind](template))
        return Terminal(Shell(self.setup_box(creation)), Path(creation["home"]))

    def _in_its_box(self, provider: Any, draft: bool = False):
        """A terminal in a provider's own box, in a real shell: its program, its login and settings and what runs
        there, as its runs have them. What a program starts from it still goes to a WorkBox (/bin/sh there is
        Relay's). The box stays when the shell goes. `draft`: a provider not added yet (Add provider's next step),
        whose box is made from its template, and goes with its shell if it still isn't added."""
        from relay.production import Shell
        from relay.providers import ProviderError
        from relay.terminal import Terminal
        key = provider.box_key()
        if not key:
            raise RepairError(f"{provider.template.name}'s program doesn't run in a box of Relay's")
        if self.relay.boxes is None:
            raise RepairError("Its shell needs Relay's boxes (podman), which this Relay hasn't")
        try:
            provider.shell_ready()
        except ProviderError as exc:
            raise RepairError(str(exc)) from exc
        mounts = provider.box_mounts()
        box = self.relay.boxes.ensure(key, mounts, None, busy=self._box_in_use(key), image=provider.box_image())
        creations = self

        class DraftShell(Shell):
            def close_terminal(self, name: str, tag: str) -> None:
                super().close_terminal(name, tag)
                creations.drop_unused_box(provider, tag)

        env = {**provider.env(), "PATH": _path_for(mounts, None)}
        return Terminal((DraftShell if draft else Shell)(box, provider.box_shell(), env), Path.home())

    def _box_in_use(self, key: str) -> bool:
        """Whether a provider that's added has this box (opencode's profiles share one)."""
        from relay.providers import KINDS, safely
        return any(template.kind in KINDS and safely(KINDS[template.kind](template).box_key, None) == key
                   for template in self.relay.registry.templates())

    def drop_unused_box(self, provider: Any, leaving: str | None = None) -> None:
        """The box made for a provider not added, once nothing has a use for it: no provider that's added has it, and
        no shell is open in it but the one that's ending (`leaving`, its tag)."""
        key, boxes = provider.box_key(), self.relay.boxes
        if not key or boxes is None or self._box_in_use(key):
            return
        name = boxes.name(key)
        if not any(terminal.container == name and terminal.tag != leaving for terminal in self.relay.terminals):
            boxes.remove(key)

    def provider_shell(self, template: Any, draft: bool = False):
        """A provider's Shell: in its own box (_in_its_box), and `draft`: for one not added yet. With a home of its own
        (Template.home) in a setup box with that home, where shells are real ones all the way down, to update its
        program or log in again, which goes when its shell does."""
        from relay.production import Shell
        from relay.providers import KINDS
        from relay.terminal import Terminal
        if not template.home:
            return self._in_its_box(KINDS[template.kind](template), draft)
        if self.relay.boxes is None:
            raise RepairError("Its shell needs Relay's boxes (podman), which this Relay hasn't")
        boxes = self.relay.boxes
        key, where = f"home-{template.id}", Path(template.home)
        box = boxes.setup(key, where)

        class HomeShell(Shell):
            def close_terminal(self, name: str, tag: str) -> None:
                super().close_terminal(name, tag)
                boxes.remove_setup(key)

        return Terminal(HomeShell(box), where)

    # 2. Integration

    def prepare(self, user_id: int, creation_id: str, about: str) -> dict[str, Any]:
        """Continue: what the user installed (or what's wrong, for a repair), and its agent's folder (the guide,
        Relay's source, CREATE.md; a repair's REPAIR.md and copies of its support files). The caller starts the agent
        there."""
        with self.lock:
            creation = self.get(user_id, creation_id)
            if creation["status"] != "setup":
                raise RepairError(f"This {_what(creation)} is {creation['status']} already")
            about = str(about or "").strip()
            if not creation.get("creates"):
                base = self.relay.repairs.prepare_folder(creation_id, self.relay.registry.get(creation["target_id"]),
                                                         about[:4000])
                self.relay.db.execute("UPDATE repairs SET about = ?, base_commit = ?, status = 'planning' WHERE id = ?",
                                      (about[:4000], base, creation_id))
                return self.get(user_id, creation_id)
            if not about:
                raise RepairError("Say what you installed: the agent goes by it")
            folder = Path(creation["folder"])
            _reference(folder)
            backup = _backup(creation_id)
            backup.mkdir(parents=True, exist_ok=True)
            (backup / "fingerprint.json").write_text(json.dumps(fingerprint()))  # (Relay's own: what changed)
            (folder / "CREATE.md").write_text(_brief(about[:4000], creation["home"]))
            _commit(folder, "What Relay gives its agent")
            self.relay.db.execute("UPDATE repairs SET about = ?, base_commit = ?, status = 'planning' WHERE id = ?",
                                  (about[:4000], _git(folder, "rev-parse", "HEAD").strip(), creation_id))
            return self.get(user_id, creation_id)

    def plan(self, creation: dict[str, Any], name: str, program: str, plan: str) -> str:
        """Its agent's plan (provider_plan): for the user to approve. What the agent is told."""
        with self.lock:
            creation = self.get(creation["user_id"], creation["id"])
            if creation["status"] not in ("planning", "planned"):
                raise RepairError("Your plan is approved already: carry it out" if creation["status"] == "building"
                                  else f"This {_what(creation)} is {creation['status']}")
            name, program, plan = str(name or "").strip(), str(program or "").strip(), str(plan or "").strip()
            if not creation.get("creates"):  # (a repair: the plan alone)
                if not plan:
                    raise RepairError("plan: what's wrong and what you'll change, for the user to read")
                self.relay.db.execute("UPDATE repairs SET plan = ?, status = 'planned' WHERE id = ?",
                                      (plan[:PLAN_LONGEST], creation["id"]))
                return ("Your plan is with the user now. They approve it (then you're asked to make the repair), ask "
                        "for another, or cancel. Stop here: say in a sentence that it's handed in.")
            self._check_name(name, creation["id"])
            if not program or len(program) > 300 or "\n" in program:
                raise RepairError("program: how it's started, e.g. gemini (on its PATH in the box)")
            if not plan:
                raise RepairError("plan: what you'll build, for the user to read")
            self.relay.db.execute("UPDATE repairs SET target_id = ?, program = ?, plan = ?, status = 'planned' "
                                  "WHERE id = ?", (name, program, plan[:PLAN_LONGEST], creation["id"]))
            self.relay.db.execute("UPDATE conversations SET title = ? WHERE id = ? AND title LIKE 'New provider%'",
                                  (f"New provider: {name}", creation["conversation_id"]))  # (its name, known now)
        return ("Your plan is with the user now. They approve it (then you're asked to build it), ask for another, or "
                "cancel. Stop here: say in a sentence that it's handed in.")

    def _check_name(self, name: str, creation_id: str) -> None:
        from relay.providers import KINDS
        if not NAME.fullmatch(name):
            raise RepairError("name: its folder's, lowercase letters, digits and _ (e.g. gemini)")
        if (SOURCE / "relay" / "providers" / name).exists() or name.replace("_", "-") in KINDS:
            raise RepairError(f"name: Relay has a provider folder or kind named {name} already")
        placeholders = ",".join("?" * len(GOING))
        if self.relay.db.one(f"SELECT 1 FROM repairs WHERE creates = 1 AND target_id = ? AND id != ? AND status IN "
                             f"({placeholders})", (name, creation_id, *GOING)):
            raise RepairError(f"name: another new provider is named {name}")

    def approve(self, user_id: int, creation_id: str) -> str:
        """Its plan approved: the guide's example in its folder, to make the program's. What its agent is asked."""
        with self.lock:
            creation = self.get(user_id, creation_id)
            if creation["status"] != "planned" or not creation.get("plan"):
                raise RepairError("There's no plan to approve")
            if not creation.get("creates"):  # (a repair: its copies are there to change)
                self.relay.db.execute("UPDATE repairs SET status = 'building', review = NULL WHERE id = ?",
                                      (creation_id,))
                self.relay.db.execute("UPDATE conversations SET agent_mode = NULL WHERE id = ?",
                                      (creation["conversation_id"],))
                return ("Your plan is approved. Make the repair in the files REPAIR.md names, test it with "
                        "repair_check until it passes, then say briefly what was wrong and what you changed. Relay "
                        "checks it fully and gives the user a trial Relay with it.")
            name = creation["target_id"]
            self._check_name(name, creation_id)
            folder = Path(creation["folder"])
            _seed(folder, name)
            _commit(folder, f"The guide's example, to make {name}'s")
            self.relay.db.execute("UPDATE repairs SET status = 'building', review = NULL, base_commit = ? WHERE id = ?",
                                  (_git(folder, "rev-parse", "HEAD").strip(), creation_id))
            self.relay.db.execute("UPDATE conversations SET agent_mode = NULL WHERE id = ?",
                                  (creation["conversation_id"],))
        return (f"Your plan is approved. Build it in relay/providers/{name}/ (the guide's example is there, copied "
                "for you): CREATE.md says how. Test it with repair_check until it passes, then say briefly what you "
                "made. Relay checks it once more and gives the user a trial Relay with it.")

    def redraft(self, user_id: int, creation_id: str, wanted: str) -> str:
        """Another plan, as the user says. What its agent is asked."""
        wanted = str(wanted or "").strip()
        if not wanted:
            raise RepairError("Say what should be different")
        with self.lock:
            creation = self.get(user_id, creation_id)
            if creation["status"] not in ("planning", "planned"):
                raise RepairError(f"This {_what(creation)} is {creation['status']}")
            self.relay.db.execute("UPDATE repairs SET status = 'planning', plan = NULL WHERE id = ?", (creation_id,))
        return (f"The user wants another plan:\n\n{wanted[:4000]}\n\nHand in the new one with provider_plan, and "
                "stop there.")

    def discard(self, user_id: int, creation_id: str) -> dict[str, Any]:
        """Deny, Cancel: all of it goes (its trial, its setup box, its folder, a new provider's home; not a repaired
        one's); its conversation stays."""
        with self.lock:
            creation = self.get(user_id, creation_id)
            if creation["status"] == "applied":
                raise RepairError("It's in Relay: take it out again instead" if creation.get("creates")
                                  else "It's applied: undo it instead")
            self._stop_trial(creation)
            if self.relay.boxes is not None:
                self.relay.boxes.remove_setup(_key(creation))
            for folder in ((creation.get("home") if creation.get("creates") else None), creation["folder"]):
                if folder:
                    _remove(Path(folder))
            self.relay.db.execute("UPDATE repairs SET status = 'discarded' WHERE id = ?", (creation_id,))
            return self.get(user_id, creation_id)

    # 3. Finalisation

    def run_ended(self, conversation_id: str) -> None:
        """Its agent's turn is over (relay.runs): built, Relay checks it, and starts a trial Relay with it."""
        found = self.relay.repairs.for_conversation(conversation_id)
        if not found or not found.get("page") or found["status"] != "building":
            return
        if self.relay.runs.active_run_for(conversation_id):
            return  # (another agent goes on: a fallback's)
        if found.get("creates") and not _layer(Path(found["folder"]), found["target_id"]):
            return  # (nothing built: its answer says why)
        self.checking.add(found["id"])
        threading.Thread(target=self._checked, args=(found["user_id"], found["id"], False), daemon=True,
                         name=f"relay-creation-{found['id']}").start()

    def check(self, user_id: int, creation_id: str, anyway: bool = False) -> None:
        """Check it again (and start its trial when it passes; `anyway`: also when it doesn't)."""
        creation = self.get(user_id, creation_id)
        if creation["status"] not in ("building", "trying"):
            raise RepairError(f"This {_what(creation)} is {creation['status']}")
        if creation_id in self.checking:
            raise RepairError("Relay is checking it already")
        self.checking.add(creation_id)
        threading.Thread(target=self._checked, args=(user_id, creation_id, anyway), daemon=True,
                         name=f"relay-creation-{creation_id}").start()

    def _checked(self, user_id: int, creation_id: str, anyway: bool) -> None:
        try:
            with self.lock:
                creation = self.get(user_id, creation_id)
                self._stop_trial(creation)
                reviewed = self.relay.repairs.review(user_id, creation_id, status="building", full=True)
                if reviewed["review"] and not reviewed["review"]["compile_errors"] and \
                        ((reviewed["review"]["test"] or {}).get("ok") or anyway):
                    self._start_trial(reviewed)
                    self.relay.db.execute("UPDATE repairs SET status = 'trying' WHERE id = ?", (creation_id,))
        except Exception as exc:  # noqa: BLE001 - said on its page, as its check's
            print(f"relay: provider page {creation_id}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            self.relay.db.execute("UPDATE repairs SET review = json_set(coalesce(review, '{}'), '$.trouble', ?) "
                                  "WHERE id = ?", (f"{exc}", creation_id))
        finally:
            self.checking.discard(creation_id)

    def fix(self, user_id: int, creation_id: str, wanted: str = "") -> str:
        """Something's wrong (in the trial, or its check failed): to its agent, the trial gone meanwhile. What it's
        asked."""
        with self.lock:
            creation = self.get(user_id, creation_id)
            if creation["status"] not in ("building", "trying"):
                raise RepairError(f"This {_what(creation)} is {creation['status']}")
            self._stop_trial(creation)
            self.relay.db.execute("UPDATE repairs SET status = 'building' WHERE id = ?", (creation_id,))
        where = (f"relay/providers/{creation['target_id']}/" if creation.get("creates")
                 else "the files REPAIR.md names")
        wanted = str(wanted or "").strip()
        if wanted:
            return (f"The user tried it in a trial Relay and says:\n\n{wanted[:4000]}\n\nPlease fix that in "
                    f"{where}, test with repair_check until it passes, and say briefly what you changed. Relay checks "
                    "it again and gives the user a new trial.")
        failed = [f"- {r['name']}: {r.get('detail') or 'failed'}"
                  for r in ((creation.get("review") or {}).get("test") or {}).get("results") or [] if r["ok"] is False]
        trouble = (creation.get("review") or {}).get("trouble") or ((creation.get("review") or {}).get("test") or {}
                                                                     ).get("error")
        return ("Relay's check of your layer failed:\n\n" + ("\n".join(failed) or str(trouble or "(see repair_check)"))
                + "\n\nPlease fix it, test with repair_check until it passes, and say briefly what you changed.")

    def add(self, user_id: int, creation_id: str) -> dict[str, Any]:
        """Add: the trial goes; its folder goes into Relay and its template (with the home) among the providers.
        Relay loads it when it restarts (the caller's)."""
        with self.lock:
            creation = self.get(user_id, creation_id)
            if not creation.get("creates"):
                raise RepairError("A repair is applied, not added")
            if creation["status"] != "trying":
                raise RepairError("Try it first: Relay adds it once its trial is up")
            template = (creation["review"] or {}).get("template")
            if not template:
                raise RepairError("Its check said nothing of its template: check it again")
            self._stop_trial(creation)
            self.relay.repairs.apply(user_id, creation_id, anyway=True)  # (the user tried it: its test is theirs)
            directory = self.relay.registry.directory
            taken = {path.stem for path in directory.glob("*.json")} if directory.is_dir() else set()
            wanted, n = template["id"], 2
            while template["id"] in taken:
                template["id"], n = f"{wanted}-{n}", n + 1
            template = {**template, "enabled": True, "home": creation["home"]}
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{template['id']}.json").write_text(json.dumps(template, indent=2) + "\n")
            self.relay.db.execute("UPDATE repairs SET review = json_set(review, '$.added', ?) WHERE id = ?",
                                  (template["id"], creation_id))
            if self.relay.boxes is not None:
                self.relay.boxes.remove_setup(_key(creation))
            return self.get(user_id, creation_id)

    def apply(self, user_id: int, repair_id: str) -> dict[str, Any]:
        """A repair's Apply: the trial goes, the repaired files go into Relay (the originals kept, for Undo). Relay
        uses them when it restarts (the caller's)."""
        with self.lock:
            repair = self.get(user_id, repair_id)
            if repair.get("creates"):
                raise RepairError("A new provider is added, not applied")
            if repair["status"] != "trying":
                raise RepairError("Try it first: Relay applies it once its trial is up")
            self._stop_trial(repair)
            self.relay.repairs.apply(user_id, repair_id, anyway=True)  # (the user tried it: its test is theirs)
            if self.relay.boxes is not None:
                self.relay.boxes.remove_setup(_key(repair))
            return self.get(user_id, repair_id)

    def undo(self, user_id: int, creation_id: str) -> dict[str, Any]:
        """Take it out again: a new provider's template, folder and home go (Relay forgets the kind when it
        restarts); a repair's originals come back."""
        with self.lock:
            creation = self.get(user_id, creation_id)
            if creation["status"] != "applied":
                raise RepairError("Only what was added or applied can be undone")
            self.relay.repairs.undo(user_id, creation_id)
            if not creation.get("creates"):
                return self.get(user_id, creation_id)
            added = (creation["review"] or {}).get("added")
            if added:
                (self.relay.registry.directory / f"{added}.json").unlink(missing_ok=True)
            if creation.get("home"):
                _remove(Path(creation["home"]))
            return self.get(user_id, creation_id)

    # The trial Relay: a Relay of its own (its code with the new layer, its data, its boxes), on 127.0.0.1

    def trial(self, creation_id: str) -> dict[str, Any] | None:
        """Where relay.forward finds its trial Relay: {user, port, token}, while it's up."""
        found = self.relay.db.one("SELECT user_id, status, trial FROM repairs WHERE id = ? AND page = 1",
                                  (creation_id,))
        if not found or found["status"] != "trying" or not found["trial"]:
            return None
        trial = json.loads(found["trial"])
        if not _alive(trial.get("pid")):
            return None
        return {"user": found["user_id"], "port": trial["port"], "token": trial["token"]}

    def ensure_trial(self, user_id: int, creation_id: str) -> dict[str, Any]:
        """Its trial Relay, up (again: Relay restarted, or the machine)."""
        with self.lock:
            creation = self.get(user_id, creation_id)
            if creation["status"] != "trying":
                raise RepairError("Its trial isn't up: it's tried once Relay's check of it passed")
            if self.trial(creation_id) is None:
                self._start_trial(creation)
            return self.view(user_id, creation_id)

    def _template(self, creation: dict[str, Any]) -> dict[str, Any]:
        """The provider a trial Relay has: a new one's (its EXAMPLE, as its check had it, with its home), or the
        repaired one's own (its file as it is: its keys, its home)."""
        if creation.get("creates"):
            template = (creation["review"] or {}).get("template")
            if not template:
                raise RepairError("Its check said nothing of its template")
            return {**template, "enabled": True, "home": creation["home"]}
        try:
            return {**json.loads((self.relay.registry.directory / f"{creation['target_id']}.json").read_text()),
                    "enabled": True}
        except (OSError, ValueError) as exc:
            raise RepairError(f"The provider {creation['target_id']} isn't there any more") from exc

    def _start_trial(self, creation: dict[str, Any]) -> None:
        review = creation["review"] or {}
        template = self._template(creation)
        self._stop_process(creation)
        where = trial_dir(creation["id"])
        code, data = where / "code", where / "data"
        shutil.rmtree(code, ignore_errors=True)
        for package in PACKAGES:
            shutil.copytree(SOURCE / package, code / package, ignore=shutil.ignore_patterns("__pycache__"))
        snapshot = _reviewed(creation["id"], review["snapshot"])
        for relative in review["changed"]:
            (code / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snapshot / relative, code / relative)
        port = _free_port()
        base = self.relay.settings.base_path
        settings = {**config.read_json(data / "settings.json"), "host": "127.0.0.1", "port": port,
                    "workspace_roots": [str(where / "projects")], "base_path": f"{base}/trial/{creation['id']}",
                    "trial": {"name": template.get("name") or creation["target_id"],
                              "back": f"{base}{_page(creation)}"}}
        (data / "providers").mkdir(parents=True, exist_ok=True)
        (where / "projects").mkdir(exist_ok=True)
        config.write_json(data / "settings.json", settings)
        for old in (data / "providers").glob("*.json"):
            old.unlink()
        (data / "providers" / f"{template['id']}.json").write_text(json.dumps(template, indent=2))
        env = {**os.environ, "RELAY_HOME": str(data), "XDG_CACHE_HOME": str(where / "cache"),
               "PYTHONPATH": os.pathsep.join([str(code), str(SOURCE.parent / "vendor")]),
               "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("INVOCATION_ID", None)
        process = self.launch(where, env)
        self.trials[creation["id"]] = process
        old = json.loads(creation["trial"]) if creation.get("trial") else {}
        password = old.get("password") or secrets.token_urlsafe(24)
        try:
            _wait(process, port, where / "relay.log")
            user = self.relay.db.one("SELECT username FROM users WHERE id = ?", (creation["user_id"],))
            token = _sign_in(port, (user or {}).get("username") or "me", password)
        except Exception:
            _end(process.pid)
            raise
        self.relay.db.execute("UPDATE repairs SET trial = ? WHERE id = ?",
                              (json.dumps({"port": port, "pid": process.pid, "password": password, "token": token}),
                               creation["id"]))

    def _stop_process(self, creation: dict[str, Any]) -> None:
        process = self.trials.pop(creation["id"], None)
        pid = process.pid if process is not None else (json.loads(creation["trial"]).get("pid")
                                                         if creation.get("trial") else None)
        if pid:
            _end(pid)
        if process is not None:
            process.wait(timeout=30)

    def _stop_trial(self, creation: dict[str, Any]) -> None:
        """Its trial Relay ended, and all of it gone: its boxes (by its mark) and its folder."""
        self._stop_process(creation)
        where = trial_dir(creation["id"])
        mark = hashlib.sha256(str((where / "data").resolve()).encode()).hexdigest()[:8]
        listed = subprocess.run(["podman", "ps", "-a", "--filter", f"label=io.relay.instance={mark}", "--format",
                                 "{{.Names}}"], capture_output=True, text=True, timeout=60) \
            if self.relay.boxes is not None else None  # (a Relay without boxes started its trial without any)
        for name in listed.stdout.split() if listed is not None and listed.returncode == 0 else []:
            subprocess.run(["podman", "rm", "-f", "-t", "5", name], capture_output=True, timeout=120)
            if not name.startswith("relay-box-"):  # (a WorkBox's home and image store)
                subprocess.run(["podman", "volume", "rm", "-f", *(f"relay-{kind}-{name.removeprefix('relay-')}"
                                                                  for kind in ("home", "containers"))],
                               capture_output=True, timeout=120)
        _remove(where)
        self.relay.db.execute("UPDATE repairs SET trial = NULL WHERE id = ?", (creation["id"],))

    def stop(self) -> None:
        """Relay stopping: its trial Relays go with it (they start again when their page is opened)."""
        for creation_id, process in list(self.trials.items()):
            _end(process.pid)
            self.trials.pop(creation_id, None)


def _key(creation: dict[str, Any]) -> str:
    """Its setup box's key (Boxes.setup): a new provider's, or a repaired one's (with that provider's home)."""
    return f"{'new' if creation.get('creates') else 'repair'}-{creation['id']}"


def _page(creation: dict[str, Any]) -> str:
    """Its page's path in Relay."""
    return f"/providers/{'new' if creation.get('creates') else 'repair'}/{creation['id']}"


def _what(creation: dict[str, Any]) -> str:
    return "new provider" if creation.get("creates") else "repair"


def _commit(folder: Path, message: str) -> None:
    if not (folder / ".git").exists():
        _git(folder, "init", "-q")
    _git(folder, "add", "-A")
    _git(folder, "-c", "user.name=Relay", "-c", "user.email=relay@localhost", "-c", "commit.gpgsign=false",
         "commit", "-q", "--no-verify", "--allow-empty", "-m", message)


def _seed(folder: Path, name: str) -> None:
    """The guide's example, as relay/providers/<name>/ (its imports of itself that folder's), to make the program's."""
    layer = folder / "relay" / "providers" / name
    if layer.exists():
        return
    shutil.copytree(SOURCE / "relay" / "providers" / "example", layer, ignore=shutil.ignore_patterns("__pycache__"))
    for path in layer.rglob("*.py"):
        path.write_text(path.read_text().replace("relay.providers.example", f"relay.providers.{name}"))


def _remove(path: Path) -> None:
    """A folder of Relay's, gone; what a box wrote in it as another user of the box's (podman unshare) too."""
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        subprocess.run(["podman", "unshare", "rm", "-rf", str(path)], capture_output=True, timeout=120)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:  # (a zombie of ours is no Relay)
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def _end(pid: int) -> None:
    """A trial Relay (and what it started in its session) ended: as asked, then for sure."""
    for sig, wait in ((signal.SIGTERM, 20), (signal.SIGKILL, 5)):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not _alive(pid):
                return
            time.sleep(0.2)


def _launch(where: Path, env: dict[str, str]) -> subprocess.Popen:
    """A trial Relay started: `relay serve` with its code and data (env), its output in its folder's log."""
    with open(where / "relay.log", "ab") as log:
        return subprocess.Popen([sys.executable, "-m", "relay", "serve"], cwd=where, env=env, stdout=log,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)


def _wait(process: subprocess.Popen, port: int, log: Path) -> None:
    deadline = time.monotonic() + TRIAL_START
    while time.monotonic() < deadline:
        if process.poll() is not None:
            said = log.read_text(errors="replace")[-1500:] if log.exists() else ""
            raise RepairError(f"The trial Relay ended at its start: {said.strip() or process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise RepairError(f"The trial Relay didn't answer within {TRIAL_START} s")


def _sign_in(port: int, username: str, password: str) -> str:
    """Its account (the user's name; the password Relay keeps) made or signed in to: its session's token."""
    from relay.web import COOKIE
    for path in ("/api/setup", "/api/login"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", method="POST",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json", "X-Relay-Request": "1"})
        try:
            with urllib.request.urlopen(request, timeout=30) as answer:
                for cookie in answer.headers.get_all("Set-Cookie") or []:
                    name, _, rest = cookie.partition("=")
                    if name.strip() == COOKIE:
                        return rest.split(";", 1)[0]
        except urllib.error.HTTPError:
            continue  # (set up already: sign in)
    raise RepairError("Couldn't sign in to the trial Relay")


def _brief(about: str, home: str) -> str:
    lines = ["# A provider Relay doesn't know yet", "",
             "The user installed a program in a box of its own and logged in there, and wants it in Relay as a",
             "provider. Study it and hand in a plan for its support layer first; once the user approves it, build it.",
             "", "## What the user installed", ""]
    lines += [f"> {line}" if line else ">" for line in about.splitlines()]
    lines += ["", "## Where it is", "",
              f"In its box, its home is `{home}`: its HOME, with the program and its login. npm's global installs go",
              "to `.local/` there, and `.local/bin` is first on PATH. Relay keeps that home: the provider you build",
              "gets it as its template's `home` (Provider.box_home), mounted as its box's HOME just like this. So its",
              "layer mounts nothing for the program, and finds it with `home_program(name)`.", "",
              "Your own commands run in a container of this folder's, without the program. Relay's tools do what needs",
              "it (MCP tools, or `relay repair …` in a shell):", "",
              "- `repair_run` (`relay repair run -- <command>`): a command in that box, as the user left it: the",
              "  program's --version, its --help, how it's logged in, a tiny prompt to see what it prints. Keep",
              "  prompts few and short: they may cost the user.",
              "- `provider_plan` (`relay repair plan NAME PROGRAM`, the plan on stdin): hand in your plan.",
              "- `repair_check` (`relay repair check`): once you build it, your folder tested in a copy of Relay, in",
              "  a box of its own with the home: Relay's whole check of it (its check(), a turn, going on, stopping,",
              "  a command), with its EXAMPLE as the template.", "",
              "## First: your plan", "",
              "`reference/docs/ADDING-A-PROVIDER.md` is the guide: read it, all of it. It builds a layer step by step",
              "along a worked example (toy-agent), and has a reference of every event, hook and field.",
              "`reference/relay/` is all of Relay's source as it runs now (read-only).", "",
              "Find out what the guide asks in 4.1: how the program starts, what it prints, sessions, models,",
              "stopping, permissions. Maybe a kind Relay has fits already (section 1 of the guide): say so in your",
              "plan. Then hand it in with `provider_plan`:", "",
              "- `name`: its folder's, a Python name (gemini, my_agent): relay/providers/<name>/, not one Relay has.",
              "- `program`: how it's started in its box, e.g. `gemini`.",
              "- `plan`: for the user, short and plain: what it is, how Relay will run it (the command, what it",
              "  prints), sessions, models, permissions, and what you couldn't find out.", "",
              "Then stop: the user approves it, asks for another, or cancels.", "",
              "## Then: build it (once approved)", "",
              "Relay copies the guide's example to `relay/providers/<name>/`; "
              "make it the program's, as the guide does:",
              "",
              "- Its kind (KIND) is lowercase letters, digits and -, and not one Relay has; LABEL and EXAMPLE are its.",
              "- Remove what's the example's: toy_agent.py, `LISTED = False`, its README and tests; write the",
              "  program's own (its README: what you found; tests/test_<name>.py).",
              "- Its box: box_key() (its template's id) and no box_mounts() for the program: its home has it.",
              "  check() asks home_program(…); start() runs it by its name, with context.box.wrap(…).",
              "- Its EXAMPLE is the template it's tested and added with (Relay adds the home).",
              "- Test it with `repair_check` until it passes, then say briefly what you made.", "",
              "Relay then checks it once more and starts a trial Relay with it, for the user to try. If they find",
              "something wrong, they tell you here.", "",
              "## What you may change", "",
              "Only `relay/providers/<name>/` in this folder. Relay takes nothing else from here, and Relay itself",
              "isn't yours to change: if the layer truly needs something Relay doesn't offer, say what and why."]
    return "\n".join(lines) + "\n"
