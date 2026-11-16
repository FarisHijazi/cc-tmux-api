"""The MCP tools against a fake HQ: what a calling Claude sees and what reaches the HQ."""
import asyncio
import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cc_tmux_api import mcp_server

SESSIONS = [
    {"id": "a", "host": "dev", "host_label": "Dev", "session": "ai-course", "title": "✳ ai course",
     "workdir": "/p/course", "activity": "idle", "has_question": False, "alive": True, "revision": "r1"},
    {"id": "b", "host": "ftower", "host_label": "Ftower", "session": "proxy-1", "title": "proxy",
     "workdir": "/p/proxy", "activity": "working", "has_question": True, "alive": True, "revision": "r2",
     "menu": {"options": ["Yes", "No"]}, "tail": "Proceed?", "messages": [{"role": "user", "text": "hi"}]},
    {"id": "c", "host": "dev", "host_label": "Dev", "session": "old", "title": "old", "workdir": "/p",
     "alive": False, "revision": "r3"},
]


@pytest.fixture
def hq():
    posted = []

    def handler(request: httpx.Request):
        assert request.headers["authorization"] == "Bearer secret"
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json={"sessions": SESSIONS})
        sid = request.url.path.split("/")[3]
        if request.method == "POST":
            posted.append((sid, json.loads(request.content)))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json=next(s for s in SESSIONS if s["id"] == sid))

    def factory():
        return httpx.Client(base_url="http://hq", transport=httpx.MockTransport(handler),
                            headers={"Authorization": "Bearer secret"})

    return mcp_server.build(factory), posted


def call(server, name, **args):
    result = asyncio.run(server.call_tool(name, args))
    assert not result.is_error, result.content
    data = result.structured_content
    if data is None:
        return json.loads(result.content[0].text)
    return data["result"] if set(data) == {"result"} else data


def test_lists_live_sessions_across_hosts_and_filters(hq):
    server, _ = hq
    assert [r["id"] for r in call(server, "list_sessions")] == ["a", "b"]
    assert [r["id"] for r in call(server, "list_sessions", host="ftower")] == ["b"]
    assert [r["id"] for r in call(server, "list_sessions", query="COURSE")] == ["a"]
    assert call(server, "list_sessions")[0]["title"] == "ai course"


@pytest.mark.parametrize("include_ended", [False, True])
def test_discovery_requests_only_the_history_the_caller_needs(include_ended):
    requests = []

    def handler(request):
        requests.append(request)
        # Older HQs may ignore the query; the MCP must retain its local filter.
        return httpx.Response(200, json={"sessions": SESSIONS})

    server = mcp_server.build(lambda: httpx.Client(base_url="http://hq", transport=httpx.MockTransport(handler)))
    rows = call(server, "list_sessions", include_ended=include_ended)
    assert requests[0].url.params["include_ended"] == str(include_ended).lower()
    assert [row["id"] for row in rows] == (["a", "b", "c"] if include_ended else ["a", "b"])


def test_read_session_shows_question_and_messages(hq):
    server, _ = hq
    detail = call(server, "read_session", session_id="b")
    assert detail["waiting_on_question"] and detail["question"] == {"options": ["Yes", "No"]}
    assert detail["messages"] == [{"role": "user", "text": "hi"}]


def test_codex_discovery_preserves_provider_controls_and_source_message_ids():
    row = {**SESSIONS[0], "provider": "codex", "transport": "codex",
           "native_control": {"request_id": "approval-1", "tool_name": "Command"},
           "messages": [{"id": "native-item-1", "role": "assistant", "text": "Ready"}],
           "capabilities": {"send": False, "respond": True}}
    def handler(request):
        return httpx.Response(200, json={"sessions": [row]} if request.url.path == "/api/sessions" else row)
    server = mcp_server.build(lambda: httpx.Client(base_url="http://hq", transport=httpx.MockTransport(handler)))
    assert call(server, "list_sessions")[0]["provider"] == "codex"
    detail = call(server, "read_session", session_id="a")
    assert detail["transport"] == "codex"
    assert detail["messages"] == row["messages"]
    assert detail["capabilities"] == row["capabilities"]
    assert detail["native_control"] == row["native_control"]


def test_send_message_posts_with_current_revision_and_fresh_request_id(hq):
    server, posted = hq
    call(server, "send_message", session_id="a", text="status?")
    call(server, "send_message", session_id="a", text="status?")
    (sid, first), (_, second) = posted
    assert sid == "a" and first["action"] == "send" and first["text"] == "status?"
    assert first["revision"] == "r1" and first["request_id"] != second["request_id"]


def test_choose_option_and_interrupt(hq):
    server, posted = hq
    call(server, "choose_option", session_id="b", index=1, revision="r2")
    call(server, "interrupt", session_id="b")
    assert [(p["action"], p.get("index")) for _, p in posted] == [("select", 1), ("interrupt", None)]


def test_send_retries_with_a_fresh_revision_when_the_screen_moved():
    attempts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={**SESSIONS[1], "revision": f"r{len(attempts)}"})
        attempts.append(json.loads(request.content)["revision"])
        if len(attempts) < 3:
            return httpx.Response(409, json={"detail": "Screen changed; refresh before acting"})
        return httpx.Response(200, json={"ok": True})

    server = mcp_server.build(lambda: httpx.Client(base_url="http://hq", transport=httpx.MockTransport(handler)))
    assert call(server, "send_message", session_id="b", text="hi")["ok"]
    assert attempts == ["r0", "r1", "r2"]


def test_remote_control_health_reports_native_bridges():
    snap = {"sessions": [{"session": "cse_1", "transport": "native", "alive": True, "online": True,
                          "upstream": {"status": "connected"}}, {"session": "pane", "alive": True}]}
    server = mcp_server.build(lambda: httpx.Client(base_url="http://hq", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=snap))))
    assert call(server, "remote_control_health") == {"cse_1": True}


def test_unreachable_sessions_are_not_reported_as_live():
    snapshot = {"sessions": [{**SESSIONS[0], "alive": None, "lifecycle": "unreachable"}]}
    server = mcp_server.build(lambda: httpx.Client(base_url="http://hq", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=snapshot))))
    assert call(server, "list_sessions") == []
    assert call(server, "list_sessions", include_ended=True)[0]["lifecycle"] == "unreachable"


def test_lists_nodes_even_when_they_have_no_sessions():
    hosts = [{"id": "work", "label": "Work", "available": False, "error": "Connection failed"}]
    server = mcp_server.build(lambda: httpx.Client(base_url="http://hq", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"hosts": hosts, "sessions": []}))))
    assert call(server, "list_nodes") == hosts


def test_read_exposes_revision_and_actions_return_the_hq_receipt(hq):
    server, posted = hq
    assert call(server, "read_session", session_id="b")["revision"] == "r2"
    result = call(server, "send_message", session_id="a", text="hello")
    assert result["receipt"] == {"ok": True}
    assert result["request_id"] == posted[0][1]["request_id"]


def test_select_preserves_observed_revision_and_never_retries_a_changed_question():
    posted = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={**SESSIONS[1], "revision": "new-question"})
        posted.append(json.loads(request.content))
        return httpx.Response(409, json={"detail": "Question changed; refresh before acting"})

    server = mcp_server.build(lambda: httpx.Client(base_url="http://hq", transport=httpx.MockTransport(handler)))
    with pytest.raises(ToolError, match="Question changed"):
        asyncio.run(server.call_tool("choose_option", {"session_id": "b", "index": 0, "revision": "observed"}))
    assert len(posted) == 1 and posted[0]["revision"] == "observed"


@pytest.mark.parametrize("status", [409, 502, 503])
def test_uncertain_delivery_is_not_retried(status):
    posted = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=SESSIONS[0])
        posted.append(request)
        return httpx.Response(status, json={"detail": "Action delivery unknown; do not retry"})

    server = mcp_server.build(lambda: httpx.Client(base_url="http://hq", transport=httpx.MockTransport(handler)))
    with pytest.raises(ToolError, match="Action delivery unknown"):
        asyncio.run(server.call_tool("send_message", {"session_id": "a", "text": "hi"}))
    assert len(posted) == 1


def test_native_response_uses_the_existing_guarded_action_path(hq):
    server, posted = hq
    response = {"request_id": "question-1", "behavior": "deny"}
    call(server, "respond_native_request", session_id="b", revision="r2", response=response)
    assert posted[0][1]["action"] == "respond"
    assert posted[0][1]["response"] == response
    assert posted[0][1]["revision"] == "r2"


def test_empty_revision_cannot_approve_the_current_question(hq):
    server, posted = hq
    with pytest.raises(ToolError, match="nonempty revision"):
        call(server, "choose_option", session_id="b", index=0, revision="")
    assert posted == []


def test_transport_timeout_is_reported_as_uncertain_without_retry():
    posted = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=SESSIONS[0])
        posted.append(request)
        raise httpx.ReadTimeout("timed out")

    server = mcp_server.build(lambda: httpx.Client(base_url="http://hq", transport=httpx.MockTransport(handler)))
    with pytest.raises(ToolError, match="Action delivery unknown; do not retry"):
        asyncio.run(server.call_tool("send_message", {"session_id": "a", "text": "hi"}))
    assert len(posted) == 1
