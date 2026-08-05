"""
Routines — scheduled workers defined as markdown files. Opt-in via env.

One routine = one .md file in $CC_TMUX_ROUTINES_DIR with 3 lines of
frontmatter; edit the file and the next tick (60s) picks it up. No reload,
no database. When the env var is unset the feature is dormant: listings are
empty and the server starts no scheduler loop.

    ---
    title: Morning brief
    every: daily@07:00          # or: 30m / 2h / daily@HH:MM
    days: sun,mon,tue,wed,thu   # optional
    enabled: true
    workdir: /path/for/worker   # optional (default: CC_TMUX_WORKDIR)
    ---
    <body = the prompt given to the spawned worker>

The files are the source of truth (toggling rewrites the `enabled:` line);
the only state is last-run per routine in <dir>/.state.json.
Overlap guard: a routine is skipped while its previous worker is alive.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import workers

DIR = Path(os.environ["CC_TMUX_ROUTINES_DIR"]).expanduser() \
    if os.environ.get("CC_TMUX_ROUTINES_DIR") else None
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]  # datetime.weekday() order


def enabled() -> bool:
    return DIR is not None


def _state_file() -> Path:
    return DIR / ".state.json"


def _state() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(st: dict) -> None:
    _state_file().write_text(json.dumps(st, indent=1))


def _parse(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    return {
        "name": path.stem,
        "title": str(meta.get("title", path.stem)),
        "every": str(meta.get("every", "")).strip(),
        "days": [d.strip().lower()[:3] for d in str(meta.get("days", "")).split(",") if d.strip()],
        "enabled": bool(meta.get("enabled", False)),
        "prompt": m.group(2).strip(),
        "workdir": meta.get("workdir"),
    }


def _next_run(r: dict, last: float) -> float | None:
    """Epoch seconds of the next due run, or None if unschedulable."""
    every = r["every"]
    m = re.fullmatch(r"(\d+)\s*([mh])", every)
    if m:
        delta = int(m.group(1)) * (60 if m.group(2) == "m" else 3600)
        return (last + delta) if last else 0.0  # never ran -> due immediately
    m = re.fullmatch(r"daily@(\d{1,2}):(\d{2})", every)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        cand = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
        for _ in range(8):  # find next allowed day at HH:MM after last run
            ok_day = not r["days"] or DAYS[cand.weekday()] in r["days"]
            if ok_day and cand.timestamp() > max(last, 0):
                return cand.timestamp()
            cand += timedelta(days=1)
    return None


def _alive_session(name: str, live: list[str] | None = None) -> str | None:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if live is None:
        live = [s["name"] for s in workers._sessions()]
    for n in live:
        if n.startswith(f"{workers.PREFIX}{slug}-"):
            return n
    return None


def list_routines() -> list[dict]:
    if not enabled():
        return []
    DIR.mkdir(parents=True, exist_ok=True)
    st = _state()
    live = [s["name"] for s in workers._sessions()]  # ONE tmux call, not one per routine
    out = []
    for f in sorted(DIR.glob("*.md")):
        r = _parse(f)
        if not r:
            continue
        last = st.get(r["name"], {}).get("last_run", 0)
        nxt = _next_run(r, last)
        r.update({
            "last_run": last or None,
            "next_run": nxt if r["enabled"] else None,
            "running": _alive_session(r["name"], live),
            "file": str(f),
        })
        r.pop("prompt")  # not needed in listings
        out.append(r)
    return out


def run_now(name: str) -> dict:
    if not enabled():
        raise ValueError("routines disabled (CC_TMUX_ROUTINES_DIR unset)")
    r = _parse(DIR / f"{name}.md")
    if not r:
        raise ValueError(f"no routine named {name}")
    info = workers.spawn(r["name"], r["prompt"], r.get("workdir"), kind="routine")
    st = _state()
    st.setdefault(r["name"], {})["last_run"] = time.time()
    _save_state(st)
    return info


def set_enabled(name: str, on: bool) -> None:
    """Flip the enabled: line in the file — the file stays the source of truth."""
    f = DIR / f"{name}.md"
    text = f.read_text(encoding="utf-8")
    new = re.sub(r"^enabled:.*$", f"enabled: {str(on).lower()}",
                 text, count=1, flags=re.MULTILINE)
    if new == text and "enabled:" not in text:
        new = text.replace("---\n", f"---\nenabled: {str(on).lower()}\n", 1)
    f.write_text(new, encoding="utf-8")


def tick() -> list[str]:
    """Spawn every routine that is due; return their names. Idempotent within a
    minute: last-run state + the overlap guard make re-entry harmless."""
    if not enabled():
        return []
    spawned = []
    now = time.time()
    st = _state()
    for f in sorted(DIR.glob("*.md")) if DIR.exists() else []:
        r = _parse(f)
        if not r or not r["enabled"] or not r["prompt"]:
            continue
        nxt = _next_run(r, st.get(r["name"], {}).get("last_run", 0))
        if nxt is None or nxt > now:
            continue
        if _alive_session(r["name"]):  # previous run still working -> skip
            continue
        workers.spawn(r["name"], r["prompt"], r.get("workdir"), kind="routine")
        st.setdefault(r["name"], {})["last_run"] = now
        spawned.append(r["name"])
    if spawned:
        _save_state(st)
    return spawned
