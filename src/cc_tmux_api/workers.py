"""
tmux worker layer — spawn, observe, steer, and revive Claude Code sessions.

Design (deliberately minimal):
- The tmux server IS the live registry: a worker is any session named with
  PREFIX; its title lives in the tmux user option @hq_title. No database.
- Shell-first spawn: the session starts as a plain shell and the claude
  command is typed into it, so when claude exits the pane (and its output)
  survives for review instead of the session dying.
- Sessions without the PREFIX are a disjoint namespace and are never touched;
  they only appear read-only in list_others().
- Send mechanics per Tmux-Orchestrator's send-claude-message.sh:
  literal text, 0.5s for the UI to register, then Enter.
- State classification: vendor_amux._detect_claude_status (see that file),
  upgraded by session-jsonl freshness (see status_of).
- A persistent per-session registry under $BCE_WORKSPACE_DIR/workspace/<sid>/
  survives reboots; resume_registered() revives non-tombstoned sessions with
  `claude --resume` under the SAME tmux name, so remote-control web URLs and
  any client tracking the session name reconnect unchanged.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from .vendor_amux import _detect_claude_status

PREFIX = os.environ.get("CC_TMUX_PREFIX", "hq-")
STATUS_STYLE = "bg=colour208,fg=colour232"  # orange = automation worker
PROMPT_DIR = Path("/tmp/cc-tmux-api-prompts")
DEFAULT_WORKDIR = os.environ.get("CC_TMUX_WORKDIR", str(Path.home()))
SHELLS = {"zsh", "bash", "sh", "fish"}

# Persistent per-session registry (survives reboot — the tmux server doesn't).
# BCE_WORKSPACE_DIR is the BASE dir (default ~/.basecamp-enhancer); each task
# session gets $BASE/workspace/<claude-session-uuid>/ holding meta.json, a
# .done tombstone when finished, and whatever working files the agent writes
# (its prompt names this dir as its scratch space). resume_registered() revives
# from here after a reboot.
BASE_DIR = Path(os.environ.get("BCE_WORKSPACE_DIR",
                               str(Path.home() / ".basecamp-enhancer")))
SESSIONS_DIR = BASE_DIR / "workspace"
RESUME_MAX_DAYS = 5   # never revive anything older
GC_DONE_DAYS = 14     # tombstoned session dirs older than this are deleted

# A worker mid-turn appends to its session jsonl constantly (assistant deltas,
# tool events), so a fresh mtime = actually doing work — even when the tmux pane
# shows scrolling tool output with no spinner and the pane classifier reads
# "idle"/"unknown". This is the local activity feed; no Claude Code hooks needed.
JSONL_FRESH_S = 60

# Workers run unattended in tmux — no human answers in real time. An interactive
# prompt (AskUserQuestion menu, "shall I proceed?") blocks the session FOREVER.
# Two guards: hard-disable the question tool, and tell the model to assume+proceed.
BLOCKED_TOOLS = "AskUserQuestion"
HEADLESS_SYSTEM_PROMPT = (
    "You are running UNATTENDED in a background tmux session. No human is watching "
    "in real time, so you MUST NOT ask questions, present option menus, or wait for "
    "approval — doing so hangs you forever with nobody to answer. NEVER end your turn "
    "with a question or an offer ('want me to X or Y?', 'shall I proceed?'): that IS "
    "asking-and-waiting. When you would normally ask, instead pick the most reasonable "
    "option, state the assumption you made, and DO it. Stay strictly on the task you "
    "were given — if you notice an unrelated problem, note it in one line in your "
    "output and keep going; do NOT wander off to fix or propose other work. Only if you "
    "are genuinely blocked on the ASSIGNED task (missing secret, or an "
    "irreversible/approval-gated action per the project's rules) do you stop — and then "
    "you write your blocker and exactly what you need as your final message and stop, "
    "never a question that waits. You may be steered by messages typed into the session "
    "later, but never block waiting for one."
)
HEADLESS_PROMPT_NOTE = (
    "[Unattended background session — no human is watching. Do NOT ask questions or "
    "end your turn with an offer/question; assume the best option and DO it, staying "
    "strictly on this task (note unrelated issues in one line, don't chase them). If "
    "truly blocked on THIS task, stop with a one-line blocker as your final message — "
    "never a question that waits. Then do the task below.]"
)


def _tmux(*args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout)


def _sessions() -> list[dict]:
    r = _tmux("list-sessions", "-F", "#{session_name}\t#{session_created}\t#{session_attached}")
    if r.returncode != 0:  # no tmux server running
        return []
    out = []
    for line in r.stdout.splitlines():
        name, created, attached = line.split("\t")
        out.append({"name": name, "created": int(created), "attached": attached == "1"})
    return out


def capture(session: str, lines: int = 60) -> str:
    r = _tmux("capture-pane", "-t", session, "-p", "-S", f"-{lines}")
    return r.stdout if r.returncode == 0 else ""


def _session_opts(session: str) -> tuple[str, str]:
    """(claude session uuid, workdir) from the tmux user options; '' if unset."""
    sid = _tmux("show-option", "-t", session, "-qv", "@hq_sid").stdout.strip()
    wd = _tmux("show-option", "-t", session, "-qv", "@hq_workdir").stdout.strip()
    return sid, wd


def _jsonl_path(sid: str, workdir: str) -> Path:
    return (Path.home() / ".claude" / "projects"
            / re.sub(r"[/.]", "-", workdir) / f"{sid}.jsonl")


def _jsonl_age_s(session: str) -> int | None:
    """Seconds since the session's jsonl was last appended (None = no jsonl)."""
    sid, wd = _session_opts(session)
    if not sid or not wd:
        return None
    try:
        return max(0, int(time.time() - _jsonl_path(sid, wd).stat().st_mtime))
    except OSError:
        return None


def status_of(session: str, jsonl_age: int | None = None) -> str:
    """'working' | 'waiting' | 'idle' | 'done' (claude exited) | 'unknown'

    Pane text is the primary classifier, but it under-reports "working": long
    tool runs scroll the spinner out of the scan window. A fresh jsonl proves
    the session is mid-turn, so it upgrades idle/unknown -> working ('waiting'
    is kept — an approval prompt is pane-authoritative, and 'done' means the
    pane fell back to a shell, which no jsonl write can contradict)."""
    r = _tmux("display-message", "-t", session, "-p", "#{pane_current_command}")
    cmd = r.stdout.strip().lower()
    if r.returncode == 0 and cmd in SHELLS:
        return "done"  # back at the shell -> claude exited, output kept
    st = _detect_claude_status(capture(session, 40))
    status = {"active": "working", "waiting": "waiting", "idle": "idle"}.get(st, "unknown")
    if status in ("idle", "unknown"):
        age = jsonl_age if jsonl_age is not None else _jsonl_age_s(session)
        if age is not None and age < JSONL_FRESH_S:
            return "working"
    return status


def send_text(session: str, text: str) -> None:
    """Tmux-Orchestrator pattern: literal text, 0.5s, then Enter."""
    _tmux("send-keys", "-t", session, "-l", "--", text)
    time.sleep(0.5)
    _tmux("send-keys", "-t", session, "Enter")


def spawn(title: str, prompt: str, workdir: str | None = None, kind: str = "task") -> dict:
    workdir = workdir or DEFAULT_WORKDIR
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:24] or "task"
    n = 1
    existing = {s["name"] for s in _sessions()}
    while f"{PREFIX}{slug}-{n}" in existing:
        n += 1
    session = f"{PREFIX}{slug}-{n}"

    # pre-assign the claude session UUID so the jsonl path is known from birth
    # (-> _web_url can find the remote-control bridge id once it connects)
    sid = str(uuid.uuid4())

    # task workers get a persistent registry entry so a reboot can revive them
    # (+ the agent is told to tombstone itself when truly finished). Periodic
    # "routine" workers are disposable — the next tick respawns them — so they
    # are never registered.
    done_note = ""
    if kind == "task":
        reg = SESSIONS_DIR / sid
        reg.mkdir(parents=True, exist_ok=True)
        (reg / "meta.json").write_text(json.dumps({
            "session": session, "sid": sid, "title": title, "workdir": workdir,
            "kind": kind, "created": int(time.time()),
            # loop watchers get nudged ("continue") when revived after a reboot;
            # the /loop directive in the prompt is the source of truth
            "loop": bool(re.search(r"/loop\s", prompt)),
        }, indent=2), encoding="utf-8")
        done_note = (
            f"\n\n[Your scratch folder is {reg} — write any working files/outputs "
            f"there. When your assigned task is FULLY complete — nothing left to "
            f"do, watch, or loop on — run: touch {reg / '.done'} . That tells the "
            f"spawner this session is finished and must not be revived after a "
            f"reboot. If you are watching on a loop, do NOT write it; a looping "
            f"watch is never finished until a human closes it.]"
        )

    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    pf = PROMPT_DIR / f"{session}.md"
    # headless note in the visible prompt too (belt-and-suspenders with the
    # --append-system-prompt), so it's right there in the worker's conversation
    pf.write_text(f"{HEADLESS_PROMPT_NOTE}\n\n{prompt}{done_note}", encoding="utf-8")
    sf = PROMPT_DIR / "_headless.txt"  # stable, shared by all workers
    sf.write_text(HEADLESS_SYSTEM_PROMPT, encoding="utf-8")

    r = _tmux("new-session", "-d", "-s", session, "-c", workdir)
    if r.returncode != 0:
        raise RuntimeError(f"tmux new-session failed: {r.stderr.strip()}")
    _tmux("set-option", "-t", session, "@hq_title", title)
    _tmux("set-option", "-t", session, "@hq_sid", sid)
    _tmux("set-option", "-t", session, "@hq_workdir", workdir)
    _tmux("set-option", "-t", session, "status-style", STATUS_STYLE)
    # shell-first: type the claude command into the shell so the pane survives
    # claude's exit; prompts via file to dodge quoting. --disallowedTools +
    # --append-system-prompt keep the unattended worker from ever blocking on a
    # prompt. --remote-control (named after the tmux session, and NEVER bare —
    # its optional [name] would swallow the next token) lets the session be
    # opened/steered from claude.ai; --session-id pins the local jsonl.
    send_text(session, f'claude --dangerously-skip-permissions '
                       f'--disallowedTools {BLOCKED_TOOLS} '
                       f'--session-id {sid} '
                       f'--remote-control {session} '
                       f'--append-system-prompt "$(cat {sf})" "$(cat {pf})"')

    return {"session": session, "title": title, "workdir": workdir,
            "claude_session_id": sid}


def kill(session: str) -> None:
    if not session.startswith(PREFIX):
        raise ValueError(f"refusing to kill a non-{PREFIX} session")
    # a human/API kill is a deliberate "this is finished" — tombstone the
    # registry entry so the session is never revived after a reboot
    sid, _ = _session_opts(session)
    if sid and (SESSIONS_DIR / sid).is_dir():
        (SESSIONS_DIR / sid / ".done").touch()
    _tmux("kill-session", "-t", session)
    (PROMPT_DIR / f"{session}.md").unlink(missing_ok=True)


_WEB_URL_CACHE: dict[str, str] = {}  # session -> claude.ai URL (stable once found)


def _web_url(session: str) -> str | None:
    """claude.ai/code URL for a worker, once its remote-control bridge connects.
    The bridge id (bridgeSessionId: cse_<token>) lands in the session's jsonl,
    whose path we know because spawn() pre-assigned --session-id (@hq_sid)."""
    if session in _WEB_URL_CACHE:
        return _WEB_URL_CACHE[session]
    sid, wd = _session_opts(session)
    if not sid or not wd:
        return None
    f = _jsonl_path(sid, wd)
    token = None
    try:
        with f.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:  # last one wins (bridge reconnects supersede)
                if '"bridgeSessionId"' in line:
                    m = re.search(r'"bridgeSessionId"\s*:\s*"cse_([^"]+)"', line)
                    if m:
                        token = m.group(1)
    except OSError:
        return None
    if not token:
        return None
    url = f"https://claude.ai/code/session_{token}"
    _WEB_URL_CACHE[session] = url
    return url


def list_workers(tail_lines: int = 14) -> list[dict]:
    out = []
    for s in sorted((x for x in _sessions() if x["name"].startswith(PREFIX)),
                    key=lambda x: x["created"], reverse=True):
        name = s["name"]
        title = _tmux("show-option", "-t", name, "-qv", "@hq_title").stdout.strip()
        sid, wd = _session_opts(name)
        jsonl_age = _jsonl_age_s(name)
        out.append({
            "session": name,
            "title": title or name,
            "status": status_of(name, jsonl_age),
            "attached": s["attached"],
            "age_s": int(time.time()) - s["created"],
            "tail": capture(name, tail_lines).rstrip(),
            "web_url": _web_url(name),
            # observability for clients (busy spinners etc.): jsonl_age_s =
            # seconds since the session's event log last grew — the ground
            # truth for "actually doing something right now"
            "jsonl_age_s": jsonl_age,
            "claude_session_id": sid or None,
            "workdir": wd or None,
        })
    return out


def resume_registered() -> list[str]:
    """Revive registered task workers whose tmux session is gone (reboot).

    Called once on server startup. Revive = recreate the tmux session and
    `claude --resume <sid>` — the conversation and its claude.ai web URL come
    back live. One-shot sessions then sit idle until steered; LOOP watchers
    (meta.loop) additionally get a literal "continue" typed into their pane
    (after claude has had time to boot) so the watch actually resumes. Skips
    .done-tombstoned entries and anything older than RESUME_MAX_DAYS."""
    if not SESSIONS_DIR.is_dir():
        return []
    live = {s["name"] for s in _sessions()}
    cutoff = time.time() - RESUME_MAX_DAYS * 86400
    revived, loops = [], []
    for meta_f in sorted(SESSIONS_DIR.glob("*/meta.json")):
        reg = meta_f.parent
        if (reg / ".done").exists():
            continue
        try:
            meta = json.loads(meta_f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        session, sid = meta.get("session"), meta.get("sid")
        wd = meta.get("workdir") or DEFAULT_WORKDIR
        if not session or not sid or session in live:
            continue
        if meta.get("created", 0) < cutoff:
            continue  # too old to auto-revive; still listed in the registry
        if not _jsonl_path(sid, wd).exists():
            continue  # nothing to resume from
        r = _tmux("new-session", "-d", "-s", session, "-c", wd)
        if r.returncode != 0:
            continue
        _tmux("set-option", "-t", session, "@hq_title", meta.get("title", session))
        _tmux("set-option", "-t", session, "@hq_sid", sid)
        _tmux("set-option", "-t", session, "@hq_workdir", wd)
        _tmux("set-option", "-t", session, "status-style", STATUS_STYLE)
        # /tmp is wiped on reboot — recreate the shared headless system prompt
        PROMPT_DIR.mkdir(parents=True, exist_ok=True)
        sf = PROMPT_DIR / "_headless.txt"
        sf.write_text(HEADLESS_SYSTEM_PROMPT, encoding="utf-8")
        send_text(session, f'claude --dangerously-skip-permissions '
                           f'--disallowedTools {BLOCKED_TOOLS} '
                           f'--resume {sid} '
                           f'--remote-control {session} '
                           f'--append-system-prompt "$(cat {sf})"')
        revived.append(session)
        if meta.get("loop"):
            loops.append(session)
        time.sleep(2)  # stagger, don't slam N claude launches at once
    if loops:
        time.sleep(10)  # let claude boot before the nudge lands in its input
        for session in loops:
            send_text(session, "continue")
    return revived


def registry_gc() -> list[str]:
    """Delete tombstoned (.done) session dirs older than GC_DONE_DAYS."""
    import shutil

    if not SESSIONS_DIR.is_dir():
        return []
    cutoff = time.time() - GC_DONE_DAYS * 86400
    removed = []
    for done in SESSIONS_DIR.glob("*/.done"):
        try:
            if done.stat().st_mtime < cutoff:
                shutil.rmtree(done.parent, ignore_errors=True)
                removed.append(done.parent.name)
        except OSError:
            continue
    return removed


def list_others() -> list[dict]:
    """Read-only view of non-PREFIX tmux sessions (never touched)."""
    out = []
    for s in (x for x in _sessions() if not x["name"].startswith(PREFIX)):
        r = _tmux("display-message", "-t", s["name"], "-p", "#{pane_current_command}")
        out.append({"session": s["name"], "attached": s["attached"],
                    "command": r.stdout.strip()})
    return out
