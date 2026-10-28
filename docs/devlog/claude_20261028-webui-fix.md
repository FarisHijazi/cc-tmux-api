# Web UI rework from a live read-only audit (2026-10-28)

## How it was audited

Headless Playwright (chromium-1248) against the production hub on ftower through
an `ssh -L` tunnel to `127.0.0.1:8389`, at 1440×1000 and 390×844. Every non-GET
request was aborted by the harness, so nothing was sent to a live session. The
live snapshot then had 571 sessions (527 ended, 44 alive or unreachable).
The fixed UI was checked by replaying captured `GET` responses (session list and
five session details) through Playwright routing, not against live sessions.

## Problems found on production (0.7.0 static, identical to master)

- Default filter was **All sessions**: 527 ended rows rendered on every 8 s poll,
  rows sorted alphabetically, never by activity.
- Rows were ~130 px tall (title, folder, lifecycle pill, status, time on four
  lines); a phone showed three sessions under ~400 px of header and filters.
- **A pending approval was off screen** when a session was opened from the list:
  the card rendered from the list snapshot before the transcript arrived, the
  view scrolled to it then, and never again once 33 messages pushed it down.
- The session header took 300 px on a phone (title, host, folder row, lifecycle
  row, model input + Switch button, mode, usage, a one-tab tab bar), leaving the
  transcript ~430 px.
- Native ended sessions said "Messages will appear when this session sends them".
- A working native session showed no sign of work between streamed replies.
- Interrupt sat on its own row above the composer; Enter did not send on desktop.
- `Skill` tool cards had an empty summary; raw lower-case status (`waiting`).
- Ended sessions showed "Last activity: idle" and a Mute button in the header.

## What changed (`static/app.js`, `static/styles.css`)

- Inbox: default filter **Active**, filter and grouping remembered per device;
  order is needs-you, working, idle by most recent activity, ended newest first;
  60 rows render, then "Show N more". Two-line rows (~60 px). Compact header.
- Session view: one header (title, lifecycle, folder · host, live status, Mute);
  model and permission mode are pill selects that send `set_model` /
  `set_permission_mode` and snap back until Claude confirms (custom model via
  "Other model…"); no tab bar for native sessions; latest 120 messages render
  with "Show earlier"; "Claude is working…" while working without a stream.
- The approval scroll waits for the first detail response.
- Composer grows with its text; Enter sends with a fine pointer (Shift+Enter
  for a new line, phones keep Enter as newline); Interrupt and the draft toggle
  share the status line under the composer.
- Clearer empty states for native sessions; tool summaries include skill,
  subagent type and task id.

## Tests

- `uv run pytest -q`: 310 passed.
- `tests/frontend_test.mjs` (existing contracts, unchanged assertions; screenshot
  directory now `HQ_SHOT_DIR`/OS temp instead of macOS-only `/private/tmp`):
  PASS at 390 and 1440 px.
- New `tests/frontend_layout_test.mjs`: PASS at 390 and 1440 px. Its approval
  assertion fails when the scroll guard is removed.

## Not fixed (backend, out of this UI change)

- `GET /api/sessions` returns ~840 KB every 8 s (all 571 rows), and a working
  native session's ~1 MB detail is polled every second. Fixed afterwards on this
  branch: `docs/devlog/claude_20261028-incremental-polling.md`.
- Native sessions are titled with random slugs (`dev-structured-reddy`) and
  grouped under "Remote Control"; no task summary was reported live.
- A tmux transcript showed `/model` command output as user messages.

## Deploy (not done)

The hub serves `static/` from disk per request (`StaticFiles`, `index.html` with
`no-cache`), so copying the two files into the running release takes effect on the
next page load without a restart. On ftower (user unit `cc-tmux-hq.service`):

```sh
R=$(systemctl --user show -p ExecStart cc-tmux-hq.service | grep -o '/home/[^ ]*/releases/[0-9a-f]*' | head -1)
S=$R/venv/lib/python3.12/site-packages/cc_tmux_api/static
cp -p "$S/app.js" "$S/app.js.before-webui-fix"; cp -p "$S/styles.css" "$S/styles.css.before-webui-fix"
scp src/cc_tmux_api/static/app.js src/cc_tmux_api/static/styles.css ftower:"$S/"
```

Rollback: copy the `.before-webui-fix` files back. Restarting
`cc-tmux-hq.service` is not needed for static files.
