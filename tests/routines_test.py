"""Preserve upstream 0.3.2 finished-worker rescheduling in the unified HQ.

Exercise real markdown parsing and persisted scheduling state in a temporary
directory; stub every worker operation. A retained tmux shell must not prevent
the next due run, while an active Claude worker must never be replaced.
"""

import json
from unittest.mock import Mock, call

import pytest

from cc_tmux_api import routines


@pytest.fixture
def scheduled(monkeypatch, tmp_path):
    monkeypatch.setattr(routines, "DIR", tmp_path)
    monkeypatch.setattr(routines.time, "time", lambda: 10000.0)
    monkeypatch.setattr(routines.workers, "PREFIX", "test-")
    (tmp_path / "hourly-check.md").write_text(
        "---\ntitle: Scheduled check\nevery: 1h\nenabled: true\n"
        "workdir: /workspace/project\n---\nReview pending tasks.\n"
    )
    state = {"hourly-check": {"last_run": 6400.0}, "another-routine": {"last_run": 5000.0}}
    (tmp_path / ".state.json").write_text(json.dumps(state))
    workers = Mock()
    workers.sessions.return_value = [
        {"name": "test-unrelated-previous"}, {"name": "test-hourly-check-previous"}
    ]
    workers.status.return_value = "working"
    workers.spawn.return_value = {"session": "test-hourly-check-new"}
    for name, stub in (("_sessions", workers.sessions), ("status_of", workers.status),
                       ("kill", workers.kill), ("spawn", workers.spawn)):
        monkeypatch.setattr(routines.workers, name, stub)
    return tmp_path, workers, state


@pytest.mark.parametrize("status", ["working", "waiting", "idle", "unknown"])
def test_due_tick_preserves_previous_worker_until_done(scheduled, status):
    directory, workers, original = scheduled
    workers.status.return_value = status

    assert routines.tick() == []

    workers.status.assert_called_once_with("test-hourly-check-previous")
    workers.kill.assert_not_called()
    workers.spawn.assert_not_called()
    assert json.loads((directory / ".state.json").read_text()) == original


def test_due_tick_clears_finished_worker_then_spawns_and_advances_schedule(scheduled):
    directory, workers, original = scheduled
    workers.status.return_value = "done"

    assert routines.tick() == ["hourly-check"]

    assert workers.mock_calls == [
        call.sessions(), call.status("test-hourly-check-previous"),
        call.kill("test-hourly-check-previous"),
        call.spawn("hourly-check", "Review pending tasks.", "/workspace/project", kind="routine"),
    ]
    saved = json.loads((directory / ".state.json").read_text())
    assert saved["hourly-check"]["last_run"] == 10000.0
    assert saved["another-routine"] == original["another-routine"]
    workers.sessions.return_value = [{"name": "test-hourly-check-new"}]
    workers.status.return_value = "working"
    listed = routines.list_routines()[0]
    assert listed["last_run"] == 10000.0
    assert listed["next_run"] == 13600.0
    assert listed["running"] == "test-hourly-check-new"
    # Re-entry uses persisted last_run, even if the previous worker finishes fast.
    workers.status.return_value = "done"
    workers.reset_mock()
    assert routines.tick() == []
    workers.kill.assert_not_called()
    workers.spawn.assert_not_called()
    assert json.loads((directory / ".state.json").read_text()) == saved


def test_not_due_tick_does_not_clear_a_finished_worker(scheduled):
    directory, workers, state = scheduled
    state["hourly-check"]["last_run"] = 9999.0
    (directory / ".state.json").write_text(json.dumps(state))
    workers.status.return_value = "done"

    assert routines.tick() == []

    workers.sessions.assert_not_called()
    workers.kill.assert_not_called()
    workers.spawn.assert_not_called()
    assert json.loads((directory / ".state.json").read_text()) == state


def test_due_tick_spawns_when_previous_session_has_already_disappeared(scheduled):
    directory, workers, _ = scheduled
    workers.sessions.return_value = [{"name": "test-unrelated-previous"}]

    assert routines.tick() == ["hourly-check"]

    workers.status.assert_not_called()
    workers.kill.assert_not_called()
    workers.spawn.assert_called_once_with(
        "hourly-check", "Review pending tasks.", "/workspace/project", kind="routine"
    )
    assert json.loads((directory / ".state.json").read_text())["hourly-check"]["last_run"] == 10000.0


def test_failed_replacement_does_not_advance_schedule_and_can_retry(scheduled):
    directory, workers, original = scheduled
    workers.status.return_value = "done"
    workers.spawn.side_effect = RuntimeError("Worker launch unavailable")

    with pytest.raises(RuntimeError, match="Worker launch unavailable"):
        routines.tick()

    workers.kill.assert_called_once_with("test-hourly-check-previous")
    assert json.loads((directory / ".state.json").read_text()) == original
    workers.sessions.return_value = []
    workers.spawn.side_effect = None
    assert routines.tick() == ["hourly-check"]
    assert json.loads((directory / ".state.json").read_text())["hourly-check"]["last_run"] == 10000.0
