"""The MCP tools against a fake HQ: what a calling Claude sees and what reaches the HQ."""
import asyncio
import json

import httpx
import pytest

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


def test_read_session_shows_question_and_messages(hq):
    server, _ = hq
    detail = call(server, "read_session", session_id="b")
    assert detail["waiting_on_question"] and detail["question"] == {"options": ["Yes", "No"]}
    assert detail["messages"] == [{"role": "user", "text": "hi"}]


def test_send_message_posts_with_current_revision_and_fresh_request_id(hq):
    server, posted = hq
    call(server, "send_message", session_id="a", text="status?")
    call(server, "send_message", session_id="a", text="status?")
    (sid, first), (_, second) = posted
    assert sid == "a" and first["action"] == "send" and first["text"] == "status?"
    assert first["revision"] == "r1" and first["request_id"] != second["request_id"]


def test_choose_option_and_interrupt(hq):
    server, posted = hq
    call(server, "choose_option", session_id="b", index=1)
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
