"""Keep Remote Control connected on every interactive Claude Code session in this machine's tmux.

Detection reads files, not the screen: Claude Code writes `<config>/sessions/<pid>.json` with the tmux
pane and `bridgeSessionId` (cleared on disconnect), and the HQ reports whether that bridge is online.
Repair drives the CLI's own `/rc` toggle, only when the session is idle with an empty prompt and no menu:
  disconnected -> /rc twice; a "Remote Control" dialog (already connected) is answered with Continue
  stale        -> /rc, pick "Disconnect this session", then the disconnected repair
With --close-menus a session waiting on a menu (never a busy one) gets Escape first and, once repaired, a message
asking Claude to show that menu again.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .fleet_probe import claude_config_dirs, process_marker

log = logging.getLogger("rc-watchdog")
RULE = "────"
HUB_ID = re.compile(r"[0-9a-f]{32}")
COMPLETION = re.compile(r"❯\s+(/remote-control|…\S*) \(rc\)")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
DIM = re.compile(r"\x1b\[2m.*?(?:\x1b\[0m|\x1b\[22m|$)")
MENU_FOOTER = re.compile(r"Enter to (select|confirm|submit)|Esc to (cancel|close|continue|go back)|↑/↓ to navigate")
ENV_FILE = "~/.config/cc-rc-watchdog/env"
REASK = "sorry i skipped the menu by accident, could you show me the menu options again"


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
    return [Path(p).expanduser() for p in override.split(":") if p] if override else claude_config_dirs()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _same_process(pid: int, data: dict) -> bool:
    """A file left by a process that died before a reboot can name a pid reused by anything else."""
    if "procStart" not in data or not sys.platform.startswith("linux"):
        return True
    return process_marker({"pid": pid}) == (data["procStart"], data.get("pidDomain"))


def read_session(file: Path) -> Session | None:
    try:
        data = json.loads(file.read_text())
    except (OSError, ValueError):
        return None
    tmux = data.get("tmux") or ""
    if data.get("kind") != "interactive" or "." not in tmux:
        return None
    pid = int(data.get("pid") or 0)
    if not pid or not _alive(pid) or not _same_process(pid, data):
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


def plain(line: str) -> str:
    return ANSI.sub("", line).rstrip()


class Tmux:
    def capture(self, pane: str) -> list[str]:
        """Screen lines with styling kept, so a dim placeholder can be told from typed text."""
        out = subprocess.run(["tmux", "capture-pane", "-e", "-p", "-t", pane], capture_output=True, text=True,
                             check=True)
        return [line.rstrip() for line in out.stdout.splitlines() if plain(line).strip()]

    def idle_for(self, pane: str) -> float:
        """Seconds since a client showing this pane's window last sent input (inf when nobody is looking)."""
        where = subprocess.run(["tmux", "display", "-p", "-t", pane, "#{window_id}"], capture_output=True, text=True,
                               check=True).stdout.strip()
        out = subprocess.run(["tmux", "list-clients", "-F", "#{client_activity} #{window_id}"], capture_output=True,
                             text=True, check=True).stdout
        seen = [int(a) for a, w in (line.split() for line in out.splitlines() if line.strip()) if w == where]
        return time.time() - max(seen) if seen else float("inf")

    def keys(self, pane: str, *keys: str, literal: bool = False):
        cmd = ["tmux", "send-keys", "-t", pane] + (["-l", "--"] if literal else []) + list(keys)
        subprocess.run(cmd, check=True)


def prompt_state(lines: list[str]) -> str:
    """`empty`, `draft` (typed text, never clobbered) or `menu` (no prompt box, or a menu under it)."""
    text = [plain(line) for line in lines]
    for i in range(len(text) - 2, 0, -1):
        if text[i].startswith("❯") and text[i - 1].startswith(RULE) and text[i + 1].startswith(RULE):
            if any(MENU_FOOTER.search(line) for line in text[i + 1:]):
                return "menu"
            return "empty" if plain(DIM.sub("", lines[i])).strip() == "❯" else "draft"
    return "menu"


def highlighted(lines: list[str]) -> str:
    return next((s for s in (plain(line).strip() for line in reversed(lines)) if s.startswith("❯")), "")


def rc_dialog(lines: list[str]) -> bool:
    text = [plain(line) for line in lines]
    return any(line.strip() == "Remote Control" for line in text) and any("Continue" in line for line in text)


class Repairer:
    def __init__(self, tmux=None, sleep=time.sleep, reread=read_session, settle=1.0, wait=25.0, close_menus=False,
                 active_window=120.0):
        self.tmux, self.sleep, self.reread, self.settle, self.wait = tmux or Tmux(), sleep, reread, settle, wait
        self.close_menus, self.active_window = close_menus, active_window
        self.typed = False

    def _toggle(self, pane: str) -> list[str]:
        self.tmux.keys(pane, "/rc", literal=True)
        self.sleep(self.settle)
        if not any(COMPLETION.search(plain(line)) for line in self.tmux.capture(pane)):
            self.tmux.keys(pane, "Escape")
            self.tmux.keys(pane, "C-u")
            self.sleep(self.settle)
            raise RuntimeError("autocomplete did not offer /remote-control")
        self.tmux.keys(pane, "Enter")
        self.typed = True
        self.sleep(self.settle * 2)
        return self.tmux.capture(pane)

    def _continue_dialog(self, pane: str, lines: list[str]):
        for _ in range(4):
            if highlighted(lines) == "❯ Continue":
                self.tmux.keys(pane, "Enter")
                self.sleep(self.settle * 2)
                return
            self.tmux.keys(pane, "Down")
            self.sleep(self.settle / 2)
            lines = self.tmux.capture(pane)
        self.tmux.keys(pane, "Escape")
        raise RuntimeError("could not select Continue in the Remote Control dialog")

    def _send_rc(self, pane: str):
        """One `/rc`. It connects a disconnected CLI; a connected one opens the dialog, answered with Continue."""
        state = prompt_state(self.tmux.capture(pane))
        if state != "empty":
            raise RuntimeError("a menu or dialog is open" if state == "menu" else "prompt has a draft")
        lines = self._toggle(pane)
        if rc_dialog(lines):
            self._continue_dialog(pane, lines)

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

    def _close_menu(self, pane: str) -> bool:
        lines = self.tmux.capture(pane)
        if prompt_state(lines) != "menu" or not any(MENU_FOOTER.search(plain(line)) for line in lines):
            return False
        self.tmux.keys(pane, "Escape")
        self.typed = True
        self.sleep(self.settle * 2)
        if prompt_state(self.tmux.capture(pane)) != "empty":
            raise RuntimeError("Escape did not bring back an empty prompt")
        return True

    def _reask(self, pane: str):
        self.sleep(self.settle)
        if prompt_state(self.tmux.capture(pane)) != "empty":
            raise RuntimeError("closed a menu but could not ask for it again: the prompt is not empty")
        self.tmux.keys(pane, REASK, literal=True)
        self.sleep(self.settle / 2)
        self.tmux.keys(pane, "Enter")

    def blocker(self, session: Session) -> str | None:
        """Why this session must not be typed into right now, or None."""
        if self.active_window and (idle := self.tmux.idle_for(session.pane)) < self.active_window:
            return f"in use: a client typed into this window {idle:.0f}s ago"
        state = prompt_state(self.tmux.capture(session.pane))
        if session.status == "waiting" and self.close_menus and state == "menu":
            return None
        if session.status != "idle":
            return f"session is {session.status}, not idle"
        if state == "menu" and not self.close_menus:
            return "a menu or dialog is open"
        return "prompt has a draft" if state == "draft" else None

    def repair(self, session: Session, kind: str) -> str:
        self.typed = False
        if reason := self.blocker(session):
            raise RuntimeError(reason)
        closed = self.close_menus and self._close_menu(session.pane)
        try:
            bridge = self._repair(session, kind)
        except RuntimeError as exc:
            if not closed:
                raise
            try:
                self._reask(session.pane)
            except RuntimeError as reask:
                raise RuntimeError(f"{exc}; {reask}") from exc
            raise
        if closed:
            self._reask(session.pane)
        return bridge

    def _repair(self, session: Session, kind: str) -> str:
        state = prompt_state(self.tmux.capture(session.pane))
        if state != "empty":
            raise RuntimeError("a menu or dialog is open" if state == "menu" else "prompt has a draft")
        old = session.bridge
        if kind == "reconnect":
            self._disconnect_from_dialog(session.pane, self._toggle(session.pane))
        self._send_rc(session.pane)
        try:
            self._send_rc(session.pane)  # one /rc does not always take; a second is harmless once connected
        except RuntimeError as exc:
            log.warning("second /rc into %s: %s", session.pane, exc)
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


def safe_health(health_fn=health_now) -> dict[str, bool] | None:
    try:
        return health_fn()
    except Exception as exc:  # an unreachable HQ must not stop connecting sessions that have no bridge at all
        log.warning("bridge health unavailable (%s); only sessions without a bridge are repaired", exc)
        return None


def diagnose_all(health: dict[str, bool] | None, sessions: list[Session]) -> dict[int, tuple[Session, str | None]]:
    rows = {}
    for s in sessions:
        kind = diagnose(s, health or {})
        rows[s.pid] = (s, None if health is None and kind == "reconnect" else kind)
    return rows


def unhealthy(health_fn=health_now, sessions_fn=local_sessions) -> dict[int, tuple[Session, str]]:
    return {pid: row for pid, row in diagnose_all(safe_health(health_fn), sessions_fn()).items() if row[1]}


GAVE_UP = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "cc-rc-watchdog/gave-up.json"


def _load_gave_up(path: Path) -> set[int]:
    try:
        return {pid for pid in json.loads(path.read_text()) if _alive(pid)}
    except (OSError, ValueError):
        return set()


def run_once(health_fn=health_now, sessions_fn=local_sessions, repairer=None, confirm_after=30.0,
             sleep=time.sleep, dry_run=False, gave_up_file: Path = GAVE_UP, report_all=False,
             renew=False) -> list[dict]:
    """Repair what stays unhealthy across two checks; `report_all` also lists the sessions left alone.

    `renew` also reconnects healthy bridges: Remote Control takes its name only when a bridge registers, so this is
    how a /rename or a newer conversation title reaches the app.
    """
    gave_up = _load_gave_up(gave_up_file)
    first = unhealthy(health_fn, sessions_fn)
    if first and confirm_after:
        sleep(confirm_after)
    current = diagnose_all(safe_health(health_fn), sessions_fn()) if first or report_all or renew else {}
    repairer = repairer or Repairer()
    results = []
    for pid, (session, kind) in current.items():
        if renew and not kind and session.bridge:
            kind, first[pid] = "reconnect", (session, "reconnect")
        row = {"pid": pid, "pane": session.pane, "name": session.name, "kind": kind}
        if not kind or pid not in first:
            if report_all:
                results.append({**row, "result": "connected" if session.bridge else "skipped: not confirmed"})
            continue
        if pid in gave_up:
            results.append({**row, "result": "skipped: an earlier repair failed in this process"})
            continue
        if dry_run:
            try:
                reason = repairer.blocker(session)
            except subprocess.CalledProcessError:
                reason = "tmux pane unreadable"
            results.append({**row, "result": f"dry-run: would skip: {reason}" if reason else "dry-run: would repair"})
            continue
        try:
            row["result"] = "repaired"
            row["bridge"] = repairer.repair(session, kind)
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            row["result"] = f"skipped: {exc}"
            if repairer.typed:
                gave_up.add(pid)
        results.append(row)
    if not dry_run:
        gave_up_file.parent.mkdir(parents=True, exist_ok=True)
        gave_up_file.write_text(json.dumps(sorted(gave_up)))
    return results


def peer_command(flags: list[str]) -> str:
    """The same run on a peer, from the env file its timer reads (`CC_RC_WATCHDOG_FROM` pins the release)."""
    return (f"set -a; . {ENV_FILE}; set +a; "
            f'exec ~/.local/bin/uvx -q --from "$CC_RC_WATCHDOG_FROM" cc-rc-watchdog --now --peers= {" ".join(flags)}')


def run_peer(host: str, flags: list[str], run=subprocess.run) -> list[dict]:
    try:
        out = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, peer_command(flags)],
                  capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return [{"host": host, "result": "failed: timed out"}]
    rows = []
    for line in out.stdout.splitlines():
        try:
            rows.append({"host": host, **json.loads(line)})
        except ValueError:
            continue
    if out.returncode:
        rows.append({"host": host, "result": f"failed: exit {out.returncode}: {out.stderr.strip()[-300:]}"})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report what would be repaired")
    parser.add_argument("--confirm-after", type=float, default=30.0,
                        help="seconds a session must stay unhealthy before repair")
    parser.add_argument("--now", action="store_true",
                        help="repair now (no confirmation wait) and report every session, including those skipped")
    parser.add_argument("--active-seconds", type=float, default=120.0,
                        help="skip a pane whose window got client input this recently (default 120; 0 disables)")
    parser.add_argument("--force", action="store_true", help="also type into panes the owner is using right now")
    parser.add_argument("--close-menus", action="store_true",
                        help="Escape an open menu in a waiting (never busy) session, repair, then ask Claude to "
                             "show the menu again")
    parser.add_argument("--renew-names", action="store_true",
                        help="also reconnect healthy sessions so the app shows each one's current title")
    parser.add_argument("--peers", default=os.environ.get("CC_RC_WATCHDOG_PEERS", ""),
                        help="with --now: comma-separated ssh aliases to run the same on (default $CC_RC_WATCHDOG_PEERS)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    GAVE_UP.parent.mkdir(parents=True, exist_ok=True)
    lock = open(GAVE_UP.parent / "lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)  # the timer and a manual --now must never type into the same pane at once
    active = 0 if args.force else args.active_seconds
    rows = run_once(confirm_after=0 if args.now else args.confirm_after, dry_run=args.dry_run,
                    report_all=args.now, renew=args.renew_names,
                    repairer=Repairer(close_menus=args.close_menus, active_window=active))
    host = os.uname().nodename
    rows = [{"host": host, **row} for row in rows]
    if args.now:
        flags = [f for f, on in (("--dry-run", args.dry_run), ("--close-menus", args.close_menus),
                                 ("--force", args.force), ("--renew-names", args.renew_names)) if on]
        flags.append(f"--active-seconds={args.active_seconds:g}")
        for peer in filter(None, (p.strip() for p in args.peers.split(","))):
            rows += run_peer(peer, flags)
    for row in rows:
        print(json.dumps(row), flush=True)
