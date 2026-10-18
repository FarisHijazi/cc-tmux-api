"""Lifecycle census tests use temporary SQLite and never probe real hosts."""

import copy
import sqlite3

import pytest

from cc_tmux_api import session_history
from cc_tmux_api.session_history import RETENTION_SECONDS, SessionHistory


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(session_history.time, "time", lambda: now[0])
    return now


@pytest.fixture
def history(tmp_path):
    return SessionHistory(tmp_path)


def session(id="generation-one", host="local", **fields):
    return {"id": id, "host": host, "host_label": "Local", "title": "Example task",
            "workdir": "/workspace/project", "status": "working", "status_emoji": "opaque-token",
            "status_label": "Processing", "updated_at": 123, "revision": "screen-one",
            "control": {"session_id": id, "revision": "screen-one"},
            "menu": {"options": ["Example"]}, "notification_eligible": True, **fields}


def snapshot(sessions=(), available=True, host="local"):
    return {"hosts": [{"id": host, "available": available}], "sessions": list(sessions),
            "updated_at": 456, "extra": "preserved"}


def one(history, value):
    return history.observe(value)["sessions"][0]


def test_native_disconnect_and_restart_do_not_imply_end(history, clock):
    row = session(transport='native', lifecycle='alive', native_control={'request_id': 'p1'})
    one(history, snapshot([row]))
    clock[0] = 1100
    disconnected = one(history, snapshot([dict(row, lifecycle='unreachable')]))
    assert disconnected['lifecycle'] == 'unreachable' and disconnected['ended_at'] is None
    assert disconnected['last_seen'] == 1000 and disconnected['native_control'] is None
    assert one(history, snapshot())['lifecycle'] == 'unreachable'
    clock[0] = 1200
    ended = one(history, snapshot([dict(row, lifecycle='ended')]))
    assert ended['lifecycle'] == 'ended' and ended['ended_at'] == 1200
    assert one(history, snapshot())['lifecycle'] == 'ended'


def test_alive_census_preserves_source_and_controls_without_mutation(history, clock):
    source = snapshot([session()])
    original = copy.deepcopy(source)
    output = history.observe(source)
    live = output["sessions"][0]
    assert (live["alive"], live["lifecycle"], live["last_seen"], live["ended_at"]) == (
        True, "alive", 1000, None)
    assert live["control"] == source["sessions"][0]["control"]
    assert live["menu"] == source["sessions"][0]["menu"]
    assert live["status_emoji"] == "opaque-token"
    assert live["updated_at"] == 123 and output["updated_at"] == 456
    assert output["extra"] == "preserved" and source == original
    live["control"]["revision"] = "mutated-return"
    assert source == original


def test_disappearance_and_sticky_end_survive_clean_reload(history, tmp_path, clock):
    one(history, snapshot([session()]))
    clock[0] = 1100
    ended = one(SessionHistory(tmp_path), snapshot())
    assert (ended["alive"], ended["lifecycle"], ended["ended_at"]) == (False, "ended", 1100)
    assert ended["activity"] is None and ended["last_activity"] == "working"
    assert ended["status"] == "working" and ended["status_label"] == "Processing"
    assert ended["last_status"] == "working" and ended["last_status_label"] == "Processing"
    assert ended["last_seen"] == 1000
    assert ended["title"] == "Example task" and ended["workdir"] == "/workspace/project"
    assert ended["status_emoji"] == "opaque-token"
    assert ended["menu"] is ended["control"] is ended["revision"] is None
    assert not ended["notification_eligible"]
    clock[0] = 1200
    assert one(SessionHistory(tmp_path), snapshot()) == ended
    assert one(history, snapshot(available=False)) == ended


@pytest.mark.parametrize("gap", [snapshot(available=False), {"hosts": [], "sessions": []},
    {"hosts": [{"id": "local", "available": True}]},
    {**snapshot(), "stale": True}, {**snapshot(), "online": False}])
def test_missing_or_unavailable_is_never_evidence_of_end(history, tmp_path, clock, gap):
    one(history, snapshot([session()]))
    clock[0] = 1200
    unreachable = one(SessionHistory(tmp_path), gap)
    assert unreachable["alive"] is None
    assert unreachable["lifecycle"] == "unreachable" and unreachable["activity"] is None
    assert unreachable["last_activity"] == unreachable["status"] == "working"
    assert unreachable["last_seen"] == 1000 and unreachable["ended_at"] is None
    assert unreachable["last_status"] == "working"
    assert unreachable["status_emoji"] == "opaque-token"
    assert unreachable["control"] is unreachable["menu"] is unreachable["revision"] is None


def test_unavailable_host_does_not_refresh_stale_supplied_sessions(history, clock):
    one(history, snapshot([session()]))
    clock[0] = 1300
    result = one(history, snapshot([session(title="stale replacement")], available=False))
    assert result["title"] == "Example task" and result["last_seen"] == 1000
    assert result["alive"] is None


@pytest.mark.parametrize("gap", [snapshot(), snapshot(available=False)])
def test_same_identity_reappears_alive_after_end_or_outage(history, tmp_path, clock, gap):
    one(history, snapshot([session()]))
    one(history, gap)
    clock[0] = 1400
    returned = one(SessionHistory(tmp_path), snapshot([session(status="waiting", status_label="Question")]))
    assert returned["alive"] is True and returned["ended_at"] is None
    assert returned["lifecycle"] == "alive" and returned["last_seen"] == 1400
    assert returned["status"] == returned["last_status"] == "waiting"
    assert returned["last_status_label"] == "Question"
    assert returned["control"] and returned["menu"]


def test_generation_replacement_keeps_old_end_and_new_live(history, clock):
    one(history, snapshot([session()]))
    clock[0] = 1500
    result = history.observe(snapshot([session("generation-two")]))
    by_id = {item["id"]: item for item in result["sessions"]}
    assert len(by_id) == 2
    assert by_id["generation-one"]["ended_at"] == 1500
    assert by_id["generation-two"]["alive"] is True


@pytest.mark.parametrize("available", [True, False])
def test_activity_is_separate_from_lifecycle_and_status(history, tmp_path, available):
    live = one(history, snapshot([session(activity="tool", status="waiting")]))
    assert live["activity"] == live["last_activity"] == "tool"
    historical = one(SessionHistory(tmp_path), snapshot(available=available))
    assert historical["activity"] is None
    assert historical["last_activity"] == "tool"
    assert historical["status"] == historical["last_status"] == "waiting"
    assert historical["status_label"] == historical["last_status_label"] == "Processing"
    assert historical["lifecycle"] == ("ended" if available else "unreachable")
    returned = one(history, snapshot([session(activity="idle", status="idle")]))
    assert returned["lifecycle"] == "alive"
    assert returned["activity"] == returned["last_activity"] == "idle"


def test_existing_history_without_last_activity_keeps_known_activity(history):
    one(history, snapshot([session(activity="tool")]))
    # Simulate the pre-last_activity summary format without migrating the DB.
    with sqlite3.connect(history._path) as db:
        db.execute("UPDATE session_history SET summary=json_remove(summary, '$.last_activity')")
    retained = one(history, snapshot())
    assert retained["activity"] is None and retained["last_activity"] == "tool"


def test_each_host_census_is_independent(history, clock):
    hosts = [{"id": "local", "available": True}, {"id": "ssh-host", "available": True}]
    history.observe({"hosts": hosts, "sessions": [session(), session("remote", "ssh-host")]})
    hosts[1]["available"] = False
    result = history.observe({"hosts": hosts, "sessions": []})
    by_id = {item["id"]: item for item in result["sessions"]}
    assert by_id["generation-one"]["lifecycle"] == "ended"
    assert by_id["remote"]["lifecycle"] == "unreachable"
    hosts[1]["available"] = True
    result = history.observe({"hosts": hosts, "sessions": []})
    assert all(item["lifecycle"] == "ended" for item in result["sessions"])


def test_thirty_day_retention_never_prunes_live_or_unreachable(history, tmp_path, clock):
    history.observe(snapshot([session("ended"), session("unreachable"), session("live")]))
    clock[0] = 1100
    history.observe(snapshot([session("unreachable"), session("live")]))
    clock[0] = 1100 + RETENTION_SECONDS - 1
    assert len(history.observe(snapshot(available=False))["sessions"]) == 3
    clock[0] += 1
    result = SessionHistory(tmp_path).observe(snapshot(available=False))
    assert {item["id"] for item in result["sessions"]} == {"unreachable", "live"}
    clock[0] += RETENTION_SECONDS
    result = history.observe(snapshot([session("live")]))
    assert {item["id"]: item["lifecycle"] for item in result["sessions"]} == {
        "unreachable": "ended", "live": "alive"}


def test_private_separate_store_and_environment_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TMUX_STATE_DIR", str(tmp_path))
    history = SessionHistory()
    one(history, snapshot([session(tail="private terminal", messages=["private transcript"])]))
    assert history._path.name == "session-history.sqlite3"
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    assert history._path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(history._path) as db:
        stored = db.execute("SELECT summary FROM session_history").fetchone()[0]
    assert "private terminal" not in stored and "private transcript" not in stored
    assert '"control"' not in stored and '"menu"' not in stored and '"revision"' not in stored


def test_duplicate_ids_reject_census_without_marking_missing_ended(history):
    one(history, snapshot([session()]))
    with pytest.raises(ValueError, match="unique"):
        history.observe(snapshot([session("duplicate"), session("duplicate")]))
    retained = one(history, snapshot(available=False))
    assert retained["id"] == "generation-one" and retained["ended_at"] is None
