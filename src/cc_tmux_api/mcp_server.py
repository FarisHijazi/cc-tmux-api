"""MCP server that lets one Claude Code session list, read and message every other session an HQ sees."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer

MESSAGE_CHARS = 4000
STALE_RETRIES = 5
logging.getLogger("httpx").setLevel(logging.WARNING)


def _token() -> str:
    token = os.environ.get("CC_TMUX_HQ_TOKEN", "")
    path = os.environ.get("CC_TMUX_HQ_TOKEN_FILE", "")
    if not token and path:
        token = Path(path).expanduser().read_text().strip()
    return token


def client() -> httpx.Client:
    token = _token()
    return httpx.Client(
        base_url=os.environ.get("CC_TMUX_HQ_URL", "http://127.0.0.1:8377").rstrip("/"),
        headers={"Authorization": f"Bearer {token}"} if token else {},
        timeout=float(os.environ.get("CC_TMUX_HQ_TIMEOUT", "45")),
    )


def _get(http: httpx.Client, path: str) -> dict:
    response = http.get(path)
    if response.status_code >= 400:
        raise RuntimeError(f"HQ {path} returned {response.status_code}: {response.text[:300]}")
    return response.json()


def _row(s: dict) -> dict:
    return {
        "id": s["id"],
        "host": s.get("host_label") or s.get("host"),
        "tmux_session": s.get("session"),
        "title": (s.get("title") or "").lstrip("✳⠂⠐ ").strip(),
        "workdir": s.get("workdir"),
        "status": s.get("activity") or s.get("status"),
        "waiting_on_question": bool(s.get("has_question")),
        "alive": s.get("alive", True),
    }


def build(http_factory=client, **server_options) -> MCPServer:
    mcp = MCPServer("claude-sessions", **server_options)

    def act(session_id: str, action: str, **fields) -> dict:
        with http_factory() as http:
            for _ in range(STALE_RETRIES):
                detail = _get(http, f"/api/sessions/{session_id}")
                body = {"action": action, "revision": detail["revision"],
                        "request_id": uuid.uuid4().hex, **fields}
                response = http.post(f"/api/sessions/{session_id}/action", json=body)
                # A 409 stale revision is rejected before delivery, so a fresh read and resend is safe.
                if response.status_code != 409 or "refresh" not in response.text:
                    break
            if response.status_code >= 400:
                raise RuntimeError(f"{action} failed with {response.status_code}: {response.text[:300]}")
            return {"ok": True, "session": _row(detail)}

    @mcp.tool()
    def list_sessions(query: str = "", host: str = "", include_ended: bool = False) -> list[dict]:
        """List Claude Code sessions on every machine the HQ federates.

        query filters on title, tmux session name and folder (case-insensitive);
        host filters on the machine label, e.g. "Dev" or "Ftower".
        """
        with http_factory() as http:
            sessions = _get(http, "/api/sessions")["sessions"]
        rows = [_row(s) for s in sessions if include_ended or s.get("alive", True)]
        if host:
            rows = [r for r in rows if host.lower() in str(r["host"]).lower()]
        if query:
            q = query.lower()
            rows = [r for r in rows if any(q in str(r[k] or "").lower() for k in ("title", "tmux_session", "workdir"))]
        return rows

    @mcp.tool()
    def read_session(session_id: str, last_messages: int = 6) -> dict:
        """Read a session's recent conversation, its screen tail, and any question it is waiting on."""
        with http_factory() as http:
            detail = _get(http, f"/api/sessions/{session_id}")
        messages = [
            {"role": m.get("role"), "text": (m.get("text") or "")[-MESSAGE_CHARS:]}
            for m in (detail.get("messages") or [])[-max(1, last_messages):]
        ]
        return {**_row(detail), "question": detail.get("menu"),
                "screen_tail": (detail.get("tail") or "")[-2000:], "messages": messages}

    @mcp.tool()
    def send_message(session_id: str, text: str) -> dict:
        """Send a prompt to another session, as if typed into it and submitted."""
        return act(session_id, "send", text=text)

    @mcp.tool()
    def choose_option(session_id: str, index: int) -> dict:
        """Answer the question a session is waiting on by picking an option (0-based, as read_session shows it)."""
        return act(session_id, "select", index=index)

    @mcp.tool()
    def interrupt(session_id: str) -> dict:
        """Interrupt (Escape) whatever the session is doing."""
        return act(session_id, "interrupt")

    return mcp


def _secret(name: str) -> str:
    value = os.environ.get(name, "")
    path = os.environ.get(name + "_FILE", "")
    return value or (Path(path).expanduser().read_text().strip() if path else "")


class Verifier:
    """Accepts a static bearer token (Claude Code clients) or an OIDC JWT access token (claude.ai connectors).

    Every connector token must come from the issuer, carry the client id as audience, and name an allowed
    email: this endpoint can type into shells on every machine, so a valid login alone is not enough.
    """

    def __init__(self, resource: str, issuer: str = "", client_id: str = "", emails=(), static: str = ""):
        import jwt

        self.jwt, self.resource, self.issuer, self.client_id = jwt, resource, issuer, client_id
        self.emails = {e.strip().lower() for e in emails if e.strip()}
        self.static = static
        self.jwks = jwt.PyJWKClient(issuer.rstrip("/") + "/jwks/", headers={"User-Agent": "cc-tmux-mcp/1"}) if issuer else None

    async def verify_token(self, token: str):
        import asyncio
        import hmac

        from mcp.server.auth.provider import AccessToken

        if self.static and hmac.compare_digest(token.encode(), self.static.encode()):
            return AccessToken(token=token, client_id="static-token", scopes=[], resource=self.resource)
        if not self.jwks or not self.emails:
            return None
        try:
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims = self.jwt.decode(token, key.key, algorithms=["RS256", "ES256"], audience=self.client_id,
                                     issuer=self.issuer)
        except self.jwt.PyJWTError:
            return None
        if str(claims.get("email", "")).lower() not in self.emails:
            return None
        return AccessToken(token=token, client_id=self.client_id, scopes=claims.get("scope", "").split(),
                           expires_at=int(claims["exp"]) if claims.get("exp") else None, resource=self.resource, subject=claims.get("sub"))


def http_app():
    from mcp.server.auth.settings import AuthSettings

    public = os.environ["CC_TMUX_MCP_PUBLIC_URL"].rstrip("/")
    issuer = os.environ.get("CC_TMUX_MCP_OIDC_ISSUER", "")
    verifier = Verifier(f"{public}/mcp", issuer, os.environ.get("CC_TMUX_MCP_OIDC_CLIENT_ID", ""),
                        os.environ.get("CC_TMUX_MCP_ALLOWED_EMAILS", "").split(","), _secret("CC_TMUX_MCP_TOKEN"))
    if not verifier.static and not (verifier.jwks and verifier.emails):
        raise SystemExit("Refusing to serve HTTP without CC_TMUX_MCP_TOKEN or an issuer plus allowed emails")
    server = build(token_verifier=verifier,
                   # the issuer puts the client id in aud, which Verifier checks itself
                   auth=AuthSettings(issuer_url=issuer or public, resource_server_url=f"{public}/mcp",
                                     validate_token_resource=False))
    return server.streamable_http_app(stateless_http=True, host="0.0.0.0")


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP with OAuth instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8392)
    args = parser.parse_args()
    if not args.http:
        build().run()
        return
    import uvicorn

    uvicorn.run(http_app(), host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")
