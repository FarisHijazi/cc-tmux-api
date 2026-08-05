"""
cc-tmux-api — a tiny local daemon that spawns, lists, and kills unattended
Claude Code workers in tmux sessions, and optionally schedules them.

The whole HTTP contract:
  GET  /api/workers                  -> {"workers": [...], "others": [...], "routines": [...]}
  POST /api/workers/action           {"action": "spawn", "title", "prompt", "workdir"?}
                                     {"action": "send",  "session", "text"}
                                     {"action": "kill",  "session"}
  POST /api/routines/action          {"action": "run"|"toggle", "name", "enabled"?}
  POST /api/oneshot                  {"prompt", "timeout"?} -> {"ok", "output"}
GET / serves the workers page; #w-<session> anchors deep-link to a worker.

Embedding (an app on top — e.g. a mission-control that adds its own panels):
    from cc_tmux_api.server import create_app
    app = create_app()          # full API + UI
    app.include_router(my_router)   # layer your domain routes over it

SECURITY: there is NO auth. The server binds 127.0.0.1 only — never expose it
(no CORS, no reverse proxy): anyone who can POST here can run permission-
skipping Claude sessions on your machine.

Run: uvx git+https://github.com/FarisHijazi/cc-tmux-api
Env: BCE_WORKSPACE_DIR (registry base, default ~/.basecamp-enhancer),
     CC_TMUX_PORT (default 8377), CC_TMUX_PREFIX (default "hq-"),
     CC_TMUX_WORKDIR (default workdir for spawns, default $HOME),
     CC_TMUX_ROUTINES_DIR (routines *.md dir — unset = scheduling dormant),
     CC_TMUX_ROUTINE_REAP_HOURS (kill stale routine workers, default 12).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import routines, workers

STATIC = Path(__file__).resolve().parent / "static"
ROUTINE_REAP_HOURS = float(os.environ.get("CC_TMUX_ROUTINE_REAP_HOURS", "12"))


@asynccontextmanager
async def _lifespan(app: FastAPI):
    async def revive():
        # After a reboot the tmux server (= the live worker registry) is gone;
        # revive registered task sessions from the persistent registry (loop
        # watchers also get a "continue" nudge), and GC old tombstones.
        try:
            for name in await asyncio.to_thread(workers.resume_registered):
                print(f"revived worker: {name}", flush=True)
            for name in await asyncio.to_thread(workers.registry_gc):
                print(f"gc'd finished session dir: {name}", flush=True)
        except Exception as e:
            print(f"revive error: {e}", file=sys.stderr, flush=True)

    async def scheduler():
        # spawn due routines + reap stale routine workers, every 60s
        while True:
            try:
                for name in await asyncio.to_thread(routines.tick):
                    print(f"routine spawned: {name}", flush=True)
                for name in await asyncio.to_thread(
                        workers.reap_stale, "routine", ROUTINE_REAP_HOURS):
                    print(f"reaped stale routine worker: {name}", flush=True)
            except Exception as e:
                print(f"routine tick error: {e}", file=sys.stderr, flush=True)
            await asyncio.sleep(60)

    tasks = [asyncio.get_event_loop().create_task(revive())]
    if routines.enabled():
        tasks.append(asyncio.get_event_loop().create_task(scheduler()))
    yield
    for t in tasks:
        t.cancel()


class WorkerAction(BaseModel):
    action: str  # spawn | send | kill
    title: str | None = None
    prompt: str | None = None
    workdir: str | None = None
    session: str | None = None
    text: str | None = None


class RoutineAction(BaseModel):
    action: str  # run | toggle
    name: str
    enabled: bool | None = None


class Oneshot(BaseModel):
    prompt: str
    timeout: int = 300


def create_app() -> FastAPI:
    app = FastAPI(title="cc-tmux-api", lifespan=_lifespan)

    @app.get("/api/workers")
    def workers_list() -> dict:
        return {"workers": workers.list_workers(), "others": workers.list_others(),
                "routines": routines.list_routines()}

    @app.post("/api/workers/action")
    def workers_action(req: WorkerAction) -> dict:
        if req.action == "spawn":
            if not (req.title and req.prompt):
                return {"ok": False, "output": "title and prompt required"}
            info = workers.spawn(req.title.strip(), req.prompt, req.workdir or None)
            return {"ok": True, **info}
        if req.action == "send":
            if not (req.session and req.text):
                return {"ok": False, "output": "session and text required"}
            workers.send_text(req.session, req.text)
            return {"ok": True}
        if req.action == "kill":
            try:
                workers.kill(req.session or "")
            except ValueError as e:
                return {"ok": False, "output": str(e)}
            return {"ok": True}
        return {"ok": False, "output": f"unknown action {req.action}"}

    @app.post("/api/routines/action")
    def routines_action(req: RoutineAction) -> dict:
        try:
            if req.action == "run":
                return {"ok": True, **routines.run_now(req.name)}
            if req.action == "toggle":
                routines.set_enabled(req.name, bool(req.enabled))
                return {"ok": True}
        except (ValueError, OSError) as e:
            return {"ok": False, "output": str(e)}
        return {"ok": False, "output": f"unknown action {req.action}"}

    @app.post("/api/oneshot")
    def oneshot(req: Oneshot) -> dict:
        try:
            r = workers.oneshot(req.prompt, req.timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": f"timed out after {req.timeout}s"}
        return {"ok": r.returncode == 0,
                "output": r.stdout.strip() or r.stderr.strip()}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/cc-static", StaticFiles(directory=STATIC), name="cc-static")
    return app


app = create_app()  # standalone: `uvicorn cc_tmux_api.server:app` / main()


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("CC_TMUX_PORT", "8377")))
