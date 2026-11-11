"""The public HTTP MCP: who gets in, and what an unauthenticated connector is told."""
import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from cc_tmux_api import mcp_server

ISSUER = "https://auth.example.com/application/o/cc-sessions/"
CLIENT = "client-123"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Keys:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": KEY.public_key()})()


def token(**claims):
    body = {"iss": ISSUER, "aud": CLIENT, "sub": "u1", "email": "user@example.com", "exp": time.time() + 600, **claims}
    return jwt.encode(body, KEY, algorithm="RS256")


@pytest.fixture
def verifier():
    v = mcp_server.Verifier("https://hq.example.com/mcp", ISSUER, CLIENT, ["User@Example.com"], static="s" * 40)
    v.jwks = Keys()
    return v


def check(v, t):
    return asyncio.run(v.verify_token(t))


def test_allowed_email_from_the_issuer_gets_in(verifier):
    assert check(verifier, token()).subject == "u1"


@pytest.mark.parametrize("claims", [{"email": "someone@example.com"}, {"aud": "other-client"},
                                    {"iss": "https://evil.example.com/"}, {"exp": time.time() - 5}, {"email": None}])
def test_everything_else_is_refused(verifier, claims):
    assert check(verifier, token(**claims)) is None


def test_static_token_for_claude_code_clients(verifier):
    assert check(verifier, "s" * 40).client_id == "static-token"
    assert check(verifier, "s" * 39) is None


def test_refuses_to_start_without_any_auth(monkeypatch):
    monkeypatch.setenv("CC_TMUX_MCP_PUBLIC_URL", "https://hq.example.com")
    for name in ("CC_TMUX_MCP_OIDC_ISSUER", "CC_TMUX_MCP_TOKEN", "CC_TMUX_MCP_TOKEN_FILE", "CC_TMUX_MCP_ALLOWED_EMAILS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        mcp_server.http_app()


def test_unauthenticated_connector_is_pointed_at_the_issuer(monkeypatch):
    monkeypatch.setenv("CC_TMUX_MCP_PUBLIC_URL", "https://hq.example.com")
    monkeypatch.setenv("CC_TMUX_MCP_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("CC_TMUX_MCP_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("CC_TMUX_MCP_ALLOWED_EMAILS", "user@example.com")
    with TestClient(mcp_server.http_app(), base_url="https://hq.example.com") as http:
        denied = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           headers={"Accept": "application/json, text/event-stream"})
        assert denied.status_code == 401
        assert "oauth-protected-resource" in denied.headers["www-authenticate"]
        meta = http.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["authorization_servers"] == [ISSUER]
