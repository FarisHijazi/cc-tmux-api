"""
cc-tmux-api — a tiny local daemon that spawns, lists, and kills unattended
Claude Code workers in tmux sessions.

Three verbs over HTTP (the whole contract):
  GET  /api/workers                  -> {"workers": [...], "others": [...]}
  POST /api/workers/action           {"action": "spawn", "title", "prompt", "workdir"?}
                                     {"action": "send",  "session", "text"}
                                     {"action": "kill",  "session"}
GET / serves a minimal status page; #w-<session> anchors deep-link to a worker.

SECURITY: there is NO auth. The server binds 127.0.0.1 only — never expose it
(no CORS, no reverse proxy): anyone who can POST here can run permission-
skipping Claude sessions on your machine.

Run: uvx git+https://github.com/FarisHijazi/cc-tmux-api
Env: BCE_WORKSPACE_DIR (registry base, default ~/.basecamp-enhancer),
     CC_TMUX_PORT (default 8377), CC_TMUX_PREFIX (default "hq-"),
     CC_TMUX_WORKDIR (default workdir for spawns, default $HOME).
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import workers


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

    task = asyncio.get_event_loop().create_task(revive())
    yield
    task.cancel()


app = FastAPI(title="cc-tmux-api", lifespan=_lifespan)


class WorkerAction(BaseModel):
    action: str  # spawn | send | kill
    title: str | None = None
    prompt: str | None = None
    workdir: str | None = None
    session: str | None = None
    text: str | None = None


@app.get("/api/workers")
def workers_list() -> dict:
    return {"workers": workers.list_workers(), "others": workers.list_others()}


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


# Minimal status page. Worker cards carry id="w-<session>" so clients can
# deep-link a specific worker (…/#w-<session>): the hash scrolls to and
# flashes the card. Polls every 5s; no build step, no assets.
_PAGE = """<!doctype html><meta charset="utf-8"><title>cc-tmux-api</title>
<style>
 body{font:14px/1.5 -apple-system,system-ui,sans-serif;max-width:780px;margin:24px auto;padding:0 12px;background:#111;color:#ddd}
 h1{font-size:18px} .w{border:1px solid #333;border-radius:10px;padding:10px 14px;margin:10px 0;background:#1a1a1a}
 .w.flash{outline:2px solid #e8833a} .hd{display:flex;gap:10px;align-items:baseline}
 .st{font-size:12px;padding:1px 8px;border-radius:9px;background:#333}
 .st[data-s=working]{background:#2f6f3f}.st[data-s=waiting]{background:#8a6d1a}.st[data-s=done]{background:#555}
 pre{white-space:pre-wrap;font-size:11px;color:#9a9a9a;max-height:130px;overflow:auto;background:#141414;padding:8px;border-radius:6px}
 a{color:#7ab7ff} .meta{font-size:12px;color:#888} button{background:#3a2222;color:#e99;border:1px solid #633;border-radius:6px;cursor:pointer}
</style>
<h1>cc-tmux-api — workers</h1><div id="list">loading…</div>
<script>
async function tick(){
  const d = await (await fetch('/api/workers')).json();
  const el = document.getElementById('list');
  el.innerHTML = d.workers.length ? '' : '<p class="meta">no workers</p>';
  for (const w of d.workers){
    const c = document.createElement('div'); c.className='w'; c.id='w-'+w.session;
    c.innerHTML = `<div class="hd"><b>${w.title}</b><span class="st" data-s="${w.status}">${w.status}</span>`+
      `<span class="meta">${w.session}</span>`+
      (w.web_url?` <a href="${w.web_url}" target="_blank">claude.ai ↗</a>`:'')+
      ` <button data-k="${w.session}">kill</button></div><pre>${w.tail.replace(/</g,'&lt;')}</pre>`;
    el.appendChild(c);
  }
  el.querySelectorAll('button[data-k]').forEach(b=>b.onclick=async()=>{
    if(!confirm('kill '+b.dataset.k+'?'))return;
    await fetch('/api/workers/action',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'kill',session:b.dataset.k})}); tick();
  });
  if(location.hash.startsWith('#w-')){const t=document.getElementById(location.hash.slice(1));
    if(t&&!t.dataset.flashed){t.dataset.flashed=1;t.scrollIntoView({block:'center'});t.classList.add('flash');
      setTimeout(()=>t.classList.remove('flash'),2500);}}
}
tick(); setInterval(tick, 5000);
</script>"""


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(_PAGE, headers={"Cache-Control": "no-cache"})


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("CC_TMUX_PORT", "8377")))
