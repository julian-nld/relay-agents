import threading

import pytest

from relay.db import Database
from relay.runs import ActiveRun, RunManager


@pytest.fixture
def db():
    db = Database(":memory:")
    db.execute("INSERT INTO users (id, username, password_hash, created) VALUES (1, 'me', '!', 0)")
    db.execute("INSERT INTO conversations (id, user_id, title, provider_id, mode, directory, created, updated) "
               "VALUES ('c', 1, 'Test', 'fake', 'chat', '/virtual', 0, 0)")
    db.execute("INSERT INTO runs (id, conversation_id, provider_id, status, created, session) "
               "VALUES ('r', 'c', 'fake', 'running', 0, 'known-session')")
    yield db
    db.close()


def test_flush_appends_without_duplicates_or_renumbering(db):
    run = ActiveRun("r", "c")
    run.add({"type": "text", "delta": "First"})
    run.flush(db)
    run.flush(db)
    run.add({"type": "text", "delta": " second"})
    run.flush(db)
    assert db.run_events("r") == run.events
    assert [r["seq"] for r in db.all("SELECT seq FROM run_events ORDER BY seq")] == [0, 1]


def test_reasoning_duration_is_measured_once_and_survives_storage(db, monkeypatch):
    now = [10.0]
    monkeypatch.setattr("relay.runs.time.monotonic", lambda: now[0])
    run = ActiveRun("r", "c")
    first = {"type": "reasoning", "delta": "Considering"}
    run.add(first)
    assert run.events[-1]["reasoning_ms"] == 0
    assert "reasoning_ms" not in first  # provider events are not changed in place
    now[0] = 11.2
    run.add({"type": "reasoning", "delta": " the options"})
    assert run.events[-1]["reasoning_ms"] == 1200
    now[0] = 12.3
    run.add({"type": "text", "delta": "The answer"})
    assert run.events[-1]["reasoning_ms"] == 2300
    now[0] = 30.0
    run.add({"type": "tool_call", "id": "tool", "name": "shell"})
    assert "reasoning_ms" not in run.events[-1]  # tool work is not thinking time
    run.add({"type": "reasoning", "delta": "A separate thought"})
    assert run.events[-1]["reasoning_ms"] == 0
    now[0] = 98.0
    run.add({"type": "done", "status": "interrupted"})
    assert run.events[-1]["reasoning_ms"] == 68000
    run.flush(db)
    assert db.run_events("r") == run.events


@pytest.mark.parametrize("boundary", ["tool_call", "subagent", "permission", "form", "error", "done"])
def test_reasoning_ends_at_the_next_visible_phase(boundary, monkeypatch):
    now = [1.0]
    monkeypatch.setattr("relay.runs.time.monotonic", lambda: now[0])
    run = ActiveRun("r", "c")
    run.add({"type": "reasoning", "delta": ""})
    assert run.reasoning_started is None
    run.add({"type": "reasoning", "delta": "Thinking"})
    now[0] = 3.0
    run.add({"type": "usage", "tokens": {}})
    run.add({"type": "text", "delta": ""})
    assert run.reasoning_started == 1.0
    now[0] = 4.0
    run.add({"type": boundary})
    assert run.events[-1]["reasoning_ms"] == 3000
    assert run.reasoning_started is None


def test_restart_materializes_partial_answer_and_is_idempotent(db):
    events = [{"type": "text", "delta": "Partial answer"},
              {"type": "tool_call", "id": "t", "name": "shell", "input": {"command": "ls"}}]
    db.append_run_events("r", 0, events)
    assert db.fail_unfinished_runs() == [
        {"id": "r", "provider_id": "fake", "session": "known-session", "directory": "/virtual"}]
    assert db.one("SELECT content FROM messages WHERE run_id = 'r'")["content"] == "Partial answer"
    assert db.one("SELECT status FROM runs WHERE id = 'r'")["status"] == "failed"
    stored = db.run_events("r")
    assert stored[:2] == events
    assert stored[-1] == {"type": "done", "status": "failed"}
    assert db.fail_unfinished_runs() == []
    assert db.run_events("r") == stored
    assert len(db.all("SELECT * FROM messages WHERE run_id = 'r'")) == 1


def test_flush_of_deleted_run_is_harmless(db):
    run = ActiveRun("r", "c")
    run.add({"type": "text", "delta": "Late"})
    db.execute("DELETE FROM runs WHERE id = 'r'")
    run.flush(db)
    assert db.run_events("r") == []


def test_restart_closes_pending_questions_but_keeps_answered_ones(db):
    fields = [{"key": "use", "type": "string", "title": "Main use?"}]
    pending = {"type": "form", "id": "frm_1", "title": "Questions", "fields": fields, "status": "pending"}
    answered = {**pending, "id": "frm_2", "status": "answered", "answer": {"use": "Gaming"}}
    db.append_run_events("r", 0, [pending, answered])
    db.fail_unfinished_runs()
    forms = [e for e in db.run_events("r") if e["type"] == "form"]
    assert forms[:2] == [pending, answered]
    assert forms[2] == {**pending, "status": "cancelled", "message": "Relay restarted during this run"}


def test_question_reply_failure_keeps_the_answers_retryable(db):
    from relay.runs import RunError
    manager = RunManager(db, None)
    run = ActiveRun("r", "c")
    manager.active[run.id] = run
    attempts = []
    def reply(answer):
        attempts.append(answer)
        if len(attempts) == 1:
            raise RunError("Temporary failure")
    event = {"type": "form", "id": "frm_1", "fields": [], "status": "pending"}
    manager.ask(run, event, reply)
    pending = run.events[0]
    assert pending["expires"] > 0
    with pytest.raises(RunError, match="Temporary failure"):
        manager.answer_form(run.id, "frm_1", {"use": "Gaming"})
    assert "frm_1" in run.pending
    assert run.events == [pending]
    with pytest.raises(RunError):
        manager.answer(run.id, "frm_1", "allow")
    manager.answer_form(run.id, "frm_1", {"use": "Gaming"})
    assert not run.pending
    assert run.events[-1]["answer"] == {"use": "Gaming"}
    with pytest.raises(RunError, match="answered already"):
        manager.answer_form(run.id, "frm_1", {})


def test_relay_subagent_questions_are_shown_in_the_parent_run(db):
    manager = RunManager(db, None)
    parent = ActiveRun("parent", "c")
    child = ActiveRun("child", "child-c")
    child.parent_run, child.label = parent.id, "Researcher"
    manager.active.update({parent.id: parent, child.id: child})
    answers = []
    form = {"type": "form", "id": "frm_1", "title": "Use?", "fields": [], "status": "pending"}
    manager.ask(child, form, answers.append)
    assert parent.events[-1] == {**child.events[-1], "run": child.id, "subagent": "Researcher"}
    manager.answer_form(child.id, "frm_1", None)
    assert answers == [None]
    assert parent.events[-1]["status"] == "cancelled"
    assert not child.pending


@pytest.fixture
def form_timers(monkeypatch):
    timers = []
    class Timer:
        def __init__(self, seconds, callback, args):
            self.seconds, self.callback, self.args = seconds, callback, args
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def fire(self):
            self.callback(*self.args)

    monkeypatch.setattr("relay.runs.threading.Timer", Timer)
    return timers


def question(manager, reply):
    run = ActiveRun("r", "c")
    manager.active[run.id] = run
    manager.ask(run, {"type": "form", "id": "frm_1", "fields": [], "status": "pending"}, reply)
    return run


def test_unanswered_question_is_dismissed_without_a_browser(db, form_timers):
    manager, answers = RunManager(db, None), []
    run = question(manager, answers.append)
    assert form_timers[0].seconds == 300 and form_timers[0].daemon
    pending = run.events[0]
    assert pending["expires"] > 0
    form_timers[0].fire()
    assert answers == [None]
    assert not run.pending
    assert run.events[-1] == {**pending, "status": "cancelled",
                              "message": "Question dismissed automatically: no answer before the timeout"}
    assert form_timers[0].cancelled
    form_timers[0].fire()
    assert answers == [None]


def test_question_timeout_can_be_disabled(db, form_timers, monkeypatch):
    from relay import tunables
    monkeypatch.setitem(tunables._values, "questions.dismiss_timeout", 0)
    manager, answers = RunManager(db, None), []
    run = question(manager, answers.append)
    assert not form_timers and "expires" not in run.events[0]
    assert not answers and run.pending
    manager.answer_form(run.id, "frm_1", {})


@pytest.mark.parametrize("answer", [{"use": "Gaming"}, None, "stop"])
def test_answer_dismiss_and_stop_cancel_the_question_timer(db, form_timers, answer):
    manager, answers = RunManager(db, None), []
    run = question(manager, answers.append)
    if answer == "stop":
        manager.stop(run.id)
    else:
        manager.answer_form(run.id, "frm_1", answer)
    assert form_timers[0].cancelled
    form_timers[0].fire()  # even a timer callback already queued must not dismiss again
    assert answers == ([] if answer == "stop" else [answer])


def test_repeated_question_does_not_extend_its_timeout(db, form_timers, monkeypatch):
    from relay import tunables
    monkeypatch.setitem(tunables._values, "questions.dismiss_timeout", 10)
    manager = RunManager(db, None)
    run = question(manager, lambda _: None)
    entry = run.pending["frm_1"]
    manager.ask(run, {"type": "form", "id": "frm_1", "fields": [], "status": "pending"}, lambda _: None)
    assert len(form_timers) == 1 and form_timers[0].seconds == 10
    assert run.pending["frm_1"] is entry and run.events[0] == run.events[1]
    form_timers[0].fire()


def test_failed_automatic_dismissal_retries_without_closing_the_question(db, form_timers):
    manager, answers = RunManager(db, None), []
    def reply(answer):
        answers.append(answer)
        if len(answers) == 1:
            raise OSError("Provider unavailable")
    run = question(manager, reply)
    expires = run.events[0]["expires"]
    form_timers[0].fire()
    assert "frm_1" in run.pending and len(run.events) == 1
    assert len(form_timers) == 2 and form_timers[1].seconds == 30
    form_timers[1].fire()
    assert answers == [None, None] and not run.pending
    assert run.events[-1]["status"] == "cancelled" and run.events[-1]["expires"] == expires


def test_answer_in_flight_wins_over_the_question_timeout(db, form_timers):
    manager, answers = RunManager(db, None), []
    responding, release = threading.Event(), threading.Event()
    def reply(answer):
        answers.append(answer)
        responding.set()
        assert release.wait(3)
    run = question(manager, reply)
    user = threading.Thread(target=manager.answer_form, args=(run.id, "frm_1", {"use": "Gaming"}))
    timeout = threading.Thread(target=form_timers[0].fire)
    user.start()
    try:
        assert responding.wait(2)
        timeout.start()
    finally:
        release.set()
        user.join(3)
        if timeout.ident is not None:
            timeout.join(3)
    assert not user.is_alive() and not timeout.is_alive()
    assert answers == [{"use": "Gaming"}]
    assert run.events[-1]["status"] == "answered" and not run.pending


def test_periodic_flusher_persists_even_when_the_provider_falls_silent(db, monkeypatch):
    monkeypatch.setattr("relay.runs.EVENT_FLUSH_SECONDS", 0.01)
    manager = RunManager(db, None)
    run = ActiveRun("r", "c")
    run.add({"type": "text", "delta": "Before the long tool call"})
    manager.active[run.id] = run
    persisted = threading.Event()
    original = db.append_run_events
    def append(*args):
        original(*args)
        persisted.set()
    monkeypatch.setattr(db, "append_run_events", append)
    worker = threading.Thread(target=manager._flush_events)
    worker.start()
    try:
        assert persisted.wait(2)
        assert db.run_events("r") == run.events
    finally:
        with manager.lock:
            manager.active.clear()
        worker.join(2)
    assert not worker.is_alive()


def test_project_operation_blocks_new_writers_and_overlapping_operations(db, tmp_path):
    from relay.runs import ProjectBusy
    manager = RunManager(db, None)
    project = {"id": "c", "directory": str(tmp_path / "project")}
    nested = {"id": "other", "directory": str(tmp_path / "project/subfolder")}
    def restore(row):
        with pytest.raises(ProjectBusy, match="being updated"):
            manager.start(nested, "write")
        with pytest.raises(ProjectBusy, match="already being updated"):
            manager.project_operation(nested, lambda r: None)
        # An unrelated folder stays usable, rather than locking all projects.
        assert manager.project_operation({"directory": str(tmp_path / "elsewhere")}, lambda r: "ok") == "ok"
        return "restored"
    assert manager.project_operation(project, restore) == "restored"
    assert manager.project_operations == {}


def test_project_operation_detects_other_conversations_and_releases_on_error(db, tmp_path):
    from relay.runs import ProjectBusy
    manager = RunManager(db, None)
    project = {"id": "c", "directory": str(tmp_path / "project")}
    run = ActiveRun("other-run", "other-chat")
    run.project = {"directory": str(tmp_path / "project/subfolder")}
    manager.active[run.id] = run
    with pytest.raises(ProjectBusy, match="Another agent"):
        manager.project_operation(project, lambda r: None)
    assert manager.project_operation(project, lambda r: "ok", allow_run_id=run.id) == "ok"
    manager.active.clear()
    def fail(row):
        raise ValueError("failed restore")
    with pytest.raises(ValueError):
        manager.project_operation(project, fail)
    assert manager.project_operations == {}


def test_failed_flush_can_retry_the_same_event_numbers(db, monkeypatch):
    run = ActiveRun("r", "c")
    run.add({"type": "text", "delta": "Keep me"})
    original = db.append_run_events
    def unavailable(*args):
        raise OSError("disk temporarily unavailable")
    monkeypatch.setattr(db, "append_run_events", unavailable)
    with pytest.raises(OSError):
        run.flush(db)
    assert run.stored_events == 0
    monkeypatch.setattr(db, "append_run_events", original)
    run.flush(db)
    assert db.run_events("r") == run.events


def test_subagents_with_copies_still_count_as_writers_of_the_parent_project(db, tmp_path):
    from relay.runs import ProjectBusy
    manager = RunManager(db, None)
    project = {"directory": str(tmp_path / "project")}
    child = ActiveRun("child", "child-chat")
    child.project = {"directory": str(tmp_path / "copies/child"), "source_directory": project["directory"]}
    manager.active[child.id] = child
    with pytest.raises(ProjectBusy):
        manager.project_operation(project, lambda r: None)


def test_restart_closes_live_cards_and_unanswered_requests(db):
    db.execute("INSERT INTO runs (id, conversation_id, status, created, usage) "
               "VALUES ('saved', 'c', 'succeeded', 0, ?)", ('{"cost": 0.1}',))
    events = [{"type": "permission", "id": "p", "action": "shell", "decision": None},
              {"type": "subagent", "id": "child", "run": "missing", "status": "running"},
              {"type": "subagent", "id": "waiting", "run": None, "status": "waiting"},
              {"type": "subagent", "id": "finished", "status": "succeeded"},
              {"type": "subagent", "id": "saved", "run": "saved", "status": "running"}]
    db.append_run_events("r", 0, events)
    db.fail_unfinished_runs()
    stored = db.run_events("r")
    assert stored[:len(events)] == events
    updates = {e["id"]: e for e in stored[len(events):] if e.get("id")}
    assert updates["p"]["decision"] == "denied"
    assert updates["p"]["outcome"] == "Relay restarted during this run"
    assert updates["child"]["status"] == "failed"
    assert updates["waiting"]["status"] == "failed"
    assert updates["saved"]["status"] == "succeeded" and updates["saved"]["usage"] == {"cost": 0.1}
    assert "finished" not in updates
    assert stored[-1] == {"type": "done", "status": "failed"}
