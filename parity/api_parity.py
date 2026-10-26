#!/usr/bin/env python3
"""Parity harness: Python cc-tmux-api (OLD) vs Rust cc-tmux-api (NEW).

Both servers run side by side on random localhost ports against ONE scratch tmux
server (`tmux -L <socket> -f /dev/null` via a PATH wrapper — never the default
server, no user hooks/plugins), a scratch HOME, and a fake `claude`. Every case
sends the same request to both and compares status + normalized JSON (or bytes).

    OLD="uv run --project <repo> cc-tmux-api" NEW=rust/target/release/cc-tmux-api \
        python3 parity/api_parity.py

Known, intentional differences are listed in KNOWN_DIFFS and reported, not failed.
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OLD = shlex.split(os.environ.get("OLD", f"{Path.home()}/Projects/cc-tmux-api/.venv/bin/cc-tmux-api"))
NEW = shlex.split(os.environ.get("NEW", str(REPO / "rust/target/release/cc-tmux-api")))
REAL_TMUX = shutil.which("tmux")
SOCKET = f"hq-parity-{os.getpid()}"
ROOT = Path(tempfile.mkdtemp(prefix="hq-parity-")).resolve()
TOKEN = "p" * 40
VOLATILE = {"updated_at", "last_seen", "age_s", "jsonl_age_s", "ended_at", "claude_session_id", "file"}
KNOWN_DIFFS = {
    "GET /api/push": "push delivery not ported: Rust reports available=false",
}
results = {"pass": 0, "fail": 0, "known": 0}
procs = []


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def setup():
    bin_dir = ROOT / "bin"
    bin_dir.mkdir()
    (bin_dir / "tmux").write_text(f'#!/bin/sh\nexec {REAL_TMUX} -L {SOCKET} -f /dev/null "$@"\n')
    shutil.copy(HERE / "fake_claude.sh", bin_dir / "claude")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    home = ROOT / "home"
    (home / ".claude/projects").mkdir(parents=True)
    (ROOT / "work/proj").mkdir(parents=True)
    os.environ.update(PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(home), SHELL="/bin/sh")
    os.environ.pop("TMUX", None)
    for side in ("old", "new"):
        rdir = ROOT / side / "routines"
        rdir.mkdir(parents=True)
        (rdir / "every.md").write_text("---\ntitle: Every half hour\nevery: 30m\nenabled: true\n---\nsay hi\n")
        (rdir / "daily.md").write_text("---\ntitle: Morning\nevery: daily@07:00\ndays: mon,Tue, wed\nenabled: false\n---\nbrief\n")
        (rdir / "yaml11.md").write_text("---\ntitle: 42\nevery: 2h\nenabled: yes\nworkdir: /tmp\n---\nyaml one-one\n")
        (rdir / "nofront.md").write_text("no frontmatter here\n")
        (rdir / "noenabled.md").write_text("---\ntitle: Toggle me\nevery: 1h\n---\nbody\n")
    # pre-existing sessions: a human Claude session, a plain shell, a foreign worker prefix
    tmux("new-session", "-d", "-s", "human-1", "-c", str(ROOT / "work/proj"), "-x", "120", "-y", "40", "claude --session-id 11111111-2222-4333-8444-555555555555")
    # pane shells must not be login shells: /etc/profile would reset PATH and hide the fake claude
    tmux("set-option", "-g", "default-command", "/bin/sh")
    tmux("new-session", "-d", "-s", "plain", "-c", str(ROOT / "work"))
    tmux("new-session", "-d", "-s", "hq-other-1", "-c", str(ROOT / "work"))
    tmux("set-option", "-t", "hq-other-1", "@hq_owner", "hq-other-")
    time.sleep(1)


def env_for(side, port, manage="0", node="parity-node", hosts=None, token=TOKEN):
    env = dict(os.environ)
    env.update(
        CC_TMUX_PORT=str(port), CC_TMUX_BIND="127.0.0.1", CC_TMUX_TOKEN=token,
        CC_TMUX_ALLOWED_HOSTS="hq.parity.test", CC_TMUX_NODE_ID=node,
        CC_TMUX_STATE_DIR=str(ROOT / side / "state"), BCE_WORKSPACE_DIR=str(ROOT / side / "bce"),
        CC_TMUX_ROUTINES_DIR=str(ROOT / side / "routines"), CC_TMUX_MANAGE_WORKERS=manage,
        CC_TMUX_WORKDIR=str(ROOT / "work"),
        CC_TMUX_HOSTS=json.dumps(hosts or [{"id": "local", "label": "Local"}, {"id": "peer", "label": "Peer", "url": "http://127.0.0.1:9"}]),
    )
    env.pop("CC_TMUX_NATIVE_TOKEN", None)
    return env


def start(side, cmd, manage="0", **kw):
    port = free_port()
    log = open(ROOT / f"{side}-{port}.log", "w")
    p = subprocess.Popen(cmd, env=env_for(side, port, manage, **kw), stdout=log, stderr=subprocess.STDOUT)
    procs.append(p)
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/auth", timeout=1)
            return p, port
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"{side} server did not start; see {log.name}")


def stop(p):
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


def request(port, method, path, body=None, headers=None, raw=False):
    h = {"Authorization": f"Bearer {TOKEN}"}
    if body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    h = {k: v for k, v in h.items() if v is not None}
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, payload, rh = r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        status, payload, rh = e.code, e.read(), e.headers
    if raw:
        return status, payload, rh
    try:
        return status, json.loads(payload), rh
    except ValueError:
        return status, payload.decode(errors="replace"), rh


def norm(v):
    if isinstance(v, dict):
        return {k: norm(x) for k, x in sorted(v.items()) if k not in VOLATILE}
    if isinstance(v, list):
        return [norm(x) for x in v]
    if isinstance(v, str):
        return v.replace(str(ROOT / "old"), "<side>").replace(str(ROOT / "new"), "<side>")
    return v


def check(name, old, new):
    if old == new:
        results["pass"] += 1
        print(f"PASS {name}")
    elif name in KNOWN_DIFFS:
        results["known"] += 1
        print(f"KNOWN {name}: {KNOWN_DIFFS[name]}")
    else:
        results["fail"] += 1
        print(f"FAIL {name}\n  first difference: {first_diff(old, new)}")


def first_diff(a, b, path="$"):
    if type(a) is not type(b):
        return f"{path}: {a!r:.300} != {b!r:.300}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if a.get(k, "<missing>") != b.get(k, "<missing>"):
                return first_diff(a.get(k, "<missing>"), b.get(k, "<missing>"), f"{path}.{k}")
    elif isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return first_diff(x, y, f"{path}[{i}]")
    return f"{path}: {a!r:.300} != {b!r:.300}"


def both(name, method, path, body=None, headers=None, shape=None):
    o = request(P_OLD, method, path, body, headers)
    n = request(P_NEW, method, path, body, headers)
    f = shape or (lambda r: (r[0], norm(r[1])))
    check(name, f(o), f(n))
    return o, n


def status_only(r):
    return r[0]


def validation(r):
    # pydantic's exact error text is not part of the contract: status + failing field
    return r[0], [tuple(d.get("loc", [])) for d in r[1]["detail"]] if r[0] == 422 else r[1]


def session_rows(resp):
    rows = resp[1]["sessions"] if isinstance(resp[1], dict) else []
    return resp[0], sorted((norm(r) for r in rows), key=lambda r: r.get("session", ""))


def human_id(port):
    rows = request(port, "GET", "/api/sessions")[1]["sessions"]
    return next(r for r in rows if r["session"] == "human-1")


def cases():
    # --- security / auth ---
    both("GET /api/auth (bearer)", "GET", "/api/auth")
    both("GET /api/auth (anonymous)", "GET", "/api/auth", headers={"Authorization": None})
    both("GET /api/auth (forged cookie)", "GET", "/api/auth", headers={"Authorization": None, "Cookie": "hq_device=1.2.3"})
    both("GET /api/sessions unauthenticated", "GET", "/api/sessions", headers={"Authorization": None}, shape=lambda r: (r[0], r[1]))
    both("unknown Host header", "GET", "/api/auth", headers={"Host": "attacker.example.test"})
    both("allowed extra Host", "GET", "/api/auth", headers={"Host": "hq.parity.test:1234"})
    both("cross-site Origin", "GET", "/api/auth", headers={"Origin": "https://evil.example"})
    both("same Origin", "GET", "/api/auth", headers={"Origin": None, "Host": "localhost"})
    both("sec-fetch-site cross-site", "GET", "/api/auth", headers={"Sec-Fetch-Site": "cross-site"})
    both("POST without JSON content type", "POST", "/api/workers/action", body=b"{}", headers={"Content-Type": "text/plain"})
    both("login wrong key", "POST", "/api/auth/login", {"token": "wrong"}, headers={"Authorization": None})
    both("login empty key (422)", "POST", "/api/auth/login", {"token": ""}, headers={"Authorization": None}, shape=validation)
    o, n = both("login right key", "POST", "/api/auth/login", {"token": TOKEN}, headers={"Authorization": None})
    for label, (port, r) in {"old": (P_OLD, o), "new": (P_NEW, n)}.items():
        cookie = r[2]["Set-Cookie"]
        attrs = sorted(a.strip().lower() for a in cookie.split(";")[1:])
        check(f"login cookie attributes ({label})", ["httponly", "max-age=2592000", "path=/", "samesite=strict"], attrs)
        value = cookie.split(";")[0]
        check(f"cookie authenticates ({label})", {"required": True, "authenticated": True},
              request(port, "GET", "/api/auth", headers={"Authorization": None, "Cookie": value})[1])
    both("logout", "POST", "/api/auth/logout", {}, shape=lambda r: (r[0], r[1], "Max-Age=0" in r[2].get("Set-Cookie", "")))
    both("security headers", "GET", "/api/auth", shape=lambda r: (r[2]["Cache-Control"], r[2]["X-Content-Type-Options"], r[2]["Referrer-Policy"], r[2]["X-Frame-Options"]))

    # --- static UI ---
    for path in ("/", "/sw.js", "/manifest.webmanifest", "/cc-static/app.js", "/cc-static/styles.css",
                 "/cc-static/icons/hq.svg", "/cc-static/vendor/preact.mjs", "/cc-static/nope.js"):
        o = request(P_OLD, "GET", path, raw=True)
        n = request(P_NEW, "GET", path, raw=True)
        f = lambda r: (r[0], r[1], r[2].get("Content-Type"), r[2].get("Cache-Control"), r[2].get("Service-Worker-Allowed"))
        check(f"static {path}", f(o), f(n))
    both("404 unknown route", "GET", "/api/nope")
    both("405 wrong method", "DELETE", "/api/workers", body={})

    # --- sessions / fleet ---
    both("GET /api/sessions", "GET", "/api/sessions", shape=session_rows)
    both("GET /api/sessions hosts", "GET", "/api/sessions", shape=lambda r: (r[0], norm(r[1]["hosts"]), r[1]["node_id"]))
    hid = human_id(P_OLD)
    check("session id identical across implementations", hid["id"], human_id(P_NEW)["id"])
    both("GET /api/sessions/{id}", "GET", f"/api/sessions/{hid['id']}")
    both("detail invalid id", "GET", "/api/sessions/not-base64!")
    both("detail unknown host", "GET", "/api/sessions/WyJub3BlIixbXV0")
    both("detail native route", "GET", "/api/sessions/WyJAbmF0aXZlIix7Im5hdGl2ZSI6ImNzZV9hYmMifV0")
    both("detail peer down", "GET", "/api/sessions/WyJwZWVyIix7InBlZXIiOiJ4In1d")
    both("X-HQ-Path too deep", "GET", "/api/sessions", headers={"X-HQ-Path": ",".join(["n"] * 9)})
    both("X-HQ-Path cycle", "GET", "/api/sessions", headers={"X-HQ-Path": "a,parity-node"})
    both("X-HQ-Path invalid", "GET", "/api/sessions", headers={"X-HQ-Path": "a b"})
    both("X-HQ-Path too long", "GET", "/api/sessions", headers={"X-HQ-Path": "x" * 3000})
    act = f"/api/sessions/{hid['id']}/action"
    both("action bad literal (422)", "POST", act, {"action": "dance", "revision": "r", "request_id": "12345678"}, shape=validation)
    both("action short request_id (422)", "POST", act, {"action": "send", "revision": "r", "request_id": "1"}, shape=validation)
    both("action index out of range (422)", "POST", act, {"action": "select", "revision": "r", "request_id": "12345678", "index": 99}, shape=validation)
    both("action stale revision", "POST", act, {"action": "send", "revision": "stale", "request_id": "stale-req-1", "text": "hi"})
    for label, port in (("old", P_OLD), ("new", P_NEW)):
        row = request(port, "GET", f"/api/sessions/{hid['id']}")[1]
        body = {"action": "send", "revision": row["revision"], "request_id": f"send-{label}-0001", "text": f"hello from {label}"}
        first = request(port, "POST", act, body)
        again = request(port, "POST", act, body)
        changed = request(port, "POST", act, dict(body, text="different"))
        results_by[label] = (first[0], first[1] if first[0] != 200 else sorted(first[1]),
                             again[1] == first[1], changed[0], changed[1])
        time.sleep(1.5)
    check("action send + receipt replay + payload reuse", results_by["old"], results_by["new"])
    check("action send delivered (200)", 200, results_by["old"][0])
    both("action unsupported key", "POST", act, {"action": "key", "key": "F13", "revision": human_id(P_OLD)["revision"], "request_id": "key-bad-001"})
    pane = (ROOT / "home/claude-input.log").read_text() if (ROOT / "home/claude-input.log").exists() else ""
    check("both sends reached the pane", (True, True), ("hello from old" in pane, "hello from new" in pane))

    # --- workers ---
    both("GET /api/workers (no workers)", "GET", "/api/workers")
    both("spawn missing prompt", "POST", "/api/workers/action", {"action": "spawn", "title": "x"})
    o = request(P_OLD, "POST", "/api/workers/action", {"action": "spawn", "title": "  Parity Task!  ", "prompt": "do it"})
    n = request(P_NEW, "POST", "/api/workers/action", {"action": "spawn", "title": "  Parity Task!  ", "prompt": "do it /loop 5m"})
    check("spawn response shape", (o[0], sorted(o[1]), o[1]["session"], o[1]["title"], o[1]["workdir"]),
          (n[0], sorted(n[1]), "hq-parity-task-1", n[1]["title"], n[1]["workdir"]))
    check("spawn picks next free name", "hq-parity-task-2", n[1]["session"])
    results_by["sids"] = {"old": o[1]["claude_session_id"], "new": n[1]["claude_session_id"]}
    for label, r in (("old", o), ("new", n)):
        meta = json.loads((ROOT / label / "bce/workspace" / r[1]["claude_session_id"] / "meta.json").read_text())
        check(f"registry meta.json ({label})", ["created", "kind", "loop", "session", "sid", "title", "workdir"], sorted(meta))
    check("meta loop flag detects /loop", (False, True), tuple(
        json.loads((ROOT / l / "bce/workspace" / r[1]["claude_session_id"] / "meta.json").read_text())["loop"] for l, r in (("old", o), ("new", n))))
    opts = lambda s: tmux("show-options", "-t", s).stdout
    strip_sid = lambda t: sorted(l for l in t.splitlines() if not l.startswith("@hq_sid"))
    check("spawned session options", strip_sid(opts("hq-parity-task-1")), strip_sid(opts("hq-parity-task-2")))
    prompt = lambda s: Path(f"/tmp/cc-tmux-api-prompts/{s}.md").read_text().split(str(ROOT))[0]
    check("prompt file", prompt("hq-parity-task-1").replace("do it", "X"), prompt("hq-parity-task-2").replace("do it /loop 5m", "X"))
    time.sleep(3)
    both("GET /api/workers (two workers)", "GET", "/api/workers", shape=lambda r: (r[0], norm(r[1])))
    w = {x["session"]: x for x in request(P_OLD, "GET", "/api/workers")[1]["workers"]}
    check("worker web_url from jsonl bridge id", True, (w["hq-parity-task-1"]["web_url"] or "").startswith("https://claude.ai/code/session_parity"))
    both("worker send missing identity", "POST", "/api/workers/action", {"action": "send", "session": "hq-parity-task-1", "text": "x"})
    ctl = w["hq-parity-task-1"]["control"]
    both("worker send wrong session", "POST", "/api/workers/action",
         {"action": "send", "session": "plain", "text": "x", "session_id": ctl["session_id"], "revision": ctl["revision"], "request_id": "wsend-mismatch"})
    for label, port in (("old", P_OLD), ("new", P_NEW)):
        ctl = {x["session"]: x for x in request(port, "GET", "/api/workers")[1]["workers"]}["hq-parity-task-1"]["control"]
        r = request(port, "POST", "/api/workers/action", {"action": "send", "session": "hq-parity-task-1", "text": f"steer {label}",
                                                          "session_id": ctl["session_id"], "revision": ctl["revision"], "request_id": f"wsend-{label}-1"})
        results_by["w" + label] = (r[0], sorted(r[1]))
        time.sleep(1.5)
    check("worker send", results_by["wold"], results_by["wnew"])
    check("worker send status", (200, ["id", "ok", "refresh_required", "request_id", "revision"]), results_by["wold"])
    log = (ROOT / "home/claude-input.log").read_text()
    check("worker steers reached the worker", (True, True), ("steer old" in log, "steer new" in log))
    both("kill foreign session refused", "POST", "/api/workers/action", {"action": "kill", "session": "plain"})
    both("kill nested-prefix session refused", "POST", "/api/workers/action", {"action": "kill", "session": "hq-other-1"})
    both("unknown worker action", "POST", "/api/workers/action", {"action": "dance"})
    both("worker action not a string (422)", "POST", "/api/workers/action", {"action": 5}, shape=validation)

    # --- routines ---
    both("routine toggle on (no enabled: line)", "POST", "/api/routines/action", {"action": "toggle", "name": "noenabled", "enabled": True})
    check("toggle inserted enabled line", (ROOT / "old/routines/noenabled.md").read_text(), (ROOT / "new/routines/noenabled.md").read_text())
    both("routine toggle off", "POST", "/api/routines/action", {"action": "toggle", "name": "every", "enabled": "false"})
    check("toggle rewrote file", (ROOT / "old/routines/every.md").read_text(), (ROOT / "new/routines/every.md").read_text())
    both("routine toggle missing file", "POST", "/api/routines/action", {"action": "toggle", "name": "ghost", "enabled": True})
    both("routine run missing", "POST", "/api/routines/action", {"action": "run", "name": "ghost"})
    both("routine run unparseable", "POST", "/api/routines/action", {"action": "run", "name": "nofront"})
    both("routine unknown action", "POST", "/api/routines/action", {"action": "dance", "name": "every"})
    o = request(P_OLD, "POST", "/api/routines/action", {"action": "run", "name": "yaml11"})
    time.sleep(1)
    n = request(P_NEW, "POST", "/api/routines/action", {"action": "run", "name": "yaml11"})
    check("routine run spawns kind=routine", (o[0], sorted(o[1]), "routine"), (n[0], sorted(n[1]), tmux("show-options", "-v", "-t", n[1]["session"], "@hq_kind").stdout.strip()))
    check("routine state file", sorted(json.loads((ROOT / "old/routines/.state.json").read_text())["yaml11"]),
          sorted(json.loads((ROOT / "new/routines/.state.json").read_text())["yaml11"]))
    time.sleep(2)
    both("GET /api/workers routines listing", "GET", "/api/workers",
         shape=lambda r: (r[0], [dict(norm(x), last_run=bool(x["last_run"]), next_run=bool(x["next_run"])) for x in r[1]["routines"]]))

    # --- kill (after routines so both listings saw every worker) ---
    both("kill owned worker", "POST", "/api/workers/action", {"action": "kill", "session": "hq-parity-task-1"}, shape=lambda r: r[0])
    request(P_NEW, "POST", "/api/workers/action", {"action": "kill", "session": "hq-parity-task-2"})
    sids = results_by["sids"]
    check("kill tombstones the registry entry", (True, True),
          tuple((ROOT / side / "bce/workspace" / sids[side] / ".done").exists() for side in ("old", "new")))
    gone = lambda s: tmux("has-session", "-t", s).returncode != 0
    check("killed sessions gone", (True, True), (gone("hq-parity-task-1"), gone("hq-parity-task-2")))

    # --- oneshot ---
    both("oneshot ok", "POST", "/api/oneshot", {"prompt": "ping"})
    both("oneshot failing claude", "POST", "/api/oneshot", {"prompt": "fail"})
    both("oneshot timeout", "POST", "/api/oneshot", {"prompt": "slow", "timeout": "1"})
    both("oneshot missing prompt (422)", "POST", "/api/oneshot", {}, shape=validation)
    both("oneshot invalid json (422)", "POST", "/api/oneshot", b"{nope", shape=status_only)

    # --- push storage ---
    both("GET /api/push", "GET", "/api/push")
    both("mute session", "POST", "/api/push/preferences", {"session_id": "host-pane", "muted": True})
    both("mute second", "POST", "/api/push/preferences", {"session_id": "a-first", "muted": 1})
    both("unmute", "POST", "/api/push/preferences", {"session_id": "a-first", "muted": False})
    both("GET /api/push/preferences", "GET", "/api/push/preferences")
    both("mute missing bool (422)", "POST", "/api/push/preferences", {"session_id": "x"}, shape=validation)
    both("unsubscribe unknown endpoint", "DELETE", "/api/push/subscribe", {"endpoint": "https://fcm.googleapis.com/x"})

    # --- rate limit last: it blocks further logins from 127.0.0.1 for a minute ---
    for _ in range(9):
        request(P_OLD, "POST", "/api/auth/login", {"token": "nope"}, headers={"Authorization": None})
        request(P_NEW, "POST", "/api/auth/login", {"token": "nope"}, headers={"Authorization": None})
    both("pairing rate limit (11th attempt)", "POST", "/api/auth/login", {"token": TOKEN}, headers={"Authorization": None})


def revive_cases():
    """Startup revival: each implementation restarts with workers managed and must
    recreate the registered (non-tombstoned) worker under the same name."""
    for label, cmd in (("old", OLD), ("new", NEW)):
        p, port = start(label, cmd)
        r = request(port, "POST", "/api/workers/action", {"action": "spawn", "title": f"revive {label}", "prompt": "survive"})
        session = r[1]["session"]
        time.sleep(3)  # fake claude writes the jsonl revival needs
        sid = r[1]["claude_session_id"]
        jsonl = next((ROOT / "home/.claude/projects").glob(f"*/{sid}.jsonl"))
        stop(p)
        tmux("kill-session", "-t", session)  # simulate the reboot
        p, port = start(label, cmd, manage="1")
        ok = False
        for _ in range(60):
            # the fake claude appends one jsonl line per start: two lines = resumed
            if tmux("has-session", "-t", session).returncode == 0 and len(jsonl.read_text().splitlines()) == 2:
                ok = True
                break
            time.sleep(0.5)
        results_by["revive" + label] = (ok, tmux("show-options", "-v", "-t", session, "@hq_kind").stdout.strip())
        # the scheduler's first tick spawns the due routine ("noenabled": enabled, never run)
        routine = None
        for _ in range(40):
            names = tmux("list-sessions", "-F", "#S").stdout.split()
            routine = next((n for n in names if n.startswith("hq-noenabled-")), None)
            if routine:
                break
            time.sleep(0.5)
        kind = tmux("show-options", "-v", "-t", routine, "@hq_kind").stdout.strip() if routine else None
        results_by["sched" + label] = (routine, kind, "noenabled" in json.loads((ROOT / label / "routines/.state.json").read_text()))
        stop(p)
        if routine:
            tmux("kill-session", "-t", routine)
        tmux("kill-session", "-t", session)
    check("startup revives registered worker with --resume", results_by["reviveold"], results_by["revivenew"])
    check("revive actually happened", True, results_by["reviveold"][0])
    check("scheduler spawns due routine", results_by["schedold"], results_by["schednew"])
    check("scheduler spawned (old)", ("hq-noenabled-1", "routine", True), results_by["schedold"])


def federation_cases(peer_label, peer_cmd):
    """One peer node (Python or Rust), aggregated by a Python HQ and by a Rust HQ."""
    global P_OLD, P_NEW
    peer, peer_port = start("peer", peer_cmd, node="peer-node", token="")  # private peers are token-free
    peers = [{"id": "far", "label": "Far box", "url": f"http://127.0.0.1:{peer_port}"}]
    old, P_OLD = start("old", OLD, node="agg-node", hosts=peers)
    new, P_NEW = start("new", NEW, node="agg-node", hosts=peers)
    time.sleep(1)
    both("federated GET /api/sessions", "GET", "/api/sessions", shape=session_rows)
    both("federated hosts", "GET", "/api/sessions", shape=lambda r: (r[0], norm(r[1]["hosts"])))
    row = next(r for r in request(P_OLD, "GET", "/api/sessions")[1]["sessions"] if r["session"] == "human-1" and r.get("federated"))
    check("federated id identical", row["id"], next(r for r in request(P_NEW, "GET", "/api/sessions")[1]["sessions"] if r["session"] == "human-1" and r.get("federated"))["id"])
    both("federated detail", "GET", f"/api/sessions/{row['id']}")
    both("federated cycle via path", "GET", "/api/sessions", headers={"X-HQ-Path": "peer-node"}, shape=lambda r: (r[0], session_rows(r)[1], [h.get("error") for h in r[1]["hosts"]]))
    for label, port in (("old", P_OLD), ("new", P_NEW)):
        fresh = request(port, "GET", f"/api/sessions/{row['id']}")[1]
        r = request(port, "POST", f"/api/sessions/{row['id']}/action",
                    {"action": "send", "revision": fresh["revision"], "request_id": f"fed-{label}-{peer_label}", "text": f"via {peer_label} peer {label}"})
        results_by["fed" + label] = (r[0], sorted(r[1]) if r[0] == 200 else r[1])
        time.sleep(1.5)
    check("federated action", results_by["fedold"], results_by["fednew"])
    check("federated action status", 200, results_by["fedold"][0])
    log = (ROOT / "home/claude-input.log").read_text()
    check("federated sends delivered", (True, True), (f"via {peer_label} peer old" in log, f"via {peer_label} peer new" in log))
    stop(peer)
    time.sleep(0.5)
    both("peer down after it was seen (stale rows)", "GET", "/api/sessions", shape=session_rows)
    for p in (old, new):
        stop(p)


results_by = {}


def main():
    global P_OLD, P_NEW
    setup()
    try:
        old, P_OLD = start("old", OLD)
        new, P_NEW = start("new", NEW)
        time.sleep(2)
        cases()
        stop(old)
        stop(new)
        for peer_label, peer_cmd in (("python", OLD), ("rust", NEW)):
            print(f"--- federation, {peer_label} peer")
            federation_cases(peer_label, peer_cmd)
        revive_cases()
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        subprocess.run([REAL_TMUX, "-L", SOCKET, "kill-server"], capture_output=True)
        print(f"logs kept in {ROOT}" if results["fail"] else "", end="")
        if not results["fail"]:
            shutil.rmtree(ROOT, ignore_errors=True)
    print(f"\n{results['pass']} passed, {results['fail']} failed, {results['known']} known differences")
    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
