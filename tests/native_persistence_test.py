"""NativeRemote owns one transactional record per session, including observer receipts."""

import copy
import json
import os
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cc_tmux_api.native_remote import NativeRemote

TOKEN = "test-native-persistence-" + "x" * 40


def stored(path):
    with closing(sqlite3.connect(path)) as db:
        return {sid: {**json.loads(payload), **json.loads(observations)}
                for sid, payload, observations in db.execute(
                    "SELECT s.id,s.payload,o.payload FROM native_sessions s "
                    "JOIN native_observations o ON o.id=s.id")}


def legacy(path):
    sessions = {
        "cse_" + uuid4().hex: {"id": None, "epoch": 7, "worker_hash": "retained-hash",
                              "archived": False, "messages": [{"text": "مرحبا"}],
                              "observer": {"cursor": "retained"}, "unknown": [True, None, 12]},
        "cse_" + uuid4().hex: {"id": None, "archived": True, "inbound": ["retained"]},
    }
    for sid, session in sessions.items():
        session["id"] = sid
    path.write_text(json.dumps({"version": 1, "sessions": sessions}))
    path.chmod(0o600)
    return sessions


def test_legacy_import_is_exact_private_and_never_reimported(tmp_path):
    source = tmp_path / "native-remote.json"
    expected = legacy(source)
    original = source.read_bytes()
    backend = NativeRemote(tmp_path, TOKEN)
    assert backend._path.name == "native-remote.sqlite3"
    assert stored(backend._path) == expected == backend._sessions
    assert source.read_bytes() == original
    assert backend._path.stat().st_mode & 0o777 == 0o600
    sid = next(iter(expected))
    backend._sessions[sid]["unknown"].append("new")
    backend._save(backend._sessions[sid])
    source.write_text("invalid obsolete snapshot")
    restarted = NativeRemote(tmp_path, TOKEN)
    assert restarted._sessions == backend._sessions


def test_failed_migration_keeps_legacy_and_retries_atomically(tmp_path, monkeypatch):
    source = tmp_path / "native-remote.json"
    expected = legacy(source)
    original = source.read_bytes()
    replace = os.replace

    def fail(source_path, destination):
        assert stored(source_path) == expected
        assert not destination.exists()
        raise OSError("simulated migration interruption before publication")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="migration interruption"):
        NativeRemote(tmp_path, TOKEN)
    assert source.read_bytes() == original
    assert not (tmp_path / "native-remote.sqlite3").exists()
    monkeypatch.setattr(os, "replace", replace)
    assert NativeRemote(tmp_path, TOKEN)._sessions == expected


def test_interrupted_migration_transaction_leaves_no_published_partial_database(tmp_path, monkeypatch):
    source = tmp_path / "native-remote.json"
    expected = legacy(source)
    connect = sqlite3.connect

    class Interrupted(sqlite3.Connection):
        def executemany(self, statement, parameters):
            first = next(iter(parameters))
            self.execute(statement, first)
            raise sqlite3.OperationalError("simulated interruption during import")

    def fail(*args, **kwargs):
        return connect(*args, **kwargs, factory=Interrupted)

    monkeypatch.setattr(sqlite3, "connect", fail)
    with pytest.raises(sqlite3.OperationalError, match="interruption during import"):
        NativeRemote(tmp_path, TOKEN)
    assert not (tmp_path / "native-remote.sqlite3").exists()
    monkeypatch.setattr(sqlite3, "connect", connect)
    assert NativeRemote(tmp_path, TOKEN)._sessions == expected


def test_sqlite_rollback_journal_is_private_and_full_sync_is_enabled(tmp_path, monkeypatch):
    legacy(tmp_path / "native-remote.json")
    backend = NativeRemote(tmp_path, TOKEN)
    session = next(iter(backend._sessions.values()))
    session["epoch"] = 8
    connect = sqlite3.connect
    observed = []

    def inspect(*args, **kwargs):
        db = connect(*args, **kwargs)

        def trace(statement):
            if statement.startswith("DELETE FROM"):
                observed.append((backend._path.with_name(backend._path.name + "-journal")
                                 .stat().st_mode & 0o777,
                                 db.execute("PRAGMA synchronous").fetchone()[0]))

        db.set_trace_callback(trace)
        return db

    monkeypatch.setattr(sqlite3, "connect", inspect)
    backend._save(session, deleted=["absent"])
    assert observed == [(0o600, 2)]


@pytest.mark.parametrize("kind", ["invalid", "empty", "future", "wrong-shape"])
def test_existing_invalid_database_never_falls_back_to_legacy(tmp_path, kind):
    source = tmp_path / "native-remote.json"
    legacy(source)
    database = tmp_path / "native-remote.sqlite3"
    if kind == "invalid":
        database.write_bytes(b"not a sqlite database")
    else:
        with closing(sqlite3.connect(database)) as db, db:
            if kind == "future":
                db.execute("PRAGMA user_version=99")
            elif kind == "wrong-shape":
                db.execute("PRAGMA user_version=1")
                db.execute("CREATE TABLE native_sessions (wrong TEXT)")
    before = database.read_bytes()
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        NativeRemote(tmp_path, TOKEN)
    assert database.read_bytes() == before


def test_updates_write_only_requested_record_and_deletes_are_durable(tmp_path):
    expected = legacy(tmp_path / "native-remote.json")
    backend = NativeRemote(tmp_path, TOKEN)
    first, second = expected
    with closing(sqlite3.connect(backend._path)) as db, db:
        db.execute("CREATE TABLE audit (id TEXT)")
        db.execute("CREATE TRIGGER record_update AFTER UPDATE ON native_sessions "
                   "BEGIN INSERT INTO audit VALUES (NEW.id); END")
    identity = backend._sessions[first]
    identity["epoch"] = 8
    backend._save(identity)
    assert backend._sessions[first] is identity
    persisted = stored(backend._path)
    assert persisted[first]["epoch"] == 8
    assert persisted[second] == expected[second]
    with closing(sqlite3.connect(backend._path)) as db:
        assert db.execute("SELECT id FROM audit").fetchall() == [(first,)]
    backend._sessions.pop(second)
    backend._save(deleted=[second])
    assert NativeRemote(tmp_path, TOKEN)._sessions == {first: identity}
    with closing(sqlite3.connect(backend._path)) as db:
        assert db.execute("SELECT id FROM native_observations WHERE id=?", (second,)).fetchall() == []


@pytest.mark.parametrize("kind", ["missing", "orphan", "unexpected-field"])
def test_invalid_observation_ownership_refuses_startup(tmp_path, kind):
    expected = legacy(tmp_path / "native-remote.json")
    backend = NativeRemote(tmp_path, TOKEN)
    sid = next(iter(expected))
    with closing(sqlite3.connect(backend._path)) as db, db:
        if kind == "missing":
            db.execute("DELETE FROM native_observations WHERE id=?", (sid,))
        elif kind == "orphan":
            db.execute("INSERT INTO native_observations VALUES (?,?)", ("orphan", "{}"))
        else:
            db.execute("UPDATE native_observations SET payload=? WHERE id=?",
                       ('{"claude_session_id":"not-observation-data"}', sid))
    with pytest.raises(ValueError, match="observation"):
        NativeRemote(tmp_path, TOKEN)


@pytest.mark.parametrize("initial_binding", [True, False])
def test_real_commit_failure_rolls_back_mod_memory_and_never_acknowledges(tmp_path, initial_binding):
    backend = NativeRemote(tmp_path, TOKEN)
    app = FastAPI()
    app.include_router(backend.router())
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"x-hq-native-key": TOKEN}
    sid = client.post("/v1/code/sessions", headers=headers, json={}).json()["session"]["id"]
    conversation = str(uuid4())
    body = {"version": 1, "native_session_id": sid, "claude_session_id": conversation,
            "events": [{"id": f"{uuid4()}:1", "kind": "turn.start", "data": {}}]}
    if not initial_binding:
        assert client.post("/v1/code/sessions/mod-events", headers=headers, json=body).status_code == 200
        body["events"][0]["id"] = body["events"][0]["id"][:-1] + "2"
    identity = backend._sessions[sid]
    previous = copy.deepcopy(identity)
    cursor = backend.observe(sid)["cursor"]
    with closing(sqlite3.connect(backend._path)) as db, db:
        table = "native_sessions" if initial_binding else "native_observations"
        db.execute(f"CREATE TRIGGER reject_commit BEFORE UPDATE ON {table} "
                   "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END")
    response = client.post("/v1/code/sessions/mod-events", headers=headers, json=body)
    assert response.status_code == 500
    assert backend._sessions[sid] is identity
    assert identity == previous == NativeRemote(tmp_path, TOKEN)._sessions[sid]
    assert backend.observe(sid, after=cursor)["events"] == []
    with closing(sqlite3.connect(backend._path)) as db, db:
        db.execute("DROP TRIGGER reject_commit")
    accepted = client.post("/v1/code/sessions/mod-events", headers=headers, json=body)
    assert accepted.status_code == 200
    assert accepted.json()["results"][0]["duplicate"] is False
    restarted = NativeRemote(tmp_path, TOKEN)
    receipt = restarted.accept_mod_events(body)
    assert receipt["results"][0]["duplicate"] is True
    assert len(restarted.observe(sid)["events"]) == (1 if initial_binding else 2)


def test_passive_mod_does_not_rewrite_history_but_binding_is_one_transaction(tmp_path):
    backend = NativeRemote(tmp_path, TOKEN)
    app = FastAPI()
    app.include_router(backend.router())
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"x-hq-native-key": TOKEN}
    sid = client.post("/v1/code/sessions", headers=headers, json={}).json()["session"]["id"]
    conversation, producer = str(uuid4()), str(uuid4())
    body = {"version": 1, "native_session_id": sid, "claude_session_id": conversation,
            "events": [{"id": f"{producer}:1", "kind": "turn.start", "data": {}}]}
    assert client.post("/v1/code/sessions/mod-events", headers=headers, json=body).status_code == 200
    original_messages = backend._sessions[sid]["messages"]
    original_metadata = backend._sessions[sid]["external_metadata"]
    with closing(sqlite3.connect(backend._path)) as db, db:
        db.execute("CREATE TRIGGER forbid_history_write BEFORE UPDATE OF payload ON native_sessions "
                   "BEGIN SELECT RAISE(ABORT, 'history must not be rewritten'); END")
    body["events"][0]["id"] = f"{producer}:2"
    assert client.post("/v1/code/sessions/mod-events", headers=headers, json=body).status_code == 200
    assert backend._sessions[sid]["messages"] is original_messages
    assert backend._sessions[sid]["external_metadata"] is original_metadata
    before = copy.deepcopy(backend._sessions[sid])
    rotated = {**body, "claude_session_id": str(uuid4()), "previous_claude_session_id": conversation,
               "events": [{"id": f"{uuid4()}:1", "kind": "turn.start", "data": {}}]}
    assert client.post("/v1/code/sessions/mod-events", headers=headers, json=rotated).status_code == 500
    assert backend._sessions[sid] == before == NativeRemote(tmp_path, TOKEN)._sessions[sid]
