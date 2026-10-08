"""The phone API protects existing sessions, including legacy write routes."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("CC_TMUX_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CC_TMUX_TOKEN", "test-pairing-key-with-at-least-32-characters")
    monkeypatch.setenv("CC_TMUX_PUBLIC_URL", "https://hq.example.test")
    monkeypatch.setenv("CC_TMUX_MANAGE_WORKERS", "0")
    from cc_tmux_api.server import create_app

    return create_app()


@pytest.fixture
def client(app):
    # Deliberately no lifespan: never start background host polling in API tests.
    return TestClient(app, base_url="https://hq.example.test")


def pair(client):
    return client.post(
        "/api/auth/login", json={"token": "test-pairing-key-with-at-least-32-characters"}
    )


def test_unauthorized_clients_cannot_read_or_mutate_sessions(client):
    assert client.get("/api/auth").json() == {"required": True, "authenticated": False}
    for path in ("/api/sessions", "/api/workers", "/api/push/preferences"):
        assert client.get(path).status_code == 401
    assert (
        client.post(
            "/api/workers/action", json={"action": "spawn", "title": "test", "prompt": "test"}
        ).status_code
        == 401
    )


def test_pairing_uses_http_only_signed_cookie_and_logout(client):
    response = pair(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "test-pairing-key" not in cookie
    assert client.get("/api/auth").json()["authenticated"] is True
    assert client.post("/api/auth/logout", json={}).status_code == 200
    assert client.get("/api/auth").json()["authenticated"] is False


def test_cross_site_requests_rejected_even_with_valid_credentials(client):
    pair(client)
    response = client.post(
        "/api/push/preferences",
        json={"session_id": "test", "muted": True},
        headers={"Origin": "https://untrusted.example.test"},
    )
    assert response.status_code == 403
    assert client.get("/api/auth", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/auth", headers={"Host": "attacker.example.test"}).status_code == 400


def test_api_never_cached_and_static_shell_is_public(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/auth").headers["cache-control"] == "no-store"


def test_pairing_rate_limit(client):
    for _ in range(10):
        assert client.post("/api/auth/login", json={"token": "wrong"}).status_code == 401
    assert pair(client).status_code == 429


def test_actions_require_revision_and_identity_and_return_stale_errors(client, monkeypatch):
    from cc_tmux_api import fleet

    pair(client)
    assert (
        client.post(
            "/api/sessions/target/action", json={"action": "send", "text": "hello"}
        ).status_code
        == 422
    )

    def stale(*args, **kwargs):
        raise fleet.FleetError("The session changed; refresh before sending", 409)

    monkeypatch.setattr(fleet, "act", stale)
    response = client.post(
        "/api/sessions/target/action",
        json={
            "action": "send",
            "text": "hello",
            "revision": "old",
            "request_id": "request-123",
        },
    )
    assert response.status_code == 409
    assert "changed" in response.json()["detail"]


def test_muted_session_preferences_are_persistent(client, app):
    pair(client)
    assert (
        client.post(
            "/api/push/preferences", json={"session_id": "host-pane", "muted": True}
        ).status_code
        == 200
    )
    assert "host-pane" in client.get("/api/push/preferences").json()["muted"]


def test_security_rejects_unsigned_cookie_and_short_token(client):
    from cc_tmux_api.security import Security

    client.cookies.set("hq_device", "1.fake.signature")
    assert client.get("/api/auth").json()["authenticated"] is False
    assert Security("", "https://hq.example.test").public_url == "https://hq.example.test"
    with pytest.raises(ValueError):
        Security("too-short", "")


def test_bearer_auth_works_without_cookie(client):
    response = client.get(
        "/api/auth",
        headers={"Authorization": "Bearer test-pairing-key-with-at-least-32-characters"},
    )
    assert response.json()["authenticated"] is True


@pytest.mark.parametrize("url", [
    "http://hq.example.test", "https://user@hq.example.test",
    "https://hq.example.test/path", "https://hq.example.test?query=1",
    "https://hq.example.test#fragment",
])
@pytest.mark.parametrize("token", ["", "test-pairing-key-with-at-least-32-characters"])
def test_remote_configuration_requires_an_https_origin(url, token):
    from cc_tmux_api.security import Security

    with pytest.raises(ValueError, match="HTTPS origin"):
        Security(token, url)


def test_legacy_worker_send_cannot_bypass_session_guards(client, monkeypatch):
    from cc_tmux_api import fleet, workers

    pair(client)
    sent = []
    monkeypatch.setattr(workers, "send_text", lambda *args: pytest.fail("unguarded send"))
    monkeypatch.setattr(workers, "list_workers", lambda: [{"session": "hq-toy"}])
    monkeypatch.setattr(fleet, "hosts", lambda: [{"id": "local"}, {"id": "vm", "ssh": "vm"}])
    target = {"host": "local", "session": "hq-toy"}
    monkeypatch.setattr(fleet, "detail", lambda _: target)
    monkeypatch.setattr(fleet, "act", lambda *args, **kwargs: sent.append((args, kwargs)) or {"ok": True})
    body = {"action": "send", "session": "hq-toy", "text": "hello"}
    assert client.post("/api/workers/action", json=body).status_code == 400
    body.update(session_id="exact-id", revision="observed-revision", request_id="unique-request")
    for host, session in [("vm", "hq-toy"), ("local", "ordinary-shell")]:
        target.update(host=host, session=session)
        assert client.post("/api/workers/action", json=body).status_code == 409
    assert not sent
    target.update(host="local", session="hq-toy")
    assert client.post("/api/workers/action", json=body).status_code == 200
    assert sent == [(("exact-id", "send", "observed-revision", "unique-request"), {"text": "hello"})]


def test_worker_list_exposes_only_local_control_identity(client, monkeypatch):
    from cc_tmux_api import fleet, routines, workers

    pair(client)
    monkeypatch.setattr(workers, "list_workers", lambda: [{"session": "hq-toy"}, {"session": "hq-exited"}])
    monkeypatch.setattr(workers, "list_others", lambda: [])
    monkeypatch.setattr(routines, "list_routines", lambda: [])
    monkeypatch.setattr(fleet, "hosts", lambda: [{"id": "local"}, {"id": "vm", "ssh": "vm"}])
    monkeypatch.setattr(fleet, "snapshot", lambda: {"sessions": [
        {"host": "local", "session": "hq-toy", "id": "local-id", "revision": "r1"},
        {"host": "vm", "session": "hq-toy", "id": "remote-id", "revision": "r2"},
    ]})
    data = client.get("/api/workers").json()["workers"]
    assert data[0]["control"] == {"session_id": "local-id", "revision": "r1"}
    assert data[1]["control"] is None


def test_session_routes_forward_cascade_context(client, monkeypatch):
    from cc_tmux_api import fleet

    pair(client)
    seen = []
    monkeypatch.setattr(fleet, "snapshot", lambda **kw: seen.append(kw) or {"sessions": []})
    monkeypatch.setattr(fleet, "detail", lambda sid, **kw: seen.append(kw) or {"id": sid})
    monkeypatch.setattr(fleet, "act", lambda sid, **kw: seen.append(kw) or {"ok": True})
    headers = {"X-HQ-Path": "parent, middle"}
    assert client.get("/api/sessions", headers=headers).status_code == 200
    assert client.get("/api/sessions/leaf", headers=headers).status_code == 200
    assert client.post("/api/sessions/leaf/action", headers=headers, json={
        "action": "key", "key": "Left", "revision": "r", "request_id": "cascade-action"
    }).status_code == 200
    assert all(call["path"] == ("parent", "middle") for call in seen)
    assert client.get("/api/sessions", headers={"X-HQ-Path": ",".join(["node"] * 9)}).status_code == 508
    assert len(seen) == 3


def test_no_pairing_for_explicit_tokenless_private_host(monkeypatch, tmp_path):
    from cc_tmux_api import fleet
    from cc_tmux_api.server import create_app

    monkeypatch.setenv("CC_TMUX_TOKEN", "")
    monkeypatch.setenv("CC_TMUX_PUBLIC_URL", "")
    monkeypatch.setenv("CC_TMUX_ALLOWED_HOSTS", "100.64.0.10")
    monkeypatch.setenv("CC_TMUX_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(fleet, "snapshot", lambda **_: {"sessions": [], "hosts": []})
    browser = TestClient(create_app(), base_url="http://100.64.0.10:8389")
    assert browser.get("/api/auth").json() == {"required": False, "authenticated": True}
    assert browser.get("/api/sessions").status_code == 200
    assert not browser.cookies


def test_lifecycle_monitor_runs_without_push(app, monkeypatch):
    import asyncio
    import threading

    from cc_tmux_api import fleet
    from cc_tmux_api.server import _lifespan

    observed = threading.Event()
    monkeypatch.setattr(fleet, "snapshot", lambda: observed.set() or {"sessions": []})
    monkeypatch.setattr(app.state.push, "config", lambda: {"available": False})
    monkeypatch.setattr(app.state.push, "observe", lambda _: pytest.fail("push is unavailable"))

    async def check():
        async with _lifespan(app):
            assert await asyncio.to_thread(observed.wait, 2)

    asyncio.run(check())


def test_slow_push_delivery_does_not_stall_session_monitor(app, monkeypatch):
    import asyncio
    import threading

    from cc_tmux_api import fleet, server

    entered, release, second_census = threading.Event(), threading.Event(), threading.Event()
    observations = []
    original_sleep = asyncio.sleep

    async def fast_sleep(_):
        await original_sleep(0.005)

    def snapshot():
        observations.append(True)
        if len(observations) >= 2:
            second_census.set()
        return {"sessions": []}

    def deliver():
        entered.set()
        release.wait(3)

    def observe(_, *, deliver):
        assert deliver is False

    monkeypatch.setattr(fleet, "snapshot", snapshot)
    monkeypatch.setattr(server.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(app.state.push, "config", lambda: {"available": True})
    monkeypatch.setattr(app.state.push, "observe", observe)
    monkeypatch.setattr(app.state.push, "deliver_pending", deliver)

    async def check():
        async with server._lifespan(app):
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                assert await asyncio.to_thread(second_census.wait, 1)
                assert not release.is_set()
            finally:
                release.set()

    asyncio.run(check())


@pytest.mark.parametrize("endpoint,expected", [
    ("127.0.0.1", ("127.0.0.1", 8377)),
    ("127.0.0.1:8389", ("127.0.0.1", 8389)),
    ("::1", ("::1", 8377)),
    ("[::1]:8389", ("::1", 8389)),
    ("[::1]", ("::1", 8377)),
])
def test_bind_endpoint_preserves_default_or_explicit_port(endpoint, expected):
    from cc_tmux_api.server import bind_address

    assert bind_address(endpoint, 8377) == expected


@pytest.mark.parametrize("endpoint", ["localhost:nope", "localhost:70000", "user@localhost:8377", "localhost:8377/path"])
def test_invalid_bind_endpoints_rejected(endpoint):
    from cc_tmux_api.server import bind_address

    with pytest.raises(ValueError):
        bind_address(endpoint, 8377)


@pytest.fixture
def public_noauth_client(monkeypatch, tmp_path):
    monkeypatch.setenv('CC_TMUX_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('CC_TMUX_TOKEN', '')
    monkeypatch.setenv('CC_TMUX_PUBLIC_URL', 'https://hq.example.test')
    monkeypatch.setenv('CC_TMUX_MANAGE_WORKERS', '0')
    from cc_tmux_api import fleet
    from cc_tmux_api.server import create_app

    monkeypatch.setattr(fleet, 'snapshot', lambda **kwargs: {'hosts': [], 'sessions': []})
    # No lifespan or real tmux controls in these public-origin contract tests.
    return TestClient(create_app(), base_url='https://hq.example.test')


def test_public_https_without_token_allows_same_origin_reads_and_controls(public_noauth_client, monkeypatch):
    from cc_tmux_api import fleet

    calls = []
    monkeypatch.setattr(fleet, 'act', lambda *args, **kwargs: calls.append((args, kwargs)) or {'ok': True})
    client = public_noauth_client
    assert client.get('/api/auth').json() == {'required': False, 'authenticated': True}
    response = client.get('/api/sessions', headers={'Origin': 'https://hq.example.test'})
    assert response.status_code == 200 and response.json()['sessions'] == []
    assert response.headers['cache-control'] == 'no-store'
    response = client.post('/api/sessions/opaque/action',
        json={'action': 'key', 'key': 'Escape', 'revision': 'observed', 'request_id': 'request-public'},
        headers={'Origin': 'https://hq.example.test', 'Sec-Fetch-Site': 'same-origin'})
    assert response.status_code == 200 and response.json() == {'ok': True}
    assert len(calls) == 1
    assert calls[0][1]['revision'] == 'observed'
    assert calls[0][1]['request_id'] == 'request-public'
    assert not client.cookies


@pytest.mark.parametrize('headers,expected', [
    ({'Origin': 'https://other.example.test'}, 403),
    ({'Origin': 'https://hq.example.test', 'Sec-Fetch-Site': 'cross-site'}, 403),
    ({'Host': 'other.example.test'}, 400),
    ({'Origin': 'https://hq.example.test', 'Content-Type': 'text/plain'}, 415),
])
def test_public_noauth_keeps_origin_host_and_json_guards(public_noauth_client, monkeypatch, headers, expected):
    from cc_tmux_api import fleet

    def forbidden(*args, **kwargs):
        pytest.fail('Rejected public request reached a session control')
    monkeypatch.setattr(fleet, 'act', forbidden)
    response = public_noauth_client.post('/api/sessions/opaque/action',
        json={'action': 'key', 'key': 'Escape', 'revision': 'observed', 'request_id': 'request-public'},
        headers=headers)
    assert response.status_code == expected
