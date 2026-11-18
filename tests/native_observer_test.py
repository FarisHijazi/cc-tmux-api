"""Passive observations share the native owner's identity, lock and durable registry."""

import asyncio
import copy
import json
import threading
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cc_tmux_api import native_remote
from cc_tmux_api.fleet import FleetError
from cc_tmux_api.native_remote import NativeRemote

TOKEN = "test-observer-" + "x" * 40


@pytest.fixture
def observer(tmp_path):
    backend = NativeRemote(tmp_path, TOKEN)
    app = FastAPI()
    app.include_router(backend.router())
    client = TestClient(app)
    headers = {"x-hq-native-key": TOKEN}
    sid = client.post("/v1/code/sessions", json={}, headers=headers).json()["session"]["id"]
    conversation = str(uuid4())
    backend._sessions[sid]["claude_session_id"] = conversation
    backend._save()
    return backend, client, headers, sid, conversation


def event(producer, sequence=1, kind="turn.start", data=None):
    return {"id": f"{producer}:{sequence}", "kind": kind, "data": data or {"turnId": "turn-1"}}


def publish(observer, events, **changes):
    _, client, headers, _, conversation = observer
    return client.post("/v1/code/sessions/mod-events", headers=headers,
                       json={"version": 1, "claude_session_id": conversation,
                             "events": events, **changes})


def test_mod_events_require_launch_auth_and_exact_unarchived_identity(observer):
    backend, client, headers, sid, conversation = observer
    body = {"version": 1, "claude_session_id": conversation, "events": [event(uuid4())]}
    assert client.post("/v1/code/sessions/mod-events", json=body).status_code == 401
    assert publish(observer, body["events"], claude_session_id=str(uuid4())).status_code == 404
    duplicate = copy.deepcopy(backend._sessions[sid])
    duplicate["id"] = "cse_duplicate"
    backend._sessions[duplicate["id"]] = duplicate
    assert publish(observer, body["events"]).status_code == 409
    duplicate["archived"] = True
    assert publish(observer, body["events"]).status_code == 200
    backend._sessions[sid]["archived"] = True
    assert publish(observer, body["events"]).status_code == 404
    assert len(backend._sessions) == 2


def test_mod_acceptance_is_passive_and_deduplicated(observer, monkeypatch):
    backend, _, _, sid, _ = observer
    before = copy.deepcopy(backend._sessions[sid])
    for method in ("_ingest", "_queue", "_touch", "_mirror"):
        monkeypatch.setattr(backend, method, lambda *a: pytest.fail("Observation became control"))
    item = event(uuid4(), data={"text": "observed, not submitted"})
    reply = publish(observer, [item, item])
    assert reply.status_code == 200
    assert reply.json()["claude_session_id"] == observer[4]
    assert [r["duplicate"] for r in reply.json()["results"]] == [False, True]
    assert publish(observer, [item]).json()["results"][0]["duplicate"] is True
    after = {k: v for k, v in backend._sessions[sid].items()
             if k not in ("observer", "mod_producers")}
    assert after == before
    observed = backend.observe(sid)
    assert observed["session_id"] == sid and observed["reset"] is False
    assert len(observed["events"]) == 1
    assert observed["events"][0] == {
        "cursor": observed["cursor"], "source": "mod", "kind": item["kind"],
        "id": item["id"], "data": item["data"],
    }
    assert backend.observe(sid, observed["cursor"])["events"] == []


def test_conflicting_or_nonconsecutive_ids_reject_whole_batch(observer):
    backend, _, _, sid, _ = observer
    producer = uuid4()
    assert publish(observer, [event(producer)]).status_code == 200
    before = copy.deepcopy(backend._sessions[sid])
    assert publish(observer, [event(producer, 2), event(producer, data={"changed": True})]).status_code == 409
    assert backend._sessions[sid] == before
    assert publish(observer, [event(producer, 3)]).status_code == 409
    assert backend._sessions[sid] == before


def test_authenticated_explicit_native_pair_binds_without_control_or_liveness(observer, monkeypatch):
    backend, client, headers, sid, conversation = observer
    backend._sessions[sid]['claude_session_id'] = sid
    before = copy.deepcopy(backend._sessions[sid])
    item = event(uuid4())
    body = {'version': 1, 'claude_session_id': conversation,
            'native_session_id': sid, 'events': [item]}
    assert client.post('/v1/code/sessions/mod-events', json=body).status_code == 401
    for method in ('_ingest', '_queue', '_touch', '_mirror'):
        monkeypatch.setattr(backend, method, lambda *a: pytest.fail('Binding became control'))
    reply = client.post('/v1/code/sessions/mod-events', json=body, headers=headers)
    assert reply.status_code == 200 and reply.json()['session_id'] == sid
    assert reply.json()['claude_session_id'] == conversation
    assert client.post('/v1/code/sessions/mod-events', json=body, headers=headers).json()['results'][0]['duplicate']
    after = {k: v for k, v in backend._sessions[sid].items()
             if k not in ('observer', 'mod_producers', 'claude_session_id')}
    assert after == {k: v for k, v in before.items() if k != 'claude_session_id'}
    assert NativeRemote(backend._path.parent, TOKEN).detail(sid)['claude_session_id'] == conversation


def test_native_pair_cas_rejects_stale_rebinding_and_producer_reuse(observer):
    backend, _, _, sid, first = observer
    producer1, producer2 = uuid4(), uuid4()
    assert publish(observer, [event(producer1)], native_session_id=sid).status_code == 200
    second = str(uuid4())
    assert publish(observer, [event(producer2)], native_session_id=sid,
                   claude_session_id=second).status_code == 409
    assert publish(observer, [event(producer2)], native_session_id=sid,
                   claude_session_id=second, previous_claude_session_id=first).status_code == 200
    snapshot = copy.deepcopy(backend._sessions[sid])
    # Lost acknowledgments from an old publisher cannot return the binding to its old UUID.
    assert publish(observer, [event(producer1)], native_session_id=sid).status_code == 409
    assert publish(observer, [event(producer1)], native_session_id=sid,
                   previous_claude_session_id=second).status_code == 409
    assert publish(observer, [event(producer1, 2)], native_session_id=sid,
                   claude_session_id=second).status_code == 409
    assert backend._sessions[sid] == snapshot
    # An intentional return to a previous conversation uses a fresh publisher and exact CAS.
    assert publish(observer, [event(uuid4())], native_session_id=sid,
                   previous_claude_session_id=second).status_code == 200
    assert backend.detail(sid)['claude_session_id'] == first


def test_native_pair_unknown_archived_ambiguous_or_conflicting_targets_reject(observer):
    backend, _, _, sid, conversation = observer
    assert publish(observer, [event(uuid4())], native_session_id='cse_' + '0' * 32).status_code == 404
    other = copy.deepcopy(backend._sessions[sid])
    other['id'] = 'cse_' + '1' * 32
    backend._sessions[other['id']] = other
    assert publish(observer, [event(uuid4())], native_session_id=sid).status_code == 409
    other['claude_session_id'] = str(uuid4())
    assert publish(observer, [event(uuid4())], native_session_id=other['id']).status_code == 409
    other['archived'] = True
    assert publish(observer, [event(uuid4())], native_session_id=other['id']).status_code == 404
    assert backend._sessions[sid]['claude_session_id'] == conversation


def test_legacy_producer_is_pinned_before_conversation_rotation(observer):
    backend, _, _, sid, first = observer
    producer = uuid4()
    assert publish(observer, [event(producer)]).status_code == 200
    backend._sessions[sid]['mod_producers'][str(producer)].pop('claude_session_id')
    second = str(uuid4())
    assert publish(observer, [event(uuid4())], native_session_id=sid,
                   claude_session_id=second, previous_claude_session_id=first).status_code == 200
    before = copy.deepcopy(backend._sessions[sid])
    assert publish(observer, [event(producer, 2)], native_session_id=sid,
                   claude_session_id=second).status_code == 409
    assert backend._sessions[sid] == before
    assert before['mod_producers'][str(producer)]['claude_session_id'] == first


def test_native_pair_binding_rolls_back_if_batch_or_durable_save_fails(observer, monkeypatch):
    backend, _, _, sid, _ = observer
    backend._sessions[sid]['claude_session_id'] = sid
    before = copy.deepcopy(backend._sessions[sid])
    assert publish(observer, [event(uuid4(), 2)], native_session_id=sid).status_code == 409
    assert backend._sessions[sid] == before
    save = backend._save
    def fail():
        raise OSError('binding persistence failed')
    monkeypatch.setattr(backend, '_save', fail)
    item = event(uuid4())
    with pytest.raises(OSError):
        publish(observer, [item], native_session_id=sid)
    assert backend._sessions[sid] == before
    monkeypatch.setattr(backend, '_save', save)
    assert publish(observer, [item], native_session_id=sid).status_code == 200


@pytest.mark.parametrize('action', ['send', 'interrupt'])
def test_conversation_rotation_invalidates_old_actions_but_not_on_retry(observer, action):
    backend, _, _, sid, first = observer
    backend._streams[sid] = {'epoch': backend._sessions[sid]['epoch']}
    original = backend.detail(sid)
    stable = {key: copy.deepcopy(backend._sessions[sid][key])
              for key in ('last_seen', 'messages', 'inbound')}
    second, producer = str(uuid4()), uuid4()
    parameters = {'native_session_id': sid, 'claude_session_id': second,
                  'previous_claude_session_id': first}
    assert publish(observer, [event(producer)], **parameters).status_code == 200
    with pytest.raises(FleetError, match='changed; refresh'):
        backend.act(sid, action, original['revision'], 'old-observation', text='never send')
    current = backend.detail(sid)['revision']
    assert int(current) > int(original['revision'])
    assert backend._sessions[sid]['input_revision'] == int(current)
    assert publish(observer, [event(producer)], **parameters).status_code == 200
    assert publish(observer, [event(producer, 2)], **parameters).status_code == 200
    assert backend.detail(sid)['revision'] == current
    assert {key: backend._sessions[sid][key] for key in stable} == stable


def test_rotation_command_fence_rolls_back_with_failed_durable_save(observer, monkeypatch):
    backend, _, _, sid, first = observer
    before = copy.deepcopy(backend._sessions[sid])
    def fail():
        raise OSError('rotation persistence failed')
    monkeypatch.setattr(backend, '_save', fail)
    with pytest.raises(OSError):
        publish(observer, [event(uuid4())], native_session_id=sid,
                claude_session_id=str(uuid4()), previous_claude_session_id=first)
    assert backend._sessions[sid] == before


def test_native_init_bridge_placeholder_does_not_overwrite_explicit_uuid(observer):
    backend, _, _, sid, conversation = observer
    with backend._lock:
        backend._ingest(backend._sessions[sid], {'type': 'system', 'subtype': 'init',
                                                'session_id': sid})
    assert backend.detail(sid)['claude_session_id'] == conversation


def test_native_init_canonical_identity_rotation_also_invalidates_old_actions(observer):
    backend, _, _, sid, _ = observer
    backend._streams[sid] = {'epoch': backend._sessions[sid]['epoch']}
    original = backend.detail(sid)['revision']
    with backend._lock:
        backend._ingest(backend._sessions[sid], {'type': 'system', 'subtype': 'init',
                                                'session_id': str(uuid4())})
        backend._save()
    with pytest.raises(FleetError, match='changed; refresh'):
        backend.act(sid, 'send', original, 'old-native-identity', text='never send')
    assert backend._sessions[sid]['inbound'] == []


@pytest.mark.parametrize("changes", [
    {"version": True}, {"version": 2}, {"claude_session_id": "cse_alias"},
    {"native_session_id": "not-a-native-route"},
    {"previous_claude_session_id": str(uuid4())},
    {"native_session_id": 'cse_' + '0' * 32, "previous_claude_session_id": "wrong"},
    {"events": []}, {"events": [{"id": "arbitrary", "kind": "turn.start", "data": {}}]},
    {"events": [event(uuid4(), kind="control_request")]},
    {"events": [{**event(uuid4()), "extra": "not permitted"}]},
])
def test_invalid_mod_batches_do_not_mutate(observer, changes):
    backend, _, _, sid, _ = observer
    before = copy.deepcopy(backend._sessions[sid])
    assert publish(observer, **{"events": [event(uuid4())], **changes}).status_code == 400
    assert backend._sessions[sid] == before


def test_mod_bounds_and_json_depth(observer, monkeypatch):
    backend, _, _, sid, _ = observer
    before = copy.deepcopy(backend._sessions[sid])
    assert publish(observer, [event(uuid4(), data={"text": "x" * (64 * 1024)})]).status_code == 413
    nested = {}
    for _ in range(40):
        nested = {"nested": nested}
    assert publish(observer, [event(uuid4(), data=nested)]).status_code == 400
    invalid = {"version": 1, "claude_session_id": observer[4],
               "events": [event(uuid4(), data={"invalid": float('nan')})]}
    assert observer[1].post("/v1/code/sessions/mod-events", headers=observer[2],
                            content=json.dumps(invalid)).status_code == 400
    monkeypatch.setattr(native_remote, "MAX_MOD_PRODUCERS", 1)
    assert publish(observer, [event(uuid4())]).status_code == 200
    assert publish(observer, [event(uuid4())]).status_code == 409
    assert backend._sessions[sid]["messages"] == before["messages"]


def test_durable_receipts_and_expired_dedupe_never_reappend(observer, monkeypatch):
    backend, _, _, sid, _ = observer
    producer = uuid4()
    monkeypatch.setattr(native_remote, "MAX_MOD_RECEIPTS", 2)
    assert publish(observer, [event(producer, n) for n in range(1, 4)]).status_code == 200
    cursor = backend.observe(sid)["cursor"]
    restarted = NativeRemote(backend._path.parent, TOKEN)
    assert restarted.observe(sid)["cursor"] == cursor
    replay = restarted.accept_mod_events({"version": 1, "claude_session_id": observer[4],
                                         "events": [event(producer, 3)]})
    assert replay["results"][0]["duplicate"] is True
    assert publish(observer, [event(producer, 1)]).status_code == 409
    assert backend.observe(sid)["cursor"] == cursor


def test_observer_cursor_expiry_reset_and_validation(observer, monkeypatch):
    backend, _, _, sid, _ = observer
    monkeypatch.setattr(native_remote, "MAX_OBSERVER_EVENTS", 2)
    initial = backend.observe(sid)["cursor"]
    producer = uuid4()
    assert publish(observer, [event(producer, n) for n in range(1, 4)]).status_code == 200
    current = backend.observe(sid)
    assert len(current["events"]) == 2
    expired = backend.observe(sid, initial)
    assert expired == {"session_id": sid, "events": [], "cursor": current["cursor"],
                       "reset": True, "gap": {"reason": "expired"}}
    assert backend.observe(sid, f"{uuid4()}:0")["gap"] == {"reason": "reset"}
    for cursor in ("bad", "", "x" * 200):
        with pytest.raises(FleetError, match="cursor"):
            backend.observe(sid, cursor)
    with pytest.raises(FleetError, match="wait"):
        backend.observe(sid, wait=21)


def test_save_failure_rolls_back_mod_acceptance(observer, monkeypatch):
    backend, _, _, sid, _ = observer
    before = copy.deepcopy(backend._sessions[sid])
    persisted = backend._path.read_bytes()
    def fail():
        raise OSError("simulated disk failure")
    monkeypatch.setattr(backend, "_save", fail)
    with pytest.raises(OSError, match="disk failure"):
        publish(observer, [event(uuid4())])
    assert backend._sessions[sid] == before
    assert backend._path.read_bytes() == persisted


def test_native_ingest_emits_approved_raw_events_without_replaying_commands(observer):
    backend, client, headers, sid, _ = observer
    bridge = client.post(f"/v1/code/sessions/{sid}/bridge", json={}, headers=headers).json()
    auth = {"Authorization": "Bearer " + bridge["worker_jwt"]}
    payload = {"type": "assistant", "uuid": "native-message", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "Native answer"}]}}
    batch = {"worker_epoch": bridge["worker_epoch"], "events": [{"payload": payload}]}
    assert client.post(f"/v1/code/sessions/{sid}/worker/events", json=batch, headers=auth).status_code == 200
    revision = backend._sessions[sid]["revision"]
    assert client.post(f"/v1/code/sessions/{sid}/worker/events", json=batch, headers=auth).status_code == 200
    observed = backend.observe(sid)
    assert len(observed["events"]) == 1
    assert observed["events"][0]["source"] == "native"
    assert observed["events"][0]["data"] == payload
    assert backend._sessions[sid]["revision"] == revision
    assert len(backend._sessions[sid]["messages"]) == 1
    assert not backend._sessions[sid]["inbound"]


def test_native_save_failure_does_not_publish_or_deduplicate_uncommitted_events(observer, monkeypatch):
    backend, client, headers, sid, _ = observer
    bridge = client.post(f"/v1/code/sessions/{sid}/bridge", json={}, headers=headers).json()
    auth = {"Authorization": "Bearer " + bridge["worker_jwt"]}
    body = {"worker_epoch": bridge["worker_epoch"], "events": [{"payload": {
        "type": "assistant", "uuid": "retry-native", "message": {
            "content": [{"type": "text", "text": "Only once after persistence"}]}}}]}
    before = copy.deepcopy(backend._sessions[sid])
    save = backend._save
    def fail():
        raise OSError("simulated disk failure")
    monkeypatch.setattr(backend, "_save", fail)
    with pytest.raises(OSError):
        client.post(f"/v1/code/sessions/{sid}/worker/events", json=body, headers=auth)
    assert backend._sessions[sid] == before
    assert backend.observe(sid)["events"] == []
    monkeypatch.setattr(backend, "_save", save)
    reply = client.post(f"/v1/code/sessions/{sid}/worker/events", json=body, headers=auth)
    assert reply.status_code == 200 and reply.json()["results"][0]["duplicate"] is False
    assert len(backend.observe(sid)["events"]) == 1


def test_observer_byte_budget_and_oversized_native_event_are_explicit(observer, monkeypatch):
    backend, _, _, sid, _ = observer
    monkeypatch.setattr(native_remote, "MAX_OBSERVER_BYTES", 1024)
    producer = uuid4()
    assert publish(observer, [event(producer, n, data={"text": "x" * 600})
                             for n in range(1, 4)]).status_code == 200
    assert len(backend.observe(sid)["events"]) == 1
    with backend._lock:
        backend._ingest(backend._sessions[sid], {
            "type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 70000}]}})
        backend._save()
    last = backend.observe(sid)["events"][-1]
    assert last["source"] == "native" and last["kind"] == "gap"
    assert last["data"] == {"reason": "event_too_large", "kind": "assistant"}


def test_empty_observer_reads_and_timeout_do_not_mutate_session(observer):
    backend, _, _, sid, _ = observer
    before = copy.deepcopy(backend._sessions[sid])
    initial = backend.observe(sid)
    assert backend.observe(sid, initial["cursor"], 0.01) == initial
    assert backend._sessions[sid] == before


@pytest.mark.parametrize("payload,reason", [
    ({"type": ""}, "invalid_kind"),
    ({"type": "x" * 70000}, "event_too_large"),
    ({"type": "status", "value": float('nan')}, "invalid_json"),
    ({"type": "\ud800"}, "invalid_json"),
])
def test_unrepresentable_native_observations_have_bounded_gaps(observer, payload, reason):
    backend, _, _, sid, _ = observer
    with backend._lock:
        backend._ingest(backend._sessions[sid], payload)
        backend._save()
    result = backend.observe(sid)
    assert len(result["events"]) == 1
    item = result["events"][0]
    assert item["kind"] == "gap" and item["data"]["reason"] == reason
    assert len(item["data"]["kind"]) <= 128


def test_long_poll_wakes_only_after_durable_acceptance(observer, monkeypatch):
    backend, client, _, sid, _ = observer
    entered, release = threading.Event(), threading.Event()
    save = backend._save
    cursor = backend.observe(sid)["cursor"]
    def slow_save():
        entered.set()
        assert release.wait(3)
        save()
    monkeypatch.setattr(backend, "_save", slow_save)
    @client.app.get("/health")
    async def health():
        return {"ok": True}
    async def exercise():
        observed = asyncio.create_task(asyncio.to_thread(backend.observe, sid, cursor, 2))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                     base_url="http://testserver") as http:
            write = asyncio.create_task(http.post("/v1/code/sessions/mod-events",
                headers=observer[2], json={"version": 1, "claude_session_id": observer[4],
                                          "events": [event(uuid4())]}))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                assert not observed.done() and not write.done()
                assert (await http.get("/health")).status_code == 200
            finally:
                release.set()
            assert (await write).status_code == 200
            result = await observed
            assert len(result["events"]) == 1
            saved = json.loads(backend._path.read_text())["sessions"][sid]["observer"]
            assert saved["events"][-1]["cursor"] == result["cursor"]
    asyncio.run(exercise())
