"""
cc-tmux-api — a tiny local daemon that spawns, lists, and kills unattended
Claude Code workers in tmux sessions, and optionally schedules them.

The whole HTTP contract:
  GET  /api/workers                  -> {"workers": [...], "others": [...], "routines": [...]}
  POST /api/workers/action           {"action": "spawn", "title", "prompt", "workdir"?}
                                     {"action": "send", "session", "text",
                                      "session_id", "revision", "request_id"}
                                     {"action": "kill",  "session"}
  POST /api/routines/action          {"action": "run"|"toggle", "name", "enabled"?}
  POST /api/oneshot                  {"prompt", "timeout"?} -> {"ok", "output"}
GET / serves the fleet dashboard; session links use #session=<opaque-id>.

Embedding (an app on top — e.g. a mission-control that adds its own panels):
    from cc_tmux_api.server import create_app
    app = create_app()          # full API + UI
    app.include_router(my_router)   # layer your domain routes over it

The server defaults to 127.0.0.1; CC_TMUX_BIND selects explicit interfaces.
CC_TMUX_TOKEN enables optional API authentication. An unset token disables
authentication; configured host and same-origin checks still apply.
Adopted interactive sessions use /api/sessions and are never worker-reaped.
See README.md for fleet, pairing, push, and private configuration contracts.

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
import secrets
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import fleet, routines, workers
from .notifications import PushService
from .security import COOKIE, MAX_AGE, Security

STATIC = Path(__file__).resolve().parent / "static"
ROUTINE_REAP_HOURS = float(os.environ.get("CC_TMUX_ROUTINE_REAP_HOURS", "12"))


def cascade_path(request: Request) -> tuple[str, ...]:
    raw = request.headers.get("x-hq-path", "")
    if len(raw) > 2048:
        raise HTTPException(400, "HQ path is too long")
    path = tuple(part.strip() for part in raw.split(",") if part.strip())
    if len(path) > 8:
        raise HTTPException(508, "HQ cascade depth exceeded")
    return path


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
                    workers.reap_stale, "routine", ROUTINE_REAP_HOURS
                ):
                    print(f"reaped stale routine worker: {name}", flush=True)
            except Exception as e:
                print(f"routine tick error: {e}", file=sys.stderr, flush=True)
            await asyncio.sleep(60)

    async def monitor():
        while True:
            try:
                snapshot = await asyncio.to_thread(fleet.snapshot)
                if app.state.push.config()["available"]:
                    await asyncio.to_thread(app.state.push.observe, snapshot, deliver=False)
            except Exception as e:
                print(f"session monitor: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            await asyncio.sleep(5)

    async def deliver_push():
        while True:
            try:
                await asyncio.to_thread(app.state.push.deliver_pending)
            except Exception as e:
                print(f"push delivery: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            await asyncio.sleep(5)

    manage = os.environ.get("CC_TMUX_MANAGE_WORKERS", "1") == "1"
    tasks = [asyncio.create_task(revive())] if manage else []
    if manage and routines.enabled():
        tasks.append(asyncio.get_event_loop().create_task(scheduler()))
    # Lifecycle history must observe sessions even without push or an open UI.
    tasks.append(asyncio.create_task(monitor()))
    if app.state.push.config()["available"]:
        tasks.append(asyncio.create_task(deliver_push()))
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class WorkerAction(BaseModel):
    action: str  # spawn | send | kill
    title: str | None = None
    prompt: str | None = None
    workdir: str | None = None
    session: str | None = None
    text: str | None = None
    session_id: str | None = None
    revision: str | None = None
    request_id: str | None = None


class RoutineAction(BaseModel):
    action: str  # run | toggle
    name: str
    enabled: bool | None = None


class Oneshot(BaseModel):
    prompt: str
    timeout: int = 300


class SessionAction(BaseModel):
    action: Literal["send", "key", "type", "select", "submit", "respond", "interrupt", "set_model", "set_permission_mode"]
    revision: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=8, max_length=128)
    text: str | None = Field(default=None, max_length=32000)
    key: str | None = Field(default=None, max_length=16)
    index: int | None = Field(default=None, ge=0, le=50)
    response: dict | None = None


class Pairing(BaseModel):
    token: str = Field(min_length=1, max_length=1024)


class Subscription(BaseModel):
    subscription: dict


class Unsubscribe(BaseModel):
    endpoint: str = Field(max_length=4096)


class PushPreference(BaseModel):
    session_id: str = Field(max_length=1024)
    muted: bool


def create_app() -> FastAPI:
    app = FastAPI(title="cc-tmux-api", lifespan=_lifespan)
    security = Security(
        os.environ.get("CC_TMUX_TOKEN", ""), os.environ.get("CC_TMUX_PUBLIC_URL", "")
    )
    app.state.security = security
    state_dir = Path(
        os.environ.get("CC_TMUX_STATE_DIR", str(Path.home() / ".local/state/cc-tmux-api"))
    )
    app.state.push = PushService(
        state_dir, os.environ.get("CC_TMUX_VAPID_SUBJECT", security.public_url)
    )
    app.middleware("http")(security.middleware)
    native = fleet.native_backend()
    if native is not None:
        native.enable_computer_use_dialogs()
        app.include_router(native.router())

    @app.exception_handler(fleet.FleetError)
    async def fleet_error(request: Request, exc: fleet.FleetError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/api/auth")
    def auth(request: Request):
        return {"required": bool(security.token), "authenticated": security.authenticated(request)}

    @app.post("/api/auth/login")
    def login(req: Pairing, request: Request, response: Response):
        address = request.client.host if request.client else "local"
        if not security.can_attempt(address):
            raise HTTPException(429, "Too many pairing attempts. Try again in one minute.")
        if not security.token or not secrets.compare_digest(req.token, security.token):
            raise HTTPException(401, "Pairing key not recognized")
        response.set_cookie(
            COOKIE,
            security.cookie(),
            max_age=MAX_AGE,
            httponly=True,
            secure=bool(security.public_url) or request.url.scheme == "https",
            samesite="strict",
            path="/",
        )
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/sessions")
    def sessions_list(request: Request):
        return fleet.snapshot(path=cascade_path(request))

    @app.get("/api/sessions/{session_id}")
    def session_detail(session_id: str, request: Request):
        return fleet.detail(session_id, path=cascade_path(request))

    @app.post("/api/sessions/{session_id}/action")
    def session_action(session_id: str, req: SessionAction, request: Request):
        return fleet.act(session_id, **req.model_dump(), path=cascade_path(request))

    @app.get("/api/push")
    def push_config():
        return app.state.push.config()

    @app.post("/api/push/subscribe")
    def subscribe(req: Subscription):
        try:
            app.state.push.subscribe(req.subscription)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @app.delete("/api/push/subscribe")
    def unsubscribe(req: Unsubscribe):
        app.state.push.remove(req.endpoint)
        return {"ok": True}

    @app.get("/api/push/preferences")
    def push_preferences():
        return app.state.push.preferences()

    @app.post("/api/push/preferences")
    def push_preference(req: PushPreference):
        app.state.push.set_muted(req.session_id, req.muted)
        return app.state.push.preferences()

    @app.post("/api/push/test")
    def test_push():
        return app.state.push.test()

    @app.get("/api/workers")
    def workers_list() -> dict:
        local_hosts = {host["id"] for host in fleet.hosts() if not host.get("ssh") and not host.get("url")}
        controls = {
            item["session"]: {"session_id": item["id"], "revision": item["revision"]}
            for item in fleet.snapshot()["sessions"]
            if item["host"] in local_hosts and item.get("alive", True) is True
        }
        return {
            "workers": [dict(item, control=controls.get(item["session"]))
                        for item in workers.list_workers()],
            "others": workers.list_others(),
            "routines": routines.list_routines(),
        }

    @app.post("/api/workers/action")
    def workers_action(req: WorkerAction) -> dict:
        if req.action == "spawn":
            if not (req.title and req.prompt):
                return {"ok": False, "output": "title and prompt required"}
            info = workers.spawn(req.title.strip(), req.prompt, req.workdir or None)
            return {"ok": True, **info}
        if req.action == "send":
            if not all((req.session, req.text, req.session_id, req.revision, req.request_id)):
                raise HTTPException(400, "Refresh the worker: session identity, revision and request_id are required")
            target = fleet.detail(req.session_id)
            local_hosts = {host["id"] for host in fleet.hosts() if not host.get("ssh") and not host.get("url")}
            if (target["host"] not in local_hosts or target["session"] != req.session
                    or req.session not in {item["session"] for item in workers.list_workers()}):
                raise HTTPException(409, "Worker identity changed or is not owned by this service")
            return fleet.act(req.session_id, "send", req.revision, req.request_id, text=req.text)
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
        return {"ok": r.returncode == 0, "output": r.stdout.strip() or r.stderr.strip()}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(
            STATIC / "sw.js",
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    app.mount("/cc-static", StaticFiles(directory=STATIC), name="cc-static")
    return app


app = create_app()  # standalone: `uvicorn cc_tmux_api.server:app` / main()


def bind_address(value: str, default_port: int) -> tuple[str, int]:
    """Accept an address or host:port; bracket IPv6 when specifying a port."""
    if value.startswith("[") or value.count(":") == 1:
        parsed = urlsplit("tcp://" + value)
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("Invalid CC_TMUX_BIND endpoint")
        return parsed.hostname, parsed.port if parsed.port is not None else default_port
    return value, default_port


def main(app_spec: str | None = None) -> None:
    import uvicorn

    port = int(os.environ.get("CC_TMUX_PORT", "8377"))
    addresses = list(dict.fromkeys(
        part.strip() for part in os.environ.get("CC_TMUX_BIND", "127.0.0.1").split(",")
        if part.strip()
    ))
    if not addresses:
        raise ValueError("CC_TMUX_BIND requires at least one address")
    listeners = []
    try:
        for endpoint in addresses:
            address, bind_port = bind_address(endpoint, port)
            listener = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET)
            listeners.append(listener)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((address, bind_port))
            listener.listen(128)
            listener.setblocking(False)
        # Native SSE connections are intentionally long-lived; bound service restarts.
        uvicorn.Server(uvicorn.Config(
            app_spec or app, port=port, timeout_graceful_shutdown=5,
        )).run(sockets=listeners)
    finally:
        for listener in listeners:
            listener.close()
