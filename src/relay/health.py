"""Which models work: the last result of each provider and model.

Every run leaves its mark (it worked, or failed with this error: a usage
limit, a login that expired, a model that's gone), and a test sends a tiny
prompt to find out now. The model picker and Settings → Providers show it.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from relay import limits, tunables
from relay.db import Database
from relay.providers import Context, Provider

MAX_MESSAGE = 300


def record(db: Database, provider_id: str, model: str | None, ok: bool, message: str = "",
           seconds: float | None = None, source: str = "run", resets_at: float | None = None,
           usage: dict[str, Any] | None = None) -> None:
    db.execute(
        "INSERT INTO model_status (provider_id, model, ok, message, seconds, checked, source, resets_at, usage) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (provider_id, model) DO UPDATE SET ok = excluded.ok, "
        "message = excluded.message, seconds = excluded.seconds, checked = excluded.checked, "
        "source = excluded.source, resets_at = excluded.resets_at, "
        "usage = COALESCE(excluded.usage, model_status.usage)",
        (provider_id, model or "", 1 if ok else 0, _short(message), seconds, time.time(), source, resets_at,
         json.dumps(usage) if usage else None))


def statuses(db: Database, provider_id: str | None = None) -> dict[str, dict[str, Any]]:
    """{"provider/model": {provider_id, model, ok, message, seconds, checked, source}}; model "" is the default."""
    sql, params = "SELECT * FROM model_status", ()
    if provider_id:
        sql, params = sql + " WHERE provider_id = ?", (provider_id,)
    return {f"{r['provider_id']}/{r['model']}": {**r, "ok": bool(r["ok"]),
                                                  "usage": json.loads(r["usage"]) if r.get("usage") else None}
            for r in db.all(sql, params)}


def after_run(db: Database, provider_id: str, model: str | None, status: str, events: list[dict[str, Any]],
              seconds: float) -> None:
    """What a finished run says about its model; a stopped one says nothing."""
    errors = [e.get("message", "") for e in events if e.get("type") == "error"]
    latest = limits.latest(events)
    usage = {k: latest.get(k) for k in ("status", "resets_at", "window", "windows")} if latest else None
    if status == "succeeded":
        record(db, provider_id, model, True, "", seconds, usage=usage)
    elif status == "failed":
        limited = limits.reached(events)
        record(db, provider_id, model, False,
               (limited or {}).get("message") or (errors[-1] if errors else "The run failed"),
               seconds, resets_at=(limited or {}).get("resets_at"), usage=usage)


def test(provider: Provider, model: str | None, scratch: Path,
         place: Callable[[Provider, Context], Any] | None = None,
         discard: Callable[[Path], Any] | None = None) -> dict[str, Any]:
    """Send the model a tiny prompt; {"ok", "message", "seconds"}. `place` puts it where a run would be
    (RunManager.place_check: the provider's box with its login, the WorkBox for its commands), and may return what
    to call when it's over; `discard` takes the scratch folder's WorkBox away after (as conformance.check)."""
    folder = scratch / f"test-{uuid.uuid4().hex[:12]}"
    folder.mkdir(parents=True, exist_ok=True)
    context = Context(conversation_id=folder.name, directory=folder, mode="chat", model=model or None,
                      provider_session=None)
    started = time.monotonic()
    limit = tunables.get("tests.timeout")  # seconds; agents take a while to start
    text, errors, status = [], [], "failed"
    run = None
    events = None
    timer = None
    after = None
    try:
        after = place(provider, context) if place is not None else None
        run = provider.start(context, tunables.get("tests.prompt"))
        timer = threading.Timer(limit, run.interrupt)
        timer.start()
        events = run.events()
        for event in events:
            kind = event.get("type")
            if kind == "text":
                text.append(str(event.get("delta") or ""))
            elif kind == "error":
                errors.append(str(event.get("message") or "error"))
            elif kind == "done":
                status = event.get("status", "failed")
    except Exception as exc:  # noqa: BLE001 - whatever went wrong is the answer
        errors.append(str(exc) or type(exc).__name__)
    finally:
        if timer is not None:
            timer.cancel()
        # (as a run ends: the provider's own cleanup, what it started in its box, and its folder's WorkBox)
        for end in (getattr(events, "close", None), getattr(context.box, "end", None),
                    after if callable(after) else None, (lambda: discard(folder)) if discard is not None else None):
            try:
                if end is not None:
                    end()
            except Exception:  # noqa: BLE001 - never lose the answer over it (an idle WorkBox goes by itself)
                pass
        shutil.rmtree(folder, ignore_errors=True)
    seconds = round(time.monotonic() - started, 1)
    answer = " ".join("".join(text).split())
    if seconds >= limit and status != "succeeded":
        errors.append(f"No answer within {limit} seconds")
    ok = status == "succeeded" and not errors and bool(answer)
    if ok:
        message = f"Answered in {seconds:g} s: “{answer[:60]}”"
    elif errors:
        message = errors[-1]
    else:
        message = "It gave no answer" if status == "succeeded" else f"The run {status}"
    return {"ok": ok, "message": _short(message), "seconds": seconds}


def _short(message: str) -> str:
    message = " ".join((message or "").split())
    return message if len(message) <= MAX_MESSAGE else message[:MAX_MESSAGE - 1] + "…"
