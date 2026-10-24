"""Upstream mirror: HQ's native sessions registered with a fake Anthropic Remote Control."""

import json
import queue
import threading
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from native_remote_test import connected, post, register

from cc_tmux_api import native_remote
from cc_tmux_api.native_remote import NativeRemote, get_backend
from cc_tmux_api.rc_mirror import Mirror

TOKEN = "test-" + "x" * 40
UPSTREAM_ID = "cse_upstream1"
LAUNCH = "Bearer proxy-account-personal"


class FakeUpstream:
    """Anthropic's worker-side Remote Control API, as the real CLI sees it."""

    def __init__(self):
        self.requests = []
        self.failures = {}
        self.streams = []
        self.emitted = []
        self.bridges = 0
        self.creates = 0
        self.interval = 0.05
        self.closed = threading.Event()
        self.lock = threading.Lock()

    def fail(self, method, suffix, *statuses):
        self.failures.setdefault((method, suffix), []).extend(statuses)

    def calls(self, method, suffix):
        with self.lock:
            return [r for r in self.requests if r["method"] == method and r["path"].endswith(suffix)]

    def emit(self, sequence, payload, event_id=None, source="client"):
        envelope = dict(sequence_num=str(sequence), event_id=event_id or f"up-{sequence}",
                        event_type="client_event", source=source, payload=payload)
        frame = f"event: client_event\nid: {sequence}\ndata: {json.dumps(envelope)}\n\n"
        self.emitted.append((sequence, frame.encode()))
        self.raw(frame)

    def raw(self, frame):
        wait_for(lambda: self.streams)
        self.streams[-1].put(frame.encode() if isinstance(frame, str) else frame)

    def drop_stream(self):
        self.streams[-1].put(None)

    def handle(self, request):
        path = request.url.path.removeprefix("/v1/code/sessions")
        body = json.loads(request.content) if request.content else None
        record = dict(method=request.method, path=path, body=body, headers=dict(request.headers),
                      params=dict(request.url.params))
        with self.lock:
            self.requests.append(record)
            pending = self.failures.get((request.method, path.removeprefix(f"/{UPSTREAM_ID}")))
            status = pending.pop(0) if pending else None
        if status:
            return httpx.Response(status, json={"type": "error"})
        if request.method == "POST" and path == "":
            self.creates += 1
            return httpx.Response(200, json={"session": {
                "id": UPSTREAM_ID, "session_url": "https://claude.ai/code/session_upstream1"}})
        if path.endswith("/bridge"):
            self.bridges += 1
            return httpx.Response(200, json={
                "worker_jwt": f"upstream-jwt-{self.bridges}", "worker_epoch": self.bridges,
                "expires_in": 3600, "api_base_url": "https://api.anthropic.com"})
        if path.endswith("/worker/events/stream"):
            lane = queue.Queue()
            after = int(request.url.params.get("from_sequence_num", 0))
            for sequence, frame in list(self.emitted):
                if sequence > after:
                    lane.put(frame)
            self.streams.append(lane)

            def frames():
                yield b": keepalive\n\n"
                while not self.closed.is_set():
                    try:
                        frame = lane.get(timeout=0.02)
                    except queue.Empty:
                        continue
                    if frame is None:
                        return
                    yield frame

            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=frames())
        if path.endswith("/worker/heartbeat"):
            return httpx.Response(200, json={"heartbeat_interval_seconds": self.interval})
        if path.endswith("/worker/events"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={})


def wait_for(check, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def mirrored_backend(state_dir, fake):
    backend = NativeRemote(state_dir, TOKEN)
    backend.upstream = Mirror(backend, transport=httpx.MockTransport(fake.handle),
                              base_url="https://upstream.test", backoff=0.01, max_backoff=0.05)
    return backend


@pytest.fixture
def hub(tmp_path):
    fake = FakeUpstream()
    backend = mirrored_backend(tmp_path, fake)
    app = FastAPI()
    app.include_router(backend.router())
    with TestClient(app) as client:
        yield backend, client, {"x-hq-native-key": TOKEN}, fake
    fake.closed.set()
    backend.upstream.close()


def native(hub):
    return hub[:3]


def mirrored(hub, title="Mirrored"):
    """A CLI registered with HQ, and HQ's mirror registered and streaming upstream."""
    backend, client, launch, fake = hub
    sid, auth, epoch = register(native(hub), title)
    wait_for(lambda: fake.streams)
    wait_for(lambda: backend.detail(sid)["upstream"]["status"] == "connected")
    return sid, auth, epoch


def test_registers_with_placeholder_owner_and_reports_upstream_link(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub, "Fix the tests")
    create = fake.calls("POST", "")[0]
    assert create["body"] == {"title": "Fix the tests", "bridge": {}, "tags": ["remote-control-repl"],
                              "config": {"cwd": "/tmp/project", "model": "test-model"}}
    assert create["headers"]["authorization"] == LAUNCH
    assert all(r["headers"]["x-hq-mirror"] == "1" for r in fake.requests)
    assert fake.calls("POST", "/bridge")[0]["headers"]["authorization"] == LAUNCH
    state = wait_for(lambda: fake.calls("PUT", "/worker"))[0]
    assert state["headers"]["authorization"] == "Bearer upstream-jwt-1"
    assert state["body"] == {"worker_epoch": 1, "worker_status": "idle", "external_metadata": {}}
    row = backend.rows()[0]
    assert row["upstream"] == {"session_url": "https://claude.ai/code/session_upstream1",
                               "status": "connected"}
    assert row["private"] is False
    assert "upstream-jwt" not in backend._path.read_text()


def test_forwards_cli_events_state_and_hq_typed_messages(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    assistant = {"type": "assistant", "uuid": "a-1",
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "Hi"}]}}
    assert post(native(hub), sid, auth, epoch, [assistant]).status_code == 200
    forwarded = wait_for(lambda: fake.calls("POST", "/worker/events"))[0]
    assert forwarded["body"] == {"worker_epoch": 1, "events": [{"payload": assistant}]}
    assert forwarded["headers"]["authorization"] == "Bearer upstream-jwt-1"

    put = client.put(f"/v1/code/sessions/{sid}/worker", headers=auth, json={
        "worker_epoch": epoch, "worker_status": "running",
        "external_metadata": {"model": "test-model"}})
    assert put.status_code == 200
    wait_for(lambda: any(r["body"].get("worker_status") == "running"
                         for r in fake.calls("PUT", "/worker")))

    backend.act(sid, "send", connected(backend, sid), "send-1", text="typed in HQ")
    sent = wait_for(lambda: [r for r in fake.calls("POST", "/worker/events")
                             if r["body"]["events"][0]["payload"]["type"] == "user"])[0]
    payload = sent["body"]["events"][0]["payload"]
    assert payload["message"]["content"][0]["text"] == "typed in HQ"
    assert "client_platform" not in payload
    assert payload["uuid"] == backend.replay(sid, 0)[0]["payload"]["uuid"]


def test_injects_claude_ai_events_and_maps_delivery_back(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    user = {"type": "user", "uuid": "phone-1", "client_platform": "ios",
            "message": {"role": "user", "content": [{"type": "text", "text": "from my phone"}]}}
    fake.emit(7, user, event_id="upstream-evt-7")
    local = wait_for(lambda: backend.replay(sid, 0))[0]
    assert local["payload"] == user
    assert backend.detail(sid)["messages"][-1]["text"] == "from my phone"

    delivery = client.post(f"/v1/code/sessions/{sid}/worker/events/delivery", headers=auth, json={
        "worker_epoch": epoch, "updates": [{"event_id": local["event_id"], "status": "processed"}]})
    assert delivery.status_code == 200
    ack = wait_for(lambda: fake.calls("POST", "/worker/events/delivery"))[0]
    assert ack["body"] == {"worker_epoch": 1,
                           "updates": [{"event_id": "upstream-evt-7", "status": "processed"}]}


def test_claude_ai_answer_clears_hq_pending_prompt(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    ask = {"type": "control_request", "request_id": "perm-1",
           "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls"}}}
    post(native(hub), sid, auth, epoch, [ask])
    connected(backend, sid)
    assert backend.detail(sid)["native_control"]["request_id"] == "perm-1"
    answer = {"type": "control_response", "response": {
        "request_id": "perm-1", "subtype": "success", "response": {"behavior": "allow"}}}
    fake.emit(3, answer)
    wait_for(lambda: backend.replay(sid, 0))
    assert backend.detail(sid)["native_control"] is None


def test_heartbeats_while_the_cli_is_live(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    beats = wait_for(lambda: len(fake.calls("POST", "/worker/heartbeat")) >= 2
                     and fake.calls("POST", "/worker/heartbeat"))
    body = beats[0]["body"]
    assert body["session_id"] == UPSTREAM_ID
    assert body["worker_epoch"] == 1
    assert body["supports_heartbeat_probe"] is False
    assert isinstance(body["idle_seconds"], int)


def test_rebridges_after_401_and_resends_with_new_epoch(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    fake.fail("POST", "/worker/events", 401)
    event = {"type": "assistant", "uuid": "a-2",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}}
    post(native(hub), sid, auth, epoch, [event])
    accepted = wait_for(lambda: [r for r in fake.calls("POST", "/worker/events")
                                 if r["body"]["worker_epoch"] == 2])
    assert fake.bridges >= 2
    assert accepted[0]["headers"]["authorization"] == "Bearer upstream-jwt-2"
    assert accepted[0]["body"]["events"] == [{"payload": event}]


def test_stream_reconnects_from_last_sequence(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    fake.emit(5, {"type": "control_request", "request_id": "init-1",
                  "request": {"subtype": "initialize"}})
    wait_for(lambda: backend.replay(sid, 0))
    fake.drop_stream()
    wait_for(lambda: len(fake.streams) >= 2)
    reconnect = fake.calls("GET", "/worker/events/stream")[-1]
    assert reconnect["params"]["from_sequence_num"] == "5"
    assert reconnect["headers"]["last-event-id"] == "5"
    fake.emit(5, {"type": "control_request", "request_id": "init-1",
                  "request": {"subtype": "initialize"}})
    fake.emit(6, {"type": "control_request", "request_id": "interrupt-1",
                  "request": {"subtype": "interrupt"}})
    wait_for(lambda: len(backend.replay(sid, 0)) == 2)
    time.sleep(0.1)
    assert [e["payload"]["request_id"] for e in backend.replay(sid, 0)] == ["init-1", "interrupt-1"]


def test_private_sessions_never_reach_upstream(hub):
    backend, client, launch, fake = hub
    create = client.post("/v1/code/sessions", headers={**launch, "x-hq-private": "1"},
                         json={"title": "Secret", "config": {"cwd": "/tmp/p"}})
    sid = create.json()["session"]["id"]
    bridge = client.post(f"/v1/code/sessions/{sid}/bridge", json={}, headers=launch).json()
    auth = {"Authorization": "Bearer " + bridge["worker_jwt"]}
    client.post(f"/v1/code/sessions/{sid}/worker/heartbeat", headers=auth,
                json={"worker_epoch": bridge["worker_epoch"]})
    post(native(hub), sid, auth, bridge["worker_epoch"], [{"type": "assistant", "uuid": "s",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "secret"}]}}])
    time.sleep(0.2)
    assert fake.requests == []
    row = backend.rows()[0]
    assert row["private"] is True
    assert row["upstream"] is None


def test_restart_resumes_same_upstream_session_from_cursor(hub, tmp_path):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    fake.emit(4, {"type": "control_request", "request_id": "init-1",
                  "request": {"subtype": "initialize"}})
    wait_for(lambda: backend.replay(sid, 0))
    backend.upstream.close()

    restarted = mirrored_backend(tmp_path, fake)
    assert restarted.detail(sid)["upstream"]["status"] == "disconnected"
    app = FastAPI()
    app.include_router(restarted.router())
    try:
        with TestClient(app) as second:
            bridge = second.post(f"/v1/code/sessions/{sid}/bridge", json={}, headers=launch).json()
            second.post(f"/v1/code/sessions/{sid}/worker/heartbeat",
                        headers={"Authorization": "Bearer " + bridge["worker_jwt"]},
                        json={"worker_epoch": bridge["worker_epoch"]})
            wait_for(lambda: len(fake.calls("GET", "/worker/events/stream")) >= 2)
            resumed = fake.calls("GET", "/worker/events/stream")[-1]
            assert resumed["path"] == f"/{UPSTREAM_ID}/worker/events/stream"
            assert resumed["params"]["from_sequence_num"] == "4"
            assert fake.creates == 1
    finally:
        restarted.upstream.close()


def test_archive_ends_upstream_and_controller_end_keeps_local_session(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    fake.emit(2, {"type": "control_request", "request_id": "end-1",
                  "request": {"subtype": "end_session"}})
    wait_for(lambda: backend.detail(sid)["upstream"]["status"] == "ended")
    assert backend.detail(sid)["lifecycle"] != "ended"
    assert backend.replay(sid, 0) == []
    count = len(fake.requests)
    post(native(hub), sid, auth, epoch, [{"type": "assistant", "uuid": "after",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}}])
    time.sleep(0.1)
    assert all(not r["path"].endswith("/worker/events") or r["body"]["events"][0]["payload"]["uuid"]
               != "after" for r in fake.requests[count:])

    fake.emitted.clear()  # the fake has one upstream id; the next session starts a fresh log
    other, _, _ = register(native(hub), "Second")
    wait_for(lambda: backend.detail(other).get("upstream"))
    assert client.post(f"/v1/code/sessions/{other}/archive", headers=launch).status_code == 200
    wait_for(lambda: fake.calls("POST", "/archive"))
    assert fake.calls("POST", "/archive")[0]["headers"]["authorization"] == LAUNCH


def test_unreachable_upstream_never_blocks_the_cli(hub):
    backend, client, launch, fake = hub
    release = threading.Event()
    original = fake.handle

    def slow(request):
        release.wait(5)
        return original(request)

    fake.handle = slow
    backend.upstream.close()
    backend.upstream = Mirror(backend, transport=httpx.MockTransport(slow),
                              base_url="https://upstream.test", backoff=0.01)
    started = time.monotonic()
    sid, auth, epoch = register(native(hub))
    post(native(hub), sid, auth, epoch, [{"type": "assistant", "uuid": "fast",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}}])
    assert time.monotonic() - started < 2
    release.set()
    wait_for(lambda: fake.calls("POST", "/worker/events"))


def test_mirror_is_configured_from_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(native_remote, "_backends", {})
    monkeypatch.setenv("CC_TMUX_NATIVE_TOKEN", TOKEN)
    monkeypatch.setenv("CC_TMUX_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("CC_TMUX_RC_UPSTREAM_PROXY", raising=False)
    assert get_backend().upstream is None
    monkeypatch.setattr(native_remote, "_backends", {})
    monkeypatch.setenv("CC_TMUX_RC_UPSTREAM_PROXY", "http://127.0.0.1:18790")
    monkeypatch.setenv("CC_TMUX_RC_UPSTREAM_ACCOUNT", "work")
    backend = get_backend()
    assert backend.upstream._launch == {"Authorization": "Bearer proxy-account-work"}
    backend.upstream.close()
    monkeypatch.setattr(native_remote, "_backends", {})
    monkeypatch.setenv("CC_TMUX_RC_UPSTREAM_ACCOUNT", "bad account")
    with pytest.raises(ValueError):
        get_backend()


def test_rejected_item_is_dropped_and_later_items_still_go(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    fake.fail("POST", "/worker/events", *[400] * 100)
    fake.fail("POST", "/worker/internal-events", 404)
    post(native(hub), sid, auth, epoch, [{"type": "assistant", "uuid": "refused",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}}])
    client.post(f"/v1/code/sessions/{sid}/worker/internal-events", headers=auth, json={
        "worker_epoch": epoch, "events": [{"payload": {"type": "transcript", "uuid": "i-1"}}]})
    client.put(f"/v1/code/sessions/{sid}/worker", headers=auth,
               json={"worker_epoch": epoch, "worker_status": "running"})
    wait_for(lambda: any(r["body"].get("worker_status") == "running"
                         for r in fake.calls("PUT", "/worker")))
    assert len(fake.calls("POST", "/worker/events")) == 1
    assert len(fake.calls("POST", "/worker/internal-events")) == 1
    assert backend.detail(sid)["upstream"]["status"] == "connected"
    client.post(f"/v1/code/sessions/{sid}/archive", headers=launch)
    wait_for(lambda: fake.calls("POST", "/archive"))


def test_404_on_the_worker_ends_mirroring(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    fake.fail("PUT", "/worker", 404)
    client.put(f"/v1/code/sessions/{sid}/worker", headers=auth,
               json={"worker_epoch": epoch, "worker_status": "running"})
    wait_for(lambda: backend.detail(sid)["upstream"]["status"] == "ended")
    assert backend.detail(sid)["lifecycle"] != "ended"


def test_refused_worker_token_rebridges_at_most_once_per_backoff(hub, capsys):
    backend, client, launch, fake = hub
    original = fake.handle

    def refuse(request):
        if "/worker" in request.url.path:
            return httpx.Response(401, json={"type": "error"})
        return original(request)

    backend.upstream.close()
    backend.upstream = Mirror(backend, transport=httpx.MockTransport(refuse),
                              base_url="https://upstream.test", backoff=0.1, max_backoff=0.1)
    register(native(hub))
    time.sleep(0.5)
    assert 1 <= fake.bridges <= 14
    assert "RecursionError" not in capsys.readouterr().out


def test_stream_skips_frames_that_are_not_client_events(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    fake.raw('event: session_update\ndata: {"connection_status": "connected"}\n\n')
    fake.raw('event: ephemeral_event\ndata: {"payload": {"type": "stream_event"}}\n\n')
    fake.raw('event: delivery_update\ndata: {"event_id": "x", "status": "processed"}\n\n')
    fake.emit(1, {"type": "control_request", "request_id": "init-1",
                  "request": {"subtype": "initialize"}})
    wait_for(lambda: backend.replay(sid, 0))
    assert len(fake.streams) == 1
    assert backend.detail(sid)["upstream"]["status"] == "connected"


def test_status_returns_to_connected_after_a_retried_send(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    fake.fail("POST", "/worker/events", 500)
    post(native(hub), sid, auth, epoch, [{"type": "assistant", "uuid": "retried",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]}}])
    wait_for(lambda: len(fake.calls("POST", "/worker/events")) == 2)
    wait_for(lambda: backend.detail(sid)["upstream"]["status"] == "connected")


def test_hq_answer_withdraws_the_prompt_upstream(hub):
    backend, client, launch, fake = hub
    sid, auth, epoch = mirrored(hub)
    ask = {"type": "control_request", "request_id": "perm-2",
           "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls"}}}
    post(native(hub), sid, auth, epoch, [ask])
    revision = connected(backend, sid)
    backend.act(sid, "respond", revision, "answer-1",
                response={"request_id": "perm-2", "behavior": "allow"})
    cancel = wait_for(lambda: [r for r in fake.calls("POST", "/worker/events")
                               if r["body"]["events"][0]["payload"]["type"]
                               == "control_cancel_request"])
    assert cancel[0]["body"]["events"] == [
        {"payload": {"type": "control_cancel_request", "request_id": "perm-2"}}]


def test_full_local_queue_replays_with_growing_backoff(hub, monkeypatch):
    backend, client, launch, fake = hub
    backend.upstream.close()
    backend.upstream = Mirror(backend, transport=httpx.MockTransport(fake.handle),
                              base_url="https://upstream.test", backoff=0.05, max_backoff=1)
    sid, auth, epoch = mirrored(hub)
    monkeypatch.setattr(native_remote, "MAX_EVENTS", 1)

    def user(text):
        return {"type": "user", "uuid": text,
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}

    fake.emit(1, user("one"))
    first = wait_for(lambda: backend.replay(sid, 0))[0]
    opened = len(fake.streams)
    fake.emit(2, user("two"))
    time.sleep(0.8)
    assert len(fake.streams) - opened <= 5
    client.post(f"/v1/code/sessions/{sid}/worker/events/delivery", headers=auth, json={
        "worker_epoch": epoch, "updates": [{"event_id": first["event_id"], "status": "processed"}]})
    wait_for(lambda: [e for e in backend.replay(sid, 0) if e["payload"]["uuid"] == "two"])


def test_mirror_traffic_routed_back_to_hq_is_refused(hub):
    backend, client, launch, fake = hub
    looped = client.post("/v1/code/sessions", headers={**launch, "x-hq-mirror": "1"},
                         json={"title": "Loop", "bridge": {}, "config": {"cwd": "/tmp/p"}})
    assert looped.status_code == 508
    assert backend.rows() == []
    time.sleep(0.1)
    assert fake.requests == []


def test_failed_first_state_put_is_retried_and_stream_still_opens(hub):
    backend, client, launch, fake = hub
    fake.fail("PUT", "/worker", 500)
    mirrored(hub)
    puts = wait_for(lambda: len(fake.calls("PUT", "/worker")) >= 2 and fake.calls("PUT", "/worker"))
    assert puts[1]["body"] == {"worker_epoch": 1, "worker_status": "idle", "external_metadata": {}}
