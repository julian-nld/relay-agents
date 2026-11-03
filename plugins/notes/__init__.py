"""Notes: Relay's example plugin (docs/WRITING-A-PLUGIN.md). Short notes in its own store: its page lists them
(static/index.js), agents add them with a tool (notes_add), a run that fails leaves one, and an hourly task lets
those older than 30 days go."""

import time
import uuid

from relay.plugins import PluginError

KEEP_DAYS = 30
LONGEST = 2000  # characters a note may have


def setup(plugin):
    store = plugin.store  # (its own: each change written whole, one at a time, whichever thread makes it)

    def add(text, by, conversation=None):
        text = (text or "").strip()
        if not text:
            raise PluginError("A note needs some text")
        if len(text) > LONGEST:
            raise PluginError(f"A note is at most {LONGEST} characters: this one has {len(text)}")
        note = {"id": uuid.uuid4().hex[:8], "text": text, "by": by, "at": time.time(), "conversation": conversation}
        store.update("notes", lambda notes: [*notes, note], [])
        return note

    # Its page's API, at /api/plugins/notes/…
    def remove(call):
        gone = []

        def without(notes):
            gone.extend(n for n in notes if n["id"] == call.params["id"])
            return [n for n in notes if n["id"] != call.params["id"]]

        store.update("notes", without, [])
        if not gone:
            raise PluginError("No such note", 404)
        return {"removed": call.params["id"]}

    def remove_all(call):
        store.set("notes", [])
        return {"removed": "all"}

    plugin.route("notes", lambda call: store.get("notes", []))
    plugin.route("notes", lambda call: add((call.json or {}).get("text"), call.user["username"]), methods=["POST"])
    plugin.route("notes", remove_all, methods=["DELETE"])
    plugin.route("notes/{id}", remove, methods=["DELETE"])

    # A tool for agents: notes_add
    def noted(call):
        note = add(call.arguments["text"], "an agent", call.conversation_id)
        return f"Noted ({note['id']}): the user sees it on their Notes page."

    plugin.tool("add", "Add a short note to the user's Notes: a reminder, or a finding worth keeping.",
                {"type": "object", "properties": {"text": {"type": "string", "description": "the note"}},
                 "required": ["text"]}, noted)

    # A run that failed leaves a note, with its chat (not the runs of chats a script or plugin started)
    def ended(run):
        if run["status"] == "failed" and not run["subagent"] and not run["started_by"]:
            why = (run["error"] or "no reason given")[:1000]  # (a long one cut short)
            add(f"A run of {run['provider_id']} failed: {why}", "Relay", run["conversation_id"])

    plugin.on_run_ended(ended)

    # Hourly: notes older than 30 days go
    def tidy():
        cutoff = time.time() - KEEP_DAYS * 86400
        store.update("notes", lambda notes: [n for n in notes if n["at"] >= cutoff], [])

    plugin.every(3600, tidy)
