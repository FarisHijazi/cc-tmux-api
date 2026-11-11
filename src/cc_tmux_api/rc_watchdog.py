"""Keep Remote Control connected on every interactive Claude Code session in this machine's tmux.

Detection reads files, not the screen: Claude Code writes `<config>/sessions/<pid>.json` with the tmux
pane and `bridgeSessionId` (cleared on disconnect), and the HQ reports whether that bridge is online.
Repair drives the CLI's own `/remote-control` toggle, only when the session is idle with an empty prompt:
  disconnected -> /remote-control
  stale        -> /remote-control, pick "Disconnect this session", /remote-control
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("rc-watchdog")
RULE = "────"
HUB_ID = re.compile(r"[0-9a-f]{32}")
COMPLETION = re.compile(r"❯ /remote-control {2,}\S")


@dataclass(frozen=True)
class Session:
    pid: int
    pane: str
    bridge: str | None
    status: str
    name: str
    file: Path


def config_dirs() -> list[Path]:
    override = os.environ.get("CC_RC_WATCHDOG_DIRS")
    if override:
        return [Path(p).expanduser() for p in override.split(":") if p]
    # Only launcher-routed profiles: their bridges go through the HQ, so the HQ's view is authoritative.
    return sorted((Path.home() / ".local/state/claude-codex/account-router/accounts").glob("*/claude"))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_session(file: Path) -> Session | None:
    try:
        data = json.loads(file.read_text())
    except (OSError, ValueError):
        return None
    tmux = data.get("tmux") or ""
    if data.get("kind") != "interactive" or "." not in tmux:
        return None
    pid = int(data.get("pid") or 0)
    if not pid or not _alive(pid):
        return None
    return Session(pid, tmux.rsplit(".", 1)[1], data.get("bridgeSessionId"), data.get("status") or "",
                   data.get("name") or "", file)


def local_sessions(dirs=None) -> list[Session]:
    found = {}
    for d in dirs or config_dirs():
        for file in (d / "sessions").glob("*.json"):
            s = read_session(file)
            if s:
                found[s.pid] = s
    return list(found.values())


def bridge_health(snapshot: dict) -> dict[str, bool]:
    health = {}
    for s in snapshot.get("sessions", []):
        sid = s.get("session") or ""
        if s.get("transport") != "native" and not sid.startswith("cse_"):
            continue
        upstream = (s.get("upstream") or {}).get("status")
        ok = bool(s.get("alive") and s.get("online") and upstream in (None, "connected"))
        health[sid] = health.get(sid, False) or ok
    return health


def diagnose(session: Session, health: dict[str, bool]) -> str | None:
    if not session.bridge:
        return "connect"
    hub_id = session.bridge.removeprefix("session_")
    if not HUB_ID.fullmatch(hub_id):
        return None  # bridged straight to Anthropic; the HQ cannot vouch for it either way
    return None if health.get("cse_" + hub_id) else "reconnect"


class Tmux:
    def capture(self, pane: str) -> list[str]:
        out = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane], capture_output=True, text=True, check=True)
        return [line.rstrip() for line in out.stdout.splitlines() if line.strip()]

    def keys(self, pane: str, *keys: str, literal: bool = False):
        cmd = ["tmux", "send-keys", "-t", pane] + (["-l", "--"] if literal else []) + list(keys)
        subprocess.run(cmd, check=True)


def prompt_is_empty(lines: list[str]) -> bool:
    for i in range(len(lines) - 2, 0, -1):
        if lines[i].startswith("❯") and lines[i - 1].startswith(RULE) and lines[i + 1].startswith(RULE):
            return lines[i].strip() == "❯"
    return False


def highlighted(lines: list[str]) -> str:
    return next((line.strip() for line in reversed(lines) if line.strip().startswith("❯")), "")


class Repairer:
    def __init__(self, tmux=None, sleep=time.sleep, reread=read_session, settle=1.0, wait=25.0):
        self.tmux, self.sleep, self.reread, self.settle, self.wait = tmux or Tmux(), sleep, reread, settle, wait

    def _toggle(self, pane: str) -> list[str]:
        self.tmux.keys(pane, "/remote-control", literal=True)
        self.sleep(self.settle)
        if not any(COMPLETION.search(line) for line in self.tmux.capture(pane)):
            self.tmux.keys(pane, "Escape")
            self.tmux.keys(pane, "C-u")
            raise RuntimeError("autocomplete did not offer /remote-control")
        self.tmux.keys(pane, "Enter")
        self.sleep(self.settle * 2)
        return self.tmux.capture(pane)

    def _disconnect_from_dialog(self, pane: str, lines: list[str]):
        if not any("Disconnect this session" in line for line in lines):
            raise RuntimeError("Remote Control dialog did not open")
        for _ in range(4):
            if "Disconnect this session" in highlighted(lines):
                self.tmux.keys(pane, "Enter")
                self.sleep(self.settle * 2)
                return
            self.tmux.keys(pane, "Up")
            self.sleep(self.settle / 2)
            lines = self.tmux.capture(pane)
        self.tmux.keys(pane, "Escape")
        raise RuntimeError("could not select Disconnect this session")

    def _await_bridge(self, session: Session, old: str | None) -> str:
        deadline = time.monotonic() + self.wait
        while True:
            now = self.reread(session.file)
            if now and now.bridge and now.bridge != old:
                return now.bridge
            if time.monotonic() >= deadline:
                raise RuntimeError("no new bridge session id after /remote-control")
            self.sleep(self.settle)

    def repair(self, session: Session, kind: str) -> str:
        if session.status != "idle":
            raise RuntimeError(f"session is {session.status}, not idle")
        if not prompt_is_empty(self.tmux.capture(session.pane)):
            raise RuntimeError("prompt has a draft or a dialog is open")
        old = session.bridge
        if kind == "reconnect":
            self._disconnect_from_dialog(session.pane, self._toggle(session.pane))
        self._toggle(session.pane)
        return self._await_bridge(session, old)


def hub_snapshot() -> dict:
    token = os.environ.get("CC_TMUX_HQ_TOKEN", "")
    path = os.environ.get("CC_TMUX_HQ_TOKEN_FILE", "")
    if not token and path:
        token = Path(path).expanduser().read_text().strip()
    url = os.environ.get("CC_TMUX_HQ_URL", "http://127.0.0.1:8377").rstrip("/")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = httpx.get(url + "/api/sessions", headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()


def mcp_health() -> dict[str, bool]:
    """Health through the public claude-sessions MCP, for machines that cannot reach the HQ directly."""
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    token = os.environ.get("CC_TMUX_MCP_TOKEN") or Path(os.environ["CC_TMUX_MCP_TOKEN_FILE"]).expanduser().read_text().strip()

    async def call():
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60) as http:
            async with streamable_http_client(os.environ["CC_RC_WATCHDOG_MCP_URL"], http_client=http) as (r, w, *_):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    result = await session.call_tool("remote_control_health", {})
                    if result.is_error:
                        raise RuntimeError(str(result.content)[:200])
                    data = result.structured_content or json.loads(result.content[0].text)
                    return data.get("result", data)

    return asyncio.run(call())


def health_now() -> dict[str, bool]:
    return mcp_health() if os.environ.get("CC_RC_WATCHDOG_MCP_URL") else bridge_health(hub_snapshot())


def unhealthy(health_fn=health_now, sessions_fn=local_sessions) -> dict[int, tuple[Session, str]]:
    health = health_fn()
    return {s.pid: (s, kind) for s in sessions_fn() if (kind := diagnose(s, health))}


def run_once(health_fn=health_now, sessions_fn=local_sessions, repairer=None, confirm_after=30.0,
             sleep=time.sleep, dry_run=False) -> list[dict]:
    first = unhealthy(health_fn, sessions_fn)
    if not first:
        return []
    sleep(confirm_after)
    second = unhealthy(health_fn, sessions_fn)
    repairer = repairer or Repairer()
    results = []
    for pid, (session, kind) in second.items():
        if pid not in first:
            continue
        row = {"pid": pid, "pane": session.pane, "name": session.name, "kind": kind}
        if dry_run:
            results.append({**row, "result": "dry-run"})
            continue
        try:
            row["result"] = "repaired"
            row["bridge"] = repairer.repair(session, kind)
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            row["result"] = f"skipped: {exc}"
        results.append(row)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report what would be repaired")
    parser.add_argument("--confirm-after", type=float, default=30.0,
                        help="seconds a session must stay unhealthy before repair")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for row in run_once(confirm_after=args.confirm_after, dry_run=args.dry_run):
        log.info(json.dumps(row))
