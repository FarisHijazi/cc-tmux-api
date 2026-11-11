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


def build(http_factory=client) -> MCPServer:
    mcp = MCPServer("claude-sessions")

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


def main():
    build().run()
