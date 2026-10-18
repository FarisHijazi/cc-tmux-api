"""Native RC v2 HTTP contract and guarded per-session action tests."""

import copy
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cc_tmux_api.fleet import FleetError
from cc_tmux_api.native_remote import MAX_EVENTS, NativeRemote, _frame, get_backend


def computer_use_request(request_id="computer-consent"):
    return {
        "type": "control_request",
        "request_id": request_id,
        "request": {
            "subtype": "request_user_dialog",
            "dialog_kind": "computer_use_approval",
            "payload": {
                "requestId": "app-consent",
                "reason": "Inspect the selected document",
                "apps": [
                    {
                        "requestedName": "Preview",
                        "resolved": {
                            "bundleId": "com.apple.Preview",
                            "displayName": "Preview",
                        },
                        "alreadyGranted": False,
                        "proposedTier": "read",
                    },
                    {
                        "requestedName": "Notes",
                        "resolved": {
                            "bundleId": "com.apple.Notes",
                            "displayName": "Notes",
                        },
                        "alreadyGranted": False,
                        "proposedTier": "click",
                    },
                    {"requestedName": "Missing app", "resolved": None},
                ],
                "requestedFlags": {"clipboardRead": True},
                "screenshotFiltering": "native",
            },
        },
    }


@pytest.fixture
def native(tmp_path):
    backend = NativeRemote(tmp_path, "test-" + "x" * 40)
    app = FastAPI()
    app.include_router(backend.router())
    with TestClient(app) as client:
        yield backend, client, {"x-hq-native-key": "test-" + "x" * 40}


def register(native, title="Test session"):
    backend, client, launch = native
    create = client.post(
        "/v1/code/sessions",
        json={
            "title": title,
            "bridge": {},
            "tags": ["remote-control-repl"],
            "config": {"cwd": "/tmp/project", "model": "test-model"},
        },
        headers=launch,
    )
    assert create.status_code == 200
    sid = create.json()["session"]["id"]
    reply = client.post(f"/v1/code/sessions/{sid}/bridge", json={}, headers=launch)
    assert reply.status_code == 200
    data = reply.json()
    assert data["api_base_url"] == "https://api.anthropic.com"
    return sid, {"Authorization": "Bearer " + data["worker_jwt"]}, data["worker_epoch"]


def post(native, sid, headers, epoch, payloads):
    return native[1].post(
        f"/v1/code/sessions/{sid}/worker/events",
        headers=headers,
        json={"worker_epoch": epoch, "events": [{"payload": p} for p in payloads]},
    )


def connected(backend, sid):
    # Unit tests model the SSE connection; the independent live test exercises wire streaming.
    backend._streams[sid] = {"epoch": backend._sessions[sid]["epoch"], "id": "test-lease"}
    backend._sessions[sid]["last_seen"] = time.time()
    return backend.detail(sid)["revision"]


def test_registration_auth_and_worker_session_isolation(native):
    backend, client, launch = native
    assert client.post("/v1/code/sessions", json={}).status_code == 401
    first, auth, epoch = register(native)
    second, _, _ = register(native)
    assert client.get(f"/v1/code/sessions/{first}/worker", headers=launch).status_code == 401
    assert client.get(f"/v1/code/sessions/{second}/worker", headers=auth).status_code == 401
    assert (
        client.get(f"/v1/code/sessions/{first}/worker", headers=auth).json()["worker_epoch"]
        == epoch
    )
    disk = backend._path.read_text()
    assert auth["Authorization"][7:] not in disk
    assert launch["x-hq-native-key"] not in disk
    assert backend._path.stat().st_mode & 0o777 == 0o600


def test_epoch_rotation_rejects_old_workers_and_stale_refresh(native):
    _, client, launch = native
    sid, auth, epoch = register(native)
    path = f"/v1/code/sessions/{sid}"
    assert (
        client.post(
            path + "/bridge", headers=launch, json={"expected_epoch": epoch + 1}
        ).status_code
        == 409
    )
    refreshed = client.post(path + "/bridge", headers=launch, json={"expected_epoch": epoch}).json()
    assert refreshed["worker_epoch"] == epoch + 1
    assert client.get(path + "/worker", headers=auth).status_code == 401
    new_auth = {"Authorization": "Bearer " + refreshed["worker_jwt"]}
    assert post(native, sid, new_auth, epoch, []).status_code == 409
    assert post(native, sid, new_auth, epoch + 1, []).status_code == 200


def test_messages_deduplicate_and_exclude_injected_user_records(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    message = {
        "type": "assistant",
        "uuid": "reply-1",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
    }
    notice = {
        "type": "user",
        "uuid": "notice",
        "isMeta": True,
        "message": {"content": "Invisible system notice"},
    }
    assert post(native, sid, auth, epoch, [message, notice]).status_code == 200
    result = post(native, sid, auth, epoch, [message])
    assert result.json()["results"][0]["duplicate"] is True
    assert [m["text"] for m in backend.detail(sid)["messages"]] == ["Hello"]


def test_send_replay_isolation_and_interrupt(native):
    backend, _, _ = native
    sid, _, _ = register(native)
    other, _, _ = register(native)
    revision = connected(backend, sid)
    receipt = backend.act(sid, "send", revision, "receipt-1", text="Hello native")
    assert receipt["delivery"] == "queued"
    assert backend.replay(other, 0) == []
    events = backend.replay(sid, 0)
    assert events[0]["payload"]["message"]["content"][0]["text"] == "Hello native"
    assert events[0]["payload"]["client_platform"] == "web_claude_ai"
    assert "inbound_origin" not in events[0]["payload"]
    assert backend.replay(sid, receipt["sequence_num"]) == []
    wire = _frame(events[0])
    assert wire.startswith("event: client_event\nid: 1\ndata: ")
    assert json.loads(wire.split("data: ", 1)[1])["sequence_num"] == 1
    with pytest.raises(FleetError, match="changed"):
        backend.act(sid, "send", revision, "receipt-2", text="Stale")
    row = backend.detail(sid)
    backend.act(sid, "interrupt", row["revision"], "interrupt-1")
    assert backend.replay(sid, 1)[0]["payload"]["request"] == {"subtype": "interrupt"}


def test_permission_response_matches_exact_pending_request(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    request = {
        "type": "control_request",
        "request_id": "permission-1",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "Bash",
            "input": {"command": "echo test"},
        },
    }
    post(native, sid, auth, epoch, [request])
    revision = connected(backend, sid)
    assert backend.detail(sid)["native_control"]["tool_name"] == "Bash"
    with pytest.raises(FleetError, match="stale"):
        backend.act(
            sid,
            "respond",
            revision,
            "action-1",
            response={"request_id": "wrong", "behavior": "allow"},
        )
    with pytest.raises(FleetError, match="pending"):
        backend.act(sid, "send", revision, "action-2", text="skip approval")
    backend.act(
        sid,
        "respond",
        revision,
        "action-3",
        response={"request_id": "permission-1", "behavior": "allow"},
    )
    reply = backend.replay(sid, 0)[0]["payload"]["response"]
    assert reply == {
        "request_id": "permission-1",
        "subtype": "success",
        "response": {"behavior": "allow", "updatedInput": {"command": "echo test"}},
    }
    assert backend.detail(sid)["native_control"] is None
    with pytest.raises(FleetError, match="stale"):
        backend.act(
            sid,
            "respond",
            backend.detail(sid)["revision"],
            "action-4",
            response={"request_id": "permission-1", "behavior": "allow"},
        )


def test_disconnect_is_unreachable_and_only_archive_ends(native):
    backend, client, launch = native
    sid, auth, epoch = register(native)
    assert backend.detail(sid)["lifecycle"] == "unreachable"
    connected(backend, sid)
    assert backend.detail(sid)["lifecycle"] == "alive"
    backend._sessions[sid]["last_seen"] -= 100
    assert backend.detail(sid)["lifecycle"] == "unreachable"
    result = client.post(
        f"/v1/code/sessions/{sid}/worker/heartbeat", headers=auth, json={"worker_epoch": epoch}
    )
    assert result.status_code == 200
    assert backend.detail(sid)["lifecycle"] == "alive"
    backend._streams.pop(sid, None)
    with pytest.raises(FleetError, match="not connected"):
        backend.act(sid, "send", "1", "offline", text="hello")
    client.post(f"/v1/code/sessions/{sid}/archive", headers=launch, json={})
    assert backend.detail(sid)["lifecycle"] == "ended"
    assert client.get(f"/v1/code/sessions/{sid}/worker", headers=auth).status_code == 410


def test_restart_preserves_transport_but_never_assumes_connection(native):
    backend, _, _ = native
    sid, _, _ = register(native)
    backend.act(sid, "send", connected(backend, sid), "send", text="Persisted")
    restarted = NativeRemote(backend._path.parent, "test-" + "x" * 40)
    assert restarted.detail(sid)["lifecycle"] == "unreachable"
    assert restarted.detail(sid)["messages"][0]["text"] == "Persisted"
    assert restarted.replay(sid, 0)[0]["payload"]["type"] == "user"


def test_replay_window_and_malformed_cursor_are_explicit(native):
    backend, client, _ = native
    sid, auth, _ = register(native)
    for n in range(MAX_EVENTS):
        backend._queue(backend._sessions[sid], {"type": "user", "text": str(n)})
    with pytest.raises(FleetError, match="queue is full"):
        backend._queue(backend._sessions[sid], {"type": "user", "text": "overflow"})
    # A retained-journal gap is explicitly rejected rather than silently losing commands.
    backend._sessions[sid]["inbound"].pop(0)
    path = f"/v1/code/sessions/{sid}/worker/events/stream"
    assert client.get(path + "?from_sequence_num=0", headers=auth).status_code == 409
    assert client.get(path + "?from_sequence_num=-1", headers=auth).status_code == 400
    assert len(backend.replay(sid, 1)) == MAX_EVENTS - 1


def test_disabled_by_default_and_rejects_weak_secret(monkeypatch):
    monkeypatch.delenv("CC_TMUX_NATIVE_TOKEN", raising=False)
    assert get_backend() is None
    monkeypatch.setenv("CC_TMUX_NATIVE_TOKEN", "weak")
    with pytest.raises(ValueError, match="32"):
        get_backend()


def test_pending_requests_queue_and_notify_then_advance(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    requests = [
        {
            "type": "control_request",
            "request_id": f"permission-{i}",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": f"echo {i}"},
            },
        }
        for i in range(2)
    ]
    post(native, sid, auth, epoch, requests)
    connected(backend, sid)
    row = backend.detail(sid)
    assert row["event_id"] == "permission-0"
    assert not row["has_question"] and row["notification_eligible"]
    assert row["attention_kind"] == "permission"
    assert "messages" not in backend.rows()[0]
    backend.act(
        sid,
        "respond",
        row["revision"],
        "approve",
        response={
            "request_id": "permission-0",
            "behavior": "allow",
            "updated_input": {"command": "echo safe"},
        },
    )
    assert backend.replay(sid, 0)[0]["payload"]["response"]["response"]["updatedInput"] == {
        "command": "echo safe"
    }
    assert backend.detail(sid)["native_control"]["request_id"] == "permission-1"
    post(
        native, sid, auth, epoch, [{"type": "control_cancel_request", "request_id": "permission-1"}]
    )
    assert not backend.detail(sid)["has_question"]


def test_delivery_acks_suppress_replay_even_without_cursor(native):
    backend, client, _ = native
    sid, auth, epoch = register(native)
    for i in range(3):
        backend.act(sid, "send", connected(backend, sid), f"send-{i}", text=str(i))
    events = backend.replay(sid, 0)
    path = f"/v1/code/sessions/{sid}/worker/events/delivery"
    body = {
        "worker_epoch": epoch,
        "updates": [{"event_id": events[1]["event_id"], "status": "received"}],
    }
    assert client.post(path, headers=auth, json=body).status_code == 200
    assert [e["sequence_num"] for e in backend.replay(sid, 0)] == [1, 2, 3]
    body["updates"] = [{"event_id": e["event_id"], "status": "processed"} for e in events[:2]]
    client.post(path, headers=auth, json=body)
    assert [e["sequence_num"] for e in backend.replay(sid, 0)] == [3]
    body["updates"] = [{"event_id": events[1]["event_id"], "status": "received"}]
    client.post(path, headers=auth, json=body)
    assert backend._sessions[sid]["acknowledged"][events[1]["event_id"]] == "processed"
    restarted = NativeRemote(backend._path.parent, "test-" + "x" * 40)
    assert [e["sequence_num"] for e in restarted.replay(sid, 0)] == [3]


@pytest.mark.parametrize("malformed", [None, []])
def test_bad_control_batch_is_rejected_before_any_mutation(native, malformed):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    before = backend.detail(sid)
    result = post(
        native,
        sid,
        auth,
        epoch,
        [
            {"type": "assistant", "message": {"content": "must not be accepted"}},
            {"type": "control_request", "request_id": "bad", "request": malformed},
        ],
    )
    assert result.status_code == 400
    assert backend.detail(sid) == before


def test_bridge_rotation_immediately_invalidates_old_stream_and_revision(native):
    backend, client, launch = native
    sid, _, epoch = register(native)
    revision = connected(backend, sid)
    client.post(f"/v1/code/sessions/{sid}/bridge", headers=launch, json={"expected_epoch": epoch})
    assert backend.detail(sid)["lifecycle"] == "unreachable"
    new_revision = connected(backend, sid)
    assert new_revision != revision
    with pytest.raises(FleetError, match="changed"):
        backend.act(sid, "send", revision, "old-epoch", text="stale")


def test_sse_replacement_closes_old_lease_without_closing_new(native):
    import asyncio

    from starlette.requests import Request

    backend, _, _ = native
    sid, auth, _ = register(native)
    endpoint = next(
        route.endpoint
        for route in backend.router().routes
        if route.path.endswith("/worker/events/stream")
    )

    async def exercise():
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [(b"authorization", auth["Authorization"].encode())],
        }
        first = await endpoint(sid, Request(scope))
        assert await anext(first.body_iterator) == ": connected\n\n"
        second = await endpoint(sid, Request(scope))
        assert await anext(second.body_iterator) == ": connected\n\n"
        with pytest.raises(StopAsyncIteration):
            await anext(first.body_iterator)
        assert backend.detail(sid)["lifecycle"] == "alive"
        await second.body_iterator.aclose()
        assert backend.detail(sid)["lifecycle"] == "unreachable"

    asyncio.run(exercise())


def test_heartbeat_touches_liveness_without_rewriting_journal(native, monkeypatch):
    backend, client, _ = native
    sid, auth, epoch = register(native)
    before = backend._sessions[sid]["last_seen"]

    def forbidden():
        raise AssertionError("heartbeat must not rewrite transcript journal")

    monkeypatch.setattr(backend, "_save", forbidden)
    response = client.post(
        f"/v1/code/sessions/{sid}/worker/heartbeat", headers=auth, json={"worker_epoch": epoch}
    )
    assert response.status_code == 200
    assert backend._sessions[sid]["last_seen"] >= before


def test_oversized_permission_response_does_not_consume_request(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    post(
        native,
        sid,
        auth,
        epoch,
        [
            {
                "type": "control_request",
                "request_id": "permission",
                "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}},
            }
        ],
    )
    revision = connected(backend, sid)
    with pytest.raises(FleetError, match="256 KiB"):
        backend.act(
            sid,
            "respond",
            revision,
            "oversized",
            response={
                "request_id": "permission",
                "behavior": "allow",
                "updated_input": {"large": "x" * 300000},
            },
        )
    assert backend.detail(sid)["native_control"]["request_id"] == "permission"
    assert backend.replay(sid, 0) == []


def test_archived_transport_records_expire_but_unreachable_records_do_not(native):
    from cc_tmux_api.session_history import RETENTION_SECONDS

    backend, client, launch = native
    archived, _, _ = register(native)
    retained, _, _ = register(native)
    client.post(f"/v1/code/sessions/{archived}/archive", headers=launch, json={})
    backend._sessions[archived]["archived_at"] -= RETENTION_SECONDS + 1
    backend._sessions[retained]["last_seen"] -= RETENTION_SECONDS + 1
    assert [row["id"] for row in backend.rows()] == [retained]
    assert archived not in json.loads(backend._path.read_text())["sessions"]


def test_local_native_approval_dismisses_web_pending_prompt(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    post(
        native,
        sid,
        auth,
        epoch,
        [
            {
                "type": "control_request",
                "request_id": "permission",
                "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}},
            }
        ],
    )
    connected(backend, sid)
    assert backend.detail(sid)["native_control"]
    post(
        native,
        sid,
        auth,
        epoch,
        [
            {
                "type": "control_response",
                "response": {
                    "request_id": "permission",
                    "subtype": "success",
                    "response": {"behavior": "allow"},
                },
            }
        ],
    )
    assert backend.detail(sid)["native_control"] is None


def test_received_and_processing_commands_replay_after_restart(native):
    backend, client, _ = native
    sid, auth, epoch = register(native)
    for i in range(2):
        backend.act(sid, "send", connected(backend, sid), f"pending-{i}", text=str(i))
    events = backend.replay(sid, 0)
    client.post(
        f"/v1/code/sessions/{sid}/worker/events/delivery",
        headers=auth,
        json={
            "worker_epoch": epoch,
            "updates": [
                {"event_id": events[0]["event_id"], "status": "received"},
                {"event_id": events[1]["event_id"], "status": "processing"},
            ],
        },
    )
    restarted = NativeRemote(backend._path.parent, "test-" + "x" * 40)
    assert [e["sequence_num"] for e in restarted.replay(sid, 0)] == [1, 2]
    assert backend._sessions[sid]["delivered_sequence"] == 0


def test_ask_user_question_has_question_attention(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    post(
        native,
        sid,
        auth,
        epoch,
        [
            {
                "type": "control_request",
                "request_id": "question",
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "AskUserQuestion",
                    "input": {"questions": []},
                },
            }
        ],
    )
    connected(backend, sid)
    row = backend.detail(sid)
    assert row["has_question"]
    assert row["attention_kind"] == "question"


def test_native_ten_mib_event_batch_fits_with_protocol_framing(native):
    _, _, _ = native
    sid, auth, epoch = register(native)
    result = post(
        native,
        sid,
        auth,
        epoch,
        [
            {
                "type": "user",
                "uuid": "large-tool-result",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "x" * (10 * 1024 * 1024),
                        }
                    ],
                },
            }
        ],
    )
    assert result.status_code == 200


def test_retried_uuidless_permission_event_cannot_resurrect_answered_prompt(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    event = {
        "type": "control_request",
        "request_id": "permission-no-uuid",
        "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}},
    }
    assert post(native, sid, auth, epoch, [event]).status_code == 200
    backend.act(
        sid,
        "respond",
        connected(backend, sid),
        "answer",
        response={"request_id": "permission-no-uuid", "behavior": "deny"},
    )
    before = backend.detail(sid)
    retry = post(native, sid, auth, epoch, [event])
    assert retry.status_code == 200
    assert retry.json()["results"][0]["duplicate"] is True
    assert backend.detail(sid)["native_control"] is None
    assert backend.detail(sid)["revision"] == before["revision"]
    restarted = NativeRemote(backend._path.parent, "test-" + "x" * 40)
    assert "control_request:permission-no-uuid" in restarted._sessions[sid]["seen_events"]


def test_dialog_capability_is_explicit_and_declared_once_per_epoch(native):
    backend, client, launch = native
    sid, _, _ = register(native)
    connected(backend, sid)
    assert backend.replay(sid, 0) == []
    backend.enable_computer_use_dialogs()
    request = backend.replay(sid, 0)[0]["payload"]["request"]
    assert request == {"subtype": "initialize", "supportedDialogKinds": ["computer_use_approval"]}
    backend.enable_computer_use_dialogs()
    assert len(backend.replay(sid, 0)) == 1
    restarted = NativeRemote(backend._path.parent, "test-" + "x" * 40)
    connected(restarted, sid)
    restarted.enable_computer_use_dialogs()
    assert len(restarted.replay(sid, 0)) == 1
    assert (
        client.post(f"/v1/code/sessions/{sid}/bridge", json={}, headers=launch).status_code == 200
    )
    connected(backend, sid)
    backend.enable_computer_use_dialogs()
    assert len(backend.replay(sid, 0)) == 2


def test_computer_use_explicit_selection_preserves_scope_and_survives_restart(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    event = computer_use_request()
    assert post(native, sid, auth, epoch, [event]).status_code == 200
    connected(backend, sid)
    pending = backend.detail(sid)["native_control"]
    assert pending["dialog_kind"] == "computer_use_approval"
    assert pending["input"] == event["request"]["payload"]
    assert backend.detail(sid)["has_question"] is False
    assert backend.replay(sid, 0) == []  # Receiving a consent request grants nothing.
    restarted = NativeRemote(backend._path.parent, "test-" + "x" * 40)
    restarted.act(
        sid,
        "respond",
        connected(restarted, sid),
        "ui-response",
        response={
            "request_id": event["request_id"],
            "behavior": "completed",
            "granted_app_ids": ["com.apple.Preview"],
            "flags": {"clipboardRead": True},
        },
    )
    wire = restarted.replay(sid, 0)[0]["payload"]["response"]
    assert wire["request_id"] == event["request_id"]
    assert wire["subtype"] == "success"
    result = wire["response"]["result"]
    assert wire["response"]["behavior"] == "completed"
    grant = result["granted"][0]
    assert grant["bundleId"] == "com.apple.Preview"
    assert grant["displayName"] == "Preview"
    assert grant["tier"] == "read"
    assert abs(grant["grantedAt"] - time.time() * 1000) < 2000
    assert result["denied"] == [
        {"bundleId": "com.apple.Notes", "reason": "user_denied"},
        {"bundleId": "Missing app", "reason": "not_installed"},
    ]
    assert result["flags"] == {
        "clipboardRead": True,
        "clipboardWrite": False,
        "systemKeyCombos": False,
    }
    assert restarted.detail(sid)["native_control"] is None


@pytest.mark.parametrize(
    "selection",
    [
        {"granted_app_ids": ["com.apple.Terminal"]},
        {"granted_app_ids": ["com.apple.Preview", "com.apple.Preview"]},
        {"granted_app_ids": [None]},
        {"flags": {"clipboardWrite": True}},
        {"flags": {"clipboardRead": "true"}},
        {"flags": {"unrequested": False}},
        {"behavior": "allow"},
    ],
)
def test_computer_use_rejects_invalid_or_broadened_consent_without_mutation(native, selection):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    event = computer_use_request()
    assert post(native, sid, auth, epoch, [event]).status_code == 200
    revision = connected(backend, sid)
    before = copy.deepcopy(backend._sessions[sid])
    with pytest.raises(FleetError):
        backend.act(
            sid,
            "respond",
            revision,
            "ui-response",
            response={
                "request_id": event["request_id"],
                "behavior": "completed",
                **selection,
            },
        )
    assert backend._sessions[sid] == before


def test_computer_use_cancel_and_native_cancel_advance_queue(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    assert (
        post(
            native,
            sid,
            auth,
            epoch,
            [computer_use_request("first"), computer_use_request("second")],
        ).status_code
        == 200
    )
    backend.act(
        sid,
        "respond",
        connected(backend, sid),
        "ui-cancel",
        response={
            "request_id": "first",
            "behavior": "cancelled",
        },
    )
    assert backend.replay(sid, 0)[0]["payload"]["response"]["response"] == {"behavior": "cancelled"}
    assert backend.detail(sid)["native_control"]["request_id"] == "second"
    assert (
        post(
            native, sid, auth, epoch, [{"type": "control_cancel_request", "request_id": "second"}]
        ).status_code
        == 200
    )
    assert backend.detail(sid)["native_control"] is None
    with pytest.raises(FleetError, match="stale"):
        backend.act(
            sid,
            "respond",
            backend.detail(sid)["revision"],
            "late",
            response={
                "request_id": "second",
                "behavior": "completed",
            },
        )


def test_computer_use_os_permission_retry_cannot_grant_apps(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    event = computer_use_request()
    event["request"]["payload"] = {
        "apps": [],
        "requestedFlags": {},
        "tccState": {"accessibility": False, "screenRecording": True},
    }
    assert post(native, sid, auth, epoch, [event]).status_code == 200
    backend.act(
        sid,
        "respond",
        connected(backend, sid),
        "retry",
        response={
            "request_id": event["request_id"],
            "behavior": "completed",
        },
    )
    result = backend.replay(sid, 0)[0]["payload"]["response"]["response"]["result"]
    assert result == {
        "granted": [],
        "denied": [],
        "flags": {
            "clipboardRead": False,
            "clipboardWrite": False,
            "systemKeyCombos": False,
        },
    }


@pytest.mark.parametrize(
    "change",
    [
        {"apps": [None]},
        {"apps": [{"requestedName": "Invalid", "resolved": []}]},
        {"requestedFlags": []},
        {"tccState": {}},
    ],
)
def test_malformed_dialog_batch_rejected_atomically(native, change):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    event = computer_use_request()
    event["request"]["payload"].update(change)
    before = copy.deepcopy(backend._sessions[sid])
    assert post(native, sid, auth, epoch, [computer_use_request("valid"), event]).status_code == 400
    assert backend._sessions[sid] == before


def test_unknown_native_dialog_is_not_answered_or_exposed(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    event = computer_use_request()
    event["request"]["dialog_kind"] = "future_unsupported_dialog"
    assert post(native, sid, auth, epoch, [event]).status_code == 200
    connected(backend, sid)
    assert backend.detail(sid)["native_control"] is None
    assert backend.replay(sid, 0) == []


def test_initialize_reply_restores_pending_dialog_only_for_our_handshake(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    connected(backend, sid)
    backend.enable_computer_use_dialogs()
    initialize = backend.replay(sid, 0)[0]["payload"]
    response = {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "unrelated",
            "response": {},
            "pending_user_dialog_requests": [computer_use_request()],
        },
    }
    assert post(native, sid, auth, epoch, [response]).status_code == 200
    assert backend.detail(sid)["native_control"] is None
    response["response"]["request_id"] = initialize["request_id"]
    assert post(native, sid, auth, epoch, [response]).status_code == 200
    assert backend.detail(sid)["native_control"]["request_id"] == "computer-consent"
    backend.act(
        sid,
        "respond",
        backend.detail(sid)["revision"],
        "cancel",
        response={
            "request_id": "computer-consent",
            "behavior": "cancelled",
        },
    )
    # A retried HTTP batch containing the handshake cannot revive an answered dialog.
    assert post(native, sid, auth, epoch, [response]).status_code == 200
    assert backend.detail(sid)["native_control"] is None


def test_malformed_initialize_redelivery_rejected_atomically(native):
    backend, _, _ = native
    sid, auth, epoch = register(native)
    before = copy.deepcopy(backend._sessions[sid])
    response = {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": "initialization",
            "pending_user_dialog_requests": [{"type": "control_request", "request": None}],
        },
    }
    assert post(native, sid, auth, epoch, [computer_use_request(), response]).status_code == 400
    assert backend._sessions[sid] == before
