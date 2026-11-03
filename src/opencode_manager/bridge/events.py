"""The uniform event format every backend emits.

Each event is a JSON-serialisable dict with a "type":

    session      {"backend", "session", "profile", "directory", "new"}
    step         {"agent", "model"}                  a model turn starts
    text         {"delta"}                           answer text
    reasoning    {"delta"}                           thinking text
    tool_call    {"id", "name", "input"}
    tool_result  {"id", "name", "ok", "output"}
    permission   {"id", "action", "resources", "decision"}
                 decision: "once" when auto-approved, "denied" when refused,
                 null when left to someone else
    form         {"id", "title", "fields", "status", "answer", "message", "expires"}
                 status: "pending" | "answered" | "cancelled";
                 fields have a key, type, title and optional choices/constraints
    usage        {"tokens", "cost", "billing"}
    limit        {"status": "allowed"|"warning"|"rejected", "resets_at", "window", "windows"}:
                 the provider's usage limit (e.g. a plan's five-hour window)
                 billing: "api" | "subscription" | "free" | null, see billing.py
    title        {"title"}                           a title for the conversation
    turn         {"id"}                              the backend's id for this turn's
                                                     message (opencode), to fork before it
    error        {"message"}
    done         {"status"}  "succeeded" | "failed" | "interrupted"

"done" is always the last event of a run.
"""

from __future__ import annotations

from typing import Any

Event = dict[str, Any]

DONE_STATUSES = ("succeeded", "failed", "interrupted")


def event(type_: str, **fields: Any) -> Event:
    return {"type": type_, **fields}
