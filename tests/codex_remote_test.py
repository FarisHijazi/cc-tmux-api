"""Native owner protocol, identity, approval and reconnect guarantees."""

import copy
import json
import queue
import threading
import time
from uuid import uuid4

import pytest

from cc_tmux_api import codex_remote as codex
from cc_tmux_api.fleet import FleetError

SID = "01900123-4567-789a-bcde-0123456789ab"


class OwnerSocket:
    def __init__(self, owner):
        self.owner = owner
        self.queue = queue.Queue()
        self.closed = False

    def __iter__(self):
        while True:
            value = self.queue.get()
            if value is None:
                return
            yield json.dumps(value)

    def send(self, value):
        if self.closed:
            raise OSError("closed")
        request = json.loads(value)
        self.owner.calls.append(request)
        if "method" not in request or "id" not in request:
            return
        if self.owner.timeout_method == request["method"]:
            return
        if self.owner.error_method == request["method"]:
            self.queue.put({"id": request["id"], "error": {"message": "private secret path"}})
            return
        result = self.owner.handle(request["method"], request["params"])
        self.queue.put({"id": request["id"], "result": result})

    def close(self):
        self.closed = True
        self.queue.put(None)


@pytest.fixture
def owner():
    class Owner:
        def __init__(self):
            self.calls, self.sockets = [], []
            self.ids = [SID]
            self.state = "idle"
            self.direct = True
            self.flags = []
            self.turn = {"id": "native-turn", "status": "completed"}
            self.items = [
                {"turnId": "native-turn", "item": {"id": "user-1", "type": "userMessage",
                 "clientId": "original-client", "content": [{"type": "text", "text": "Question"}]}},
                {"turnId": "native-turn", "item": {"id": "assistant-1", "type": "agentMessage",
                 "text": "Answer", "phase": "final_answer"}},
            ]
            self.timeout_method = self.error_method = None

        def connect(self, path, **kwargs):
            assert path == "/existing/control.sock"
            value = OwnerSocket(self)
            self.sockets.append(value)
            return value

        def emit(self, message):
            self.sockets[-1].queue.put(copy.deepcopy(message))

        def handle(self, method, params):
            if method == "initialize":
                return {"userAgent": "codex-test"}
            if method == "thread/loaded/list":
                offset = int(params.get("cursor", "0"))
                end = offset + params["limit"]
                return {"data": self.ids[offset:end], "nextCursor": str(end) if end < len(self.ids) else None}
            if method == "thread/read":
                return {"thread": {"id": params["threadId"], "cwd": "/work/project", "name": "Existing",
                        "status": {"type": self.state, "activeFlags": self.flags},
                        "canAcceptDirectInput": self.direct, "updatedAt": 1}}
            if method == "thread/resume":
                assert params == {"threadId": SID, "excludeTurns": True}
                return {}
            if method == "thread/turns/list":
                return {"data": [copy.deepcopy(self.turn)]}
            if method == "thread/items/list":
                offset = int(params.get("cursor", "0"))
                end = offset + params["limit"]
                return {"data": list(reversed(copy.deepcopy(self.items)))[offset:end],
                        "nextCursor": str(end) if end < len(self.items) else None}
            if method == "turn/start":
                return {"turn": {"id": "accepted-turn"}}
            if method == "turn/steer":
                return {"turnId": self.turn["id"]}
            if method == "turn/interrupt":
                return {}
            pytest.fail(f"Unexpected native method {method}")

    value = Owner()
    rpc = codex.AppServer("/existing/control.sock", connect=value.connect, timeout=0.15)
    value.backend = codex.CodexRemote("/existing/control.sock", rpc=rpc)
    yield value
    rpc.close()


def until(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    pytest.fail("Native event did not arrive")


def pending(owner, method="item/commandExecution/requestApproval", **params):
    owner.backend.detail(SID)
    owner.emit({"id": 42, "method": method, "params": {
        "threadId": SID, "turnId": "native-turn", "itemId": "native-item",
        "command": "echo toy", **params,
    }})
    until(lambda: owner.backend.rpc.pending(SID))
    return owner.backend.detail(SID)


def test_loaded_discovery_and_history_keep_native_identity_and_never_start(owner):
    row = owner.backend.rows()[0]
    detail = owner.backend.detail(SID)
    assert row["id"] == detail["codex_thread_id"] == SID
    assert detail["messages"][0]["client_id"] == "original-client"
    assert [m["id"] for m in detail["messages"]] == ["user-1", "assistant-1"]
    assert all(r.get("method") not in {"thread/start", "thread/fork", "turn/start"} for r in owner.calls)
    assert len(owner.sockets) == 1


def test_discovery_walks_more_than_one_hundred_loaded_threads(owner):
    owner.ids = [str(uuid4()) for _ in range(237)]
    assert {r["id"] for r in owner.backend.rows()} == set(owner.ids)


def test_unloaded_thread_and_forged_identity_never_resume_or_write(owner):
    owner.ids = []
    with pytest.raises(FleetError, match="not loaded"):
        owner.backend.detail(SID)
    with pytest.raises(FleetError, match="Invalid Codex"):
        owner.backend.detail("../../other")
    assert all(r.get("method") != "thread/resume" for r in owner.calls)


def test_send_uses_original_native_owner_without_configuration_overrides(owner):
    row = owner.backend.detail(SID)
    result = owner.backend.act(SID, "send", row["revision"], "app-request", text="Continue")
    assert result["client_id"] == "app-request" and result["turn_id"] == "accepted-turn"
    assert owner.calls[-1]["params"] == {"threadId": SID, "clientUserMessageId": "app-request",
                                        "input": [{"type": "text", "text": "Continue"}]}


def test_active_send_steers_exact_turn_and_stop_targets_that_same_turn(owner):
    owner.state, owner.turn["status"] = "active", "inProgress"
    row = owner.backend.detail(SID)
    owner.backend.act(SID, "send", row["revision"], "steer", text="Continue")
    assert owner.calls[-1]["method"] == "turn/steer"
    assert owner.calls[-1]["params"]["expectedTurnId"] == "native-turn"
    owner.backend.act(SID, "interrupt", row["revision"], "stop")
    assert owner.calls[-1]["params"] == {"threadId": SID, "turnId": "native-turn"}


@pytest.mark.parametrize("change", ["turn", "input", "connection"])
def test_stale_input_turn_and_connection_revisions_reject_before_write(owner, change):
    row = owner.backend.detail(SID)
    if change == "turn":
        owner.turn["id"] = "new-turn"
    elif change == "input":
        owner.emit({"method": "item/started", "params": {"threadId": SID,
                    "item": {"type": "userMessage", "id": "terminal-user"}}})
        until(lambda: owner.backend.rpc._inputs.get(SID))
    else:
        owner.backend.rpc.close()
    with pytest.raises(FleetError, match="refresh before acting"):
        owner.backend.act(SID, "send", row["revision"], "stale", text="Continue")
    assert all(r.get("method") not in {"turn/start", "turn/steer"} for r in owner.calls)


def test_output_streaming_does_not_invalidate_input_revision(owner):
    row = owner.backend.detail(SID)
    owner.items[-1]["item"]["text"] += " more"
    updated = owner.backend.detail(SID)
    assert updated["revision"] == row["revision"]
    assert updated["messages"][-1]["text"] == "Answer more"


def test_native_history_replays_equal_text_as_distinct_ids_without_duplicates(owner):
    owner.backend.detail(SID)
    owner.items += [{"turnId": "next", "item": {"id": "user-2", "type": "userMessage",
                    "clientId": "app-next", "content": [{"type": "text", "text": "Question"}]}}]
    for _ in range(2):
        row = owner.backend.detail(SID)
        assert [m["id"] for m in row["messages"]] == ["user-1", "assistant-1", "user-2"]
        assert row["messages"][-1]["client_id"] == "app-next"
        assert not row["history_truncated"]


def test_native_rollback_removes_cached_messages_past_the_surviving_anchor(owner):
    owner.backend.detail(SID)
    owner.items = owner.items[:1]
    owner.items.append({"turnId": "replacement", "item": {
        "id": "replacement-answer", "type": "agentMessage", "text": "New answer"}})
    row = owner.backend.detail(SID)
    assert [m["id"] for m in row["messages"]] == ["user-1", "replacement-answer"]


def test_stream_growth_before_a_steered_user_message_refreshes_the_native_item(owner):
    owner.backend.detail(SID)
    owner.items += [{"turnId": "native-turn", "item": {"id": "user-2", "type": "userMessage",
                    "content": [{"type": "text", "text": "Steer"}]}}]
    owner.backend.detail(SID)
    owner.items[1]["item"]["text"] = "Answer continued after steering"
    assert owner.backend.detail(SID)["messages"][1]["text"] == "Answer continued after steering"


def test_previous_live_turn_finishes_behind_more_than_one_page_of_new_items(owner):
    owner.state, owner.turn["status"] = "active", "inProgress"
    owner.backend.detail(SID)
    owner.items[1]["item"]["text"] = "Finished previous turn"
    owner.items += [{"turnId": "new-turn", "item": {"id": f"tool-{i}", "type": "commandExecution"}}
                    for i in range(130)]
    owner.items += [{"turnId": "new-turn", "item": {"id": "user-2", "type": "userMessage",
                    "content": [{"type": "text", "text": "New turn"}]}}]
    owner.turn["id"] = "new-turn"
    row = owner.backend.detail(SID)
    assert row["messages"][1]["text"] == "Finished previous turn"
    assert row["messages"][-1]["id"] == "user-2"


@pytest.mark.parametrize("behavior,decision", [("allow", "accept"), ("deny", "decline")])
def test_permission_is_explicit_one_time_exact_request_and_resolved_by_owner(owner, behavior, decision):
    row = pending(owner, availableDecisions=["accept", "decline"])
    assert row["status"] == "waiting" and not row["capabilities"]["send"]
    owner.backend.act(SID, "respond", row["revision"], "answer", response={
        "request_id": row["native_control"]["request_id"], "behavior": behavior})
    assert owner.calls[-1] == {"id": 42, "result": {"decision": decision}}
    assert not owner.backend.detail(SID)["capabilities"]["respond"]
    owner.emit({"method": "serverRequest/resolved", "params": {"threadId": SID, "requestId": 42}})
    until(lambda: not owner.backend.rpc.pending(SID))
    assert owner.backend.detail(SID)["native_control"] is None


def test_terminal_answered_permission_cannot_be_answered_again(owner):
    row = pending(owner)
    owner.emit({"method": "serverRequest/resolved", "params": {"threadId": SID, "requestId": 42}})
    until(lambda: not owner.backend.rpc.pending(SID))
    with pytest.raises(FleetError, match="refresh before acting"):
        owner.backend.act(SID, "respond", row["revision"], "late", response={
            "request_id": row["native_control"]["request_id"], "behavior": "allow"})
    assert not any("result" in call for call in owner.calls)


def test_question_preserves_native_ids_and_requires_all_answers(owner):
    row = pending(owner, "item/tool/requestUserInput", questions=[
        {"id": "native-choice", "question": "Which one?", "header": "Choice", "options": None}])
    response = {"request_id": row["native_control"]["request_id"], "behavior": "allow",
                "updated_input": {"answers": {"Which one?": "chosen"}}}
    owner.backend.act(SID, "respond", row["revision"], "question", response=response)
    assert owner.calls[-1] == {"id": 42, "result": {"answers": {"native-choice": {"answers": ["chosen"]}}}}


def test_unknown_dialog_is_visible_but_never_silently_approved(owner):
    row = pending(owner, "future/approval")
    assert row["native_control"]["subtype"] == "request_user_dialog"
    assert not row["capabilities"]["respond"] and not row["capabilities"]["send"]
    assert not any("result" in call for call in owner.calls)


@pytest.mark.parametrize("method", ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"])
def test_session_wide_approval_is_not_implicitly_granted(owner, method):
    row = pending(owner, method)
    with pytest.raises(FleetError, match="one-time"):
        owner.backend.act(SID, "respond", row["revision"], "broad", response={
            "request_id": row["native_control"]["request_id"], "behavior": "allow", "always": True})
    assert not any("result" in call for call in owner.calls)


@pytest.mark.parametrize("behavior", ["allow", "deny"])
def test_permissions_only_grant_the_exact_request_for_this_turn(owner, behavior):
    permissions = {"fileSystem": {"write": ["/work/project/toy"]}}
    row = pending(owner, "item/permissions/requestApproval", permissions=permissions)
    owner.backend.act(SID, "respond", row["revision"], "permission", response={
        "request_id": row["native_control"]["request_id"], "behavior": behavior,
        "updated_input": {"permissions": {"fileSystem": {"write": ["/"]}}}})
    assert owner.calls[-1]["result"] == {
        "scope": "turn", "permissions": permissions if behavior == "allow" else {}}


def test_question_rejects_missing_answers_and_supports_explicit_decline(owner):
    row = pending(owner, "item/tool/requestUserInput", questions=[
        {"id": "native-choice", "question": "Which one?", "header": "Choice"}])
    response = {"request_id": row["native_control"]["request_id"], "behavior": "allow",
                "updated_input": {"answers": {}}}
    with pytest.raises(FleetError, match="Every question"):
        owner.backend.act(SID, "respond", row["revision"], "missing", response=response)
    response["behavior"] = "deny"
    owner.backend.act(SID, "respond", row["revision"], "decline", response=response)
    assert owner.calls[-1]["result"] == {"answers": {}}


def test_bounded_history_paginates_and_keeps_truncation_visible_after_refresh(owner):
    owner.items = [{"turnId": "old", "item": {"type": "agentMessage", "id": str(i), "text": str(i)}}
                   for i in range(550)]
    first = owner.backend.detail(SID)
    assert len(first["messages"]) == codex.MAX_MESSAGES
    assert first["messages"][0]["id"] == "150"
    assert first["history_truncated"]
    second = owner.backend.detail(SID)
    assert second["messages"] == first["messages"] and second["history_truncated"]


def test_native_rejection_is_sanitized_and_timeout_is_never_retried(owner):
    row = owner.backend.detail(SID)
    owner.error_method = "turn/start"
    with pytest.raises(FleetError, match="Codex rejected") as rejection:
        owner.backend.act(SID, "send", row["revision"], "reject", text="Continue")
    assert "private" not in str(rejection.value)
    assert rejection.value.status == 422 and "refresh" not in str(rejection.value)
    owner.error_method, owner.timeout_method = None, "turn/start"
    with pytest.raises(FleetError, match="delivery unknown"):
        owner.backend.act(SID, "send", row["revision"], "lost", text="Continue")
    assert sum(call.get("method") == "turn/start" for call in owner.calls) == 2
    assert len(owner.sockets) == 1


def test_reconnect_cannot_occur_implicitly_for_a_write(owner):
    owner.backend.detail(SID)
    epoch = owner.backend.rpc.epoch
    owner.backend.rpc.close()
    with pytest.raises(FleetError, match="refresh before acting"):
        owner.backend.rpc.call("turn/start", {"threadId": SID}, write=True, expected_epoch=epoch)
    assert len(owner.sockets) == 1


def test_read_only_subagent_cannot_be_controlled(owner):
    owner.direct = False
    row = owner.backend.detail(SID)
    assert not row["capabilities"]["send"]
    with pytest.raises(FleetError, match="not available"):
        owner.backend.act(SID, "send", row["revision"], "invalid", text="Continue")


def test_parallel_reads_share_one_initialized_connection(owner):
    threads = [threading.Thread(target=owner.backend.rows) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(owner.sockets) == 1
    assert sum(r.get("method") == "initialize" for r in owner.calls) == 1


def test_missing_socket_and_disabled_integration_never_create_an_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(codex, "_backend", None)
    monkeypatch.setenv("CC_TMUX_CODEX", "1")
    monkeypatch.setenv("CC_TMUX_CODEX_SOCKET", str(tmp_path / "missing.sock"))
    assert codex.get_backend() is None
    monkeypatch.setenv("CC_TMUX_CODEX", "0")
    assert codex.get_backend() is None
