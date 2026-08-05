# cc-tmux-api

A tiny local HTTP daemon that **spawns, lists, kills, and schedules unattended
[Claude Code](https://code.claude.com) workers inside tmux sessions** — with
status detection, claude.ai remote-control links, and revival after reboot.

Built as the backend for the Basecamp Enhancer extension's Claude Code
launcher, but generic: anything that can POST JSON to localhost can spin off
workers. Deliberately stateless and small — a REST wrapper around
tmux-wrapping-claude plus a scheduler, nothing more. Domain logic (approval
queues, work orchestration) belongs in apps **on top** (see Embedding).

## Run

```bash
brew install uv tmux        # prerequisites (plus Claude Code, signed in)
uvx git+https://github.com/FarisHijazi/cc-tmux-api
```

That starts the server on `http://127.0.0.1:8377`. Open it in a browser for a
minimal live dashboard (worker cards, status, pane tail, kill buttons;
`#w-<session>` deep-links to a card).

For shareable `claude.ai/code/session_…` links per worker, enable remote
control by default: inside any Claude Code session run `/config` and set
**"Enable Remote Control for all sessions"** to `true`.

## API (the whole contract)

```text
GET  /api/workers                  -> {"workers": [...], "others": [...], "routines": [...]}
POST /api/workers/action           {"action": "spawn", "title": "...", "prompt": "...", "workdir": "..."?}
                                   {"action": "send",  "session": "...", "text": "..."}
                                   {"action": "kill",  "session": "..."}
POST /api/routines/action          {"action": "run" | "toggle", "name": "...", "enabled": bool?}
POST /api/oneshot                  {"prompt": "...", "timeout": 300?} -> {"ok", "output"}
```

Each worker in the list carries: `session` (tmux name), `title`, `status`
(`working | waiting | idle | done | unknown`), `tail` (recent pane output),
`web_url` (claude.ai link once the remote-control bridge connects),
`jsonl_age_s` (seconds since the session's event log last grew — ground truth
for "actually doing something"), `kind` (`task` by default; spawn accepts a
`kind` so disposable periodic workers can be tagged and later reaped via the
library's `reap_stale(kind, max_age_hours)`), `claude_session_id`, `workdir`,
`age_s`, `attached`.

## How it works

- **tmux is the live registry** — a worker is any session named `hq-*`
  (configurable). Spawn is *shell-first*: the session starts as a plain shell
  and the `claude` command is typed in, so when claude exits the pane and its
  output survive for review.
- **Unattended by construction** — workers run with
  `--dangerously-skip-permissions`, the question tool disabled, and a system
  prompt forbidding "shall I proceed?" stalls.
- **Status** = pane-text classification (vendored from
  [amux](https://github.com/mixpeek/amux)) upgraded by session-jsonl
  freshness: a jsonl appended <60s ago means mid-turn work even when the pane
  shows only scrolling tool output.
- **Reboot revival** — every spawn writes
  `$BCE_WORKSPACE_DIR/workspace/<session-uuid>/meta.json`; on startup,
  non-tombstoned sessions <5 days old are revived with `claude --resume` under
  the **same tmux name** (same claude.ai URL), and loop watchers get a
  `continue` nudge. Workers tombstone themselves with a `.done` file when
  truly finished (kill does it too); tombstones are GC'd after 14 days.

## Routines (scheduled workers)

Set `CC_TMUX_ROUTINES_DIR` to a directory of markdown files and the server
grows a scheduler (unset = the feature is dormant). One routine = one `.md`
file — frontmatter is the schedule, the body is the worker prompt:

```markdown
---
title: Nightly triage
every: daily@07:00          # or: 30m / 2h
days: sun,mon,tue,wed,thu   # optional
enabled: true
workdir: /path/for/worker   # optional
---
<prompt given to the spawned worker>
```

A 60s tick spawns whatever is due (`kind: routine`), skips a routine while its
previous worker is still alive, and reaps routine workers older than
`CC_TMUX_ROUTINE_REAP_HOURS`. The files are the source of truth — the UI
toggle rewrites the `enabled:` line; the only state is last-run per routine in
`<dir>/.state.json`.

## Embedding (build your app on top)

```python
from cc_tmux_api.server import create_app

app = create_app()               # full API + workers page at /
app.include_router(my_router)    # layer your domain routes/pages over it
```

One process, one port: your app gets workers, routines, and the UI for free
and stays free of tmux/claude plumbing. `cc_tmux_api.workers.oneshot(prompt)`
runs a headless one-shot (`claude -p`) when you need output, not a session.

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `BCE_WORKSPACE_DIR` | `~/.basecamp-enhancer` | Base dir; the per-session registry lives in `$BASE/workspace/<uuid>/` |
| `CC_TMUX_PORT` | `8377` | HTTP port (always bound to 127.0.0.1) |
| `CC_TMUX_PREFIX` | `hq-` | tmux session-name prefix that marks a worker |
| `CC_TMUX_WORKDIR` | `$HOME` | Default working directory for spawned workers |
| `CC_TMUX_ROUTINES_DIR` | unset | Directory of routine `.md` files; unset disables scheduling |
| `CC_TMUX_ROUTINE_REAP_HOURS` | `12` | Kill routine-kind workers older than this |

## Security

**There is no auth.** The server binds `127.0.0.1` only — never expose it via
CORS, port-forwarding, or a reverse proxy: anyone who can reach it can run
permission-skipping Claude sessions on your machine. Callers on the same
machine (e.g. a browser extension's service worker with host permissions) are
the intended clients.
