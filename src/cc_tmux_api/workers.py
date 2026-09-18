"""
tmux worker layer — spawn, observe, steer, and revive Claude Code sessions.

Design (deliberately minimal):
- The tmux server IS the live registry: a worker is any session named with
  PREFIX; its title lives in the tmux user option @hq_title. No database.
- Shell-first spawn: the session starts as a plain shell and the claude
  command is typed into it, so when claude exits the pane (and its output)
  survives for review instead of the session dying.
- Sessions without the PREFIX are a disjoint namespace and are never touched;
  they only appear read-only in list_others(). Ownership additionally honours
  the @hq_owner stamp (see _mine), so two instances with NESTED prefixes
  (e.g. "hq-" and "hq-work-b-") stay disjoint too.
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
import shutil
import subprocess
import threading
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


def _mine(name: str, owner: str) -> bool:
    """Ownership predicate. A session is ours iff its name carries our PREFIX
    and, when it is stamped with an owner prefix (@hq_owner, set at spawn and
    revive), that stamp is exactly ours. The stamp disambiguates NESTED
    prefixes — two instances on "hq-" and "hq-work-b-": plain startswith would
    make the shorter-prefix instance claim (list, reap, kill) the longer one's
    sessions. Unstamped sessions (spawned before the stamp existed) fall back
    to plain startswith, so an upgrade never orphans live workers."""
    return name.startswith(PREFIX) and owner in ("", PREFIX)


def _sessions() -> list[dict]:
    r = _tmux("list-sessions",
              "-F", "#{session_name}\t#{session_created}\t#{session_attached}\t#{@hq_owner}")
    if r.returncode != 0:  # no tmux server running
        return []
    out = []
    for line in r.stdout.splitlines():
        name, created, attached, owner = line.split("\t")
        out.append({"name": name, "created": int(created), "attached": attached == "1",
                    "owner": owner})
    return out


def _sessions_full() -> list[dict]:
    """_sessions() plus the @hq_* user options, in ONE tmux call — tmux formats
    expand user options, so per-session show-option round-trips are unnecessary.
    Title goes LAST with a bounded split: it's the only field that could ever
    contain a tab (it comes from user input)."""
    r = _tmux("list-sessions", "-F",
              "#{session_name}\t#{session_created}\t#{session_attached}"
              "\t#{@hq_kind}\t#{@hq_sid}\t#{@hq_workdir}\t#{@hq_owner}\t#{@hq_title}")
    if r.returncode != 0:  # no tmux server running
        return []
    out = []
    for line in r.stdout.splitlines():
        name, created, attached, kind, sid, wd, owner, title = line.split("\t", 7)
        out.append({"name": name, "created": int(created), "attached": attached == "1",
                    "kind": kind, "sid": sid, "workdir": wd, "owner": owner,
                    "title": title})
    return out


def _pane_cmds() -> dict[str, str]:
    """session name -> current pane command, in ONE tmux call (vs a
    display-message per session)."""
    r = _tmux("list-panes", "-a", "-F", "#{session_name}\t#{pane_current_command}")
    out: dict[str, str] = {}
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            name, _, cmd = line.partition("\t")
            out.setdefault(name, cmd)
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


def _jsonl_age(sid: str, workdir: str) -> int | None:
    """Seconds since the session's jsonl was last appended (None = no jsonl)."""
    if not sid or not workdir:
        return None
    try:
        return max(0, int(time.time() - _jsonl_path(sid, workdir).stat().st_mtime))
    except OSError:
        return None


def _jsonl_age_s(session: str) -> int | None:
    return _jsonl_age(*_session_opts(session))


def _classify(cmd: str, pane_text: str, jsonl_age: int | None) -> str:
    """'working' | 'waiting' | 'idle' | 'done' (claude exited) | 'unknown'

    Pane text is the primary classifier, but it under-reports "working": long
    tool runs scroll the spinner out of the scan window. A fresh jsonl proves
    the session is mid-turn, so it upgrades idle/unknown -> working ('waiting'
    is kept — an approval prompt is pane-authoritative, and 'done' means the
    pane fell back to a shell, which no jsonl write can contradict)."""
    if cmd in SHELLS:
        return "done"  # back at the shell -> claude exited, output kept
    st = _detect_claude_status(pane_text)
    status = {"active": "working", "waiting": "waiting", "idle": "idle"}.get(st, "unknown")
    if status in ("idle", "unknown") and jsonl_age is not None and jsonl_age < JSONL_FRESH_S:
        return "working"
    return status


def status_of(session: str, jsonl_age: int | None = None) -> str:
    r = _tmux("display-message", "-t", session, "-p", "#{pane_current_command}")
    cmd = r.stdout.strip().lower() if r.returncode == 0 else ""
    if jsonl_age is None:
        jsonl_age = _jsonl_age_s(session)
    return _classify(cmd, capture(session, 40), jsonl_age)


def send_text(session: str, text: str) -> None:
    """Tmux-Orchestrator pattern: literal text, 0.5s, then Enter."""
    _tmux("send-keys", "-t", session, "-l", "--", text)
    time.sleep(0.5)
    _tmux("send-keys", "-t", session, "Enter")


def oneshot(prompt: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a prompt headlessly (`claude -p`) and return the completed process —
    the ephemeral sibling of spawn(): no tmux, no registry, output = stdout."""
    claude = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude")
    return subprocess.run([claude, "-p", prompt],
                          capture_output=True, text=True, timeout=timeout)


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
    _tmux("set-option", "-t", session, "@hq_kind", kind)
    _tmux("set-option", "-t", session, "@hq_owner", PREFIX)
    _tmux("set-option", "-t", session, "status-style", STATUS_STYLE)
    # shell-first so prompts can come from files (dodges quoting), but the
    # command ends in `; exit`: when claude exits the shell exits and tmux
    # closes the session. A lingering shell pane read as a live worker to the
    # routines overlap guard (2026-09-18) and piled up in the listing; the
    # conversation itself is durable in the jsonl (--session-id below), so
    # nothing is lost. --disallowedTools +
    # --append-system-prompt keep the unattended worker from ever blocking on a
    # prompt. --remote-control (named after the tmux session, and NEVER bare —
    # its optional [name] would swallow the next token) lets the session be
    # opened/steered from claude.ai; --session-id pins the local jsonl.
    send_text(session, f'claude --dangerously-skip-permissions '
                       f'--disallowedTools {BLOCKED_TOOLS} '
                       f'--session-id {sid} '
                       f'--remote-control {session} '
                       f'--append-system-prompt "$(cat {sf})" "$(cat {pf})"; exit')

    _invalidate_list_cache()  # the new worker must show up on the next poll
    return {"session": session, "title": title, "workdir": workdir,
            "claude_session_id": sid}


def kill(session: str) -> None:
    owner = _tmux("show-option", "-t", session, "-qv", "@hq_owner").stdout.strip()
    if not _mine(session, owner):
        raise ValueError(f"refusing to kill a session not owned by this "
                         f"instance (prefix {PREFIX})")
    # a human/API kill is a deliberate "this is finished" — tombstone the
    # registry entry so the session is never revived after a reboot
    sid, _ = _session_opts(session)
    if sid and (SESSIONS_DIR / sid).is_dir():
        (SESSIONS_DIR / sid / ".done").touch()
    _tmux("kill-session", "-t", session)
    (PROMPT_DIR / f"{session}.md").unlink(missing_ok=True)
    _invalidate_list_cache()


_WEB_URL_CACHE: dict[str, str] = {}  # session -> claude.ai URL (stable once found)


def _web_url(session: str, sid: str | None = None, wd: str | None = None) -> str | None:
    """claude.ai/code URL for a worker, once its remote-control bridge connects.
    The bridge id (bridgeSessionId: cse_<token>) lands in the session's jsonl,
    whose path we know because spawn() pre-assigned --session-id (@hq_sid).
    Pass sid/wd when already known to skip the tmux option lookups."""
    if session in _WEB_URL_CACHE:
        return _WEB_URL_CACHE[session]
    if sid is None or wd is None:
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


# list_workers is the polling hot path (browser extensions + dashboards hit it
# every few seconds, often several at once) while the tmux server processes
# commands serially and can stall for seconds under heavy pane output. Two
# defenses: batch the per-worker tmux round-trips down to ONE capture-pane each
# (options come from _sessions_full, pane commands from _pane_cmds), and a
# short TTL cache so concurrent pollers coalesce into one rebuild. On a tmux
# stall mid-rebuild the last snapshot is served stale instead of erroring —
# a slightly old list beats a 500.
_LIST_TTL_S = 2.0
_list_lock = threading.Lock()
_list_cache: dict = {"t": 0.0, "tail_lines": None, "workers": None}


def _list_workers_uncached(tail_lines: int) -> list[dict]:
    cmds = _pane_cmds()
    out = []
    for s in sorted((x for x in _sessions_full() if _mine(x["name"], x["owner"])),
                    key=lambda x: x["created"], reverse=True):
        name = s["name"]
        jsonl_age = _jsonl_age(s["sid"], s["workdir"])
        try:
            pane = capture(name, max(40, tail_lines))
            status = _classify(cmds.get(name, "").strip().lower(), pane, jsonl_age)
            tail = "\n".join(pane.splitlines()[-tail_lines:]).rstrip()
        except subprocess.TimeoutExpired:
            status, tail = "unknown", ""  # one stuck pane must not 500 the list
        out.append({
            "session": name,
            "title": s["title"] or name,
            "kind": s["kind"] or "task",
            "status": status,
            "attached": s["attached"],
            "age_s": int(time.time()) - s["created"],
            "tail": tail,
            "web_url": _web_url(name, s["sid"], s["workdir"]),
            # observability for clients (busy spinners etc.): jsonl_age_s =
            # seconds since the session's event log last grew — the ground
            # truth for "actually doing something right now"
            "jsonl_age_s": jsonl_age,
            "claude_session_id": s["sid"] or None,
            "workdir": s["workdir"] or None,
        })
    return out


def list_workers(tail_lines: int = 14) -> list[dict]:
    with _list_lock:
        c = _list_cache
        fresh = time.time() - c["t"] < _LIST_TTL_S and c["tail_lines"] == tail_lines
        if not fresh:
            try:
                c["workers"] = _list_workers_uncached(tail_lines)
                c["t"], c["tail_lines"] = time.time(), tail_lines
            except subprocess.TimeoutExpired:
                if c["workers"] is None or c["tail_lines"] != tail_lines:
                    raise  # nothing usable to fall back to
        return c["workers"]


def _invalidate_list_cache() -> None:
    _list_cache["t"] = 0.0


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
        _tmux("set-option", "-t", session, "@hq_kind", meta.get("kind", "task"))
        _tmux("set-option", "-t", session, "@hq_owner", PREFIX)
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


def reap_stale(kind: str, max_age_hours: float) -> list[str]:
    """Kill workers of the given kind (@hq_kind) older than max_age_hours and
    return their names. Meant for disposable periodic workers whose next tick
    respawns them; long-lived kinds should simply never be passed here.
    Sessions without @hq_kind are treated as "task" and never match "routine"."""
    cutoff = time.time() - max_age_hours * 3600
    reaped = []
    for s in _sessions():
        name = s["name"]
        if not _mine(name, s["owner"]) or s["created"] >= cutoff:
            continue
        k = _tmux("show-option", "-t", name, "-qv", "@hq_kind").stdout.strip() or "task"
        if k == kind:
            kill(name)
            reaped.append(name)
    return reaped


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
    """Read-only view of tmux sessions this instance does not own (never
    touched) — including another instance's workers under a nested prefix."""
    cmds = _pane_cmds()
    return [{"session": s["name"], "attached": s["attached"],
             "command": cmds.get(s["name"], "")}
            for s in _sessions() if not _mine(s["name"], s["owner"])]
