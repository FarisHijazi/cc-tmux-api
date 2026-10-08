# cc-tmux-api

HQ for existing [Claude Code](https://code.claude.com) tmux sessions: discover,
read conversations, answer questions, and operate unattended workers and routines.
Run the same application on each machine. Configured HQ peers aggregate downstream
sessions and forward reads and guarded writes through a cascade to the owning
node. Local and SSH probes remain available for hosts without an HQ peer.

The current design replaces the old HQ with this application, embedded in the
same process where domain routes are needed, rather than adding a permanent
mobile daemon. The requested runtime has **no app pairing token**. HQ serves HTTP
on explicit interfaces; ngrok or another external tunnel will provide public
HTTPS. The tunnel choice and public URL remain pending; no Serve or Funnel is
part of the plan. See [the current handoff](docs/devlog/claude_20261008-federated-hq-lifecycle.md)
for source/test evidence and limits. Domain logic belongs in apps **on top**.

The parent has verified 0.4.1 activation of all VM owners and the persistent generic
Mac HQ with matching runtime package bytes, replacing its preview. Root HTTP peer
reads, real-browser lifecycle checks, and review-card visual verification passed.
The fresh owned remote menu batch passed all nine actions and native completion;
earlier two-relay cascade checks also passed. Exact build, listener, process and state
ownership is recorded in the [current deployment report](docs/devlog/claude_20261008-federated-hq-lifecycle.md#verified-deployment-current-state).
Public HTTPS remains blocked and is not live.

## Run

```bash
brew install uv tmux        # prerequisites (plus Claude Code, signed in)
uvx git+https://github.com/FarisHijazi/cc-tmux-api
```

That starts the server on `http://127.0.0.1:8377`. Open it in a browser for a
HQ dashboard. Sessions groups existing Claude panes by host or directory, with
search, attention filtering, conversation/terminal views, and guarded controls.
Workers and Routines retain the unattended workflow described below;
`#w-<session>` opens a worker card. Fleet links use `/#session=<encoded-id>`.
The mobile upgrade is version 0.4.0 in this checkout; the upstream `uvx` command
above uses the published repository, not necessarily these uncommitted changes.

For shareable `claude.ai/code/session_…` links per worker, enable remote
control by default: inside any Claude Code session run `/config` and set
**"Enable Remote Control for all sessions"** to `true`.

## Worker API

```text
GET  /api/workers                  -> {"workers": [...], "others": [...], "routines": [...]}
POST /api/workers/action           {"action": "spawn", "title": "...", "prompt": "...", "workdir": "..."?}
                                   {"action": "send", "session": "...", "text": "...", "session_id": "...", "revision": "...", "request_id": "..."}
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
`age_s`, `attached`, and `control` (`{session_id, revision}` when a local Claude
target is discoverable, otherwise null).

### Migrating worker sends to 0.4.0

Worker sends are intentionally incompatible with clients that provide only a
tmux session name and text. Fetch `GET /api/workers` and use that worker's
`control.session_id` and `control.revision`, alongside its `session`, the `text`,
and a new `request_id` for the intended action. Use the same request-ID convention
as the [mobile action API](#mobile-api). When `control` is null, disable sending
and refresh; do not fall back to targeting the tmux name.

Missing identity/revision/request-ID fields now return HTTP 400. The route checks
the discovered target against the local worker registry and session name, then
uses the fleet's guarded send path. Changed ownership/identity or stale revisions
return 409. These checks prevent typing into an exited worker's shell or a reused
session. On uncertainty, refresh and inspect rather than automatically resending.
See [server.py](src/cc_tmux_api/server.py) for the implemented migration boundary.

## How it works

- **tmux is the live registry** — a worker is any session named `hq-*`
  (configurable). Spawn is *shell-first*: the session starts as a plain shell
  and the `claude` command is typed in with `; exit`. When Claude exits, the shell
  exits and its tmux session closes. Conversation history remains in Claude's
  session JSONL; do not rely on a surviving worker pane for review.
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

A 60s tick spawns whatever is due (`kind: routine`). It skips a previous run whose
worker status is not `done`; a leftover finished run is cleared before respawning
so an old shell cannot block the next scheduled run. It also reaps routine workers older than
`CC_TMUX_ROUTINE_REAP_HOURS`. The files are the source of truth — the UI
toggle rewrites the `enabled:` line; the only state is last-run per routine in
`<dir>/.state.json`.

## Embedding (build your app on top)

```python
from cc_tmux_api.server import create_app

app = create_app()               # full HQ API + UI at /
app.include_router(my_router)    # layer your domain routes/pages over it
```

One process serves the configured listeners: your app gets workers, routines, and the UI for free
and stays free of tmux/claude plumbing. `cc_tmux_api.workers.oneshot(prompt)`
runs a headless one-shot (`claude -p`) when you need output, not a session.

For an importable `main.py` defining `app`, use the same configuration path:

```bash
uv run cc-tmux-api --config /path/to/private-config.json --app main:app
```

Configuration loads first; `--app` then imports the wrapper module before the
CLI imports `server`. The wrapper can establish routine/workdir/worker defaults
before those modules read their import-time configuration. The CLI serves that app
on its configured sockets in one process. During deployment, replace the old HQ
process so each worker registry has one revival/scheduler owner. Deployment status
and build-specific verification are recorded in the
[handoff devlog](docs/devlog/claude_20261008-federated-hq-lifecycle.md).

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `BCE_WORKSPACE_DIR` | `~/.basecamp-enhancer` | Base dir; the per-session registry lives in `$BASE/workspace/<uuid>/` |
| `CC_TMUX_PORT` | `8377` | Default HTTP port for bind entries without an explicit port |
| `CC_TMUX_BIND` | `127.0.0.1` | Comma-separated addresses or `host:port` endpoints; use `[IPv6]:port` for IPv6 with a port. One process serves all listeners; select explicit interfaces |
| `CC_TMUX_ALLOWED_HOSTS` | unset | Additional comma-separated request hostnames/IPs without scheme or port; loopback names are already allowed |
| `CC_TMUX_PREFIX` | `hq-` | tmux session-name prefix that marks a worker |
| `CC_TMUX_WORKDIR` | `$HOME` | Default working directory for spawned workers |
| `CC_TMUX_ROUTINES_DIR` | unset | Directory of routine `.md` files; unset disables scheduling |
| `CC_TMUX_ROUTINE_REAP_HOURS` | `12` | Kill routine-kind workers older than this |
| `CC_TMUX_MANAGE_WORKERS` | `1` | Only `1` enables startup revival/GC and background routine scheduling/reaping; `0` leaves worker API routes enabled |
| `CC_TMUX_HOSTS` | `[{"id":"local","label":"Local"}]` | JSON array of 1–32 targets: local, an existing `ssh` alias, or an HQ peer `url`; `ssh` and `url` are mutually exclusive |
| `CC_TMUX_NODE_ID` | machine hostname | Stable, distinct node ID for ownership/cycle detection; 1–128 letters, digits, `_`, `.`, `:`, or `-` |
| `CC_TMUX_TOKEN` | unset | Optional pairing/Bearer secret, at least 32 characters when configured; leave unset/empty for the requested runtime |
| `CC_TMUX_PUBLIC_URL` | unset | Optional HTTPS origin without a path; requires a token. Leave unset/empty for direct Tailscale HTTP |
| `CC_TMUX_STATE_DIR` | `~/.local/state/cc-tmux-api` | Session-history SQLite store and separate push keys/subscriptions/preferences/deduplication/outbox |
| `CC_TMUX_VAPID_SUBJECT` | `CC_TMUX_PUBLIC_URL` | `mailto:` contact or HTTPS subject for push configuration; fleet/lifecycle monitoring runs independently |

The listener configuration is HTTP-only. There are no application certificate or
key settings in this contract; public HTTPS belongs to the external tunnel.

## Mobile configuration

Run this checkout with `uv sync --dev`, then `uv run cc-tmux-api`. A private JSON
configuration file can supply the environment variables above:

```bash
uv run cc-tmux-api --config /path/to/private-config.json
```

The CLI loads configuration before application imports. Existing environment
variables win over file values; arrays/objects become JSON environment strings.
Only `CC_TMUX_*` keys and `BCE_WORKSPACE_DIR` are accepted. Configuration template:

```json
{
  "CC_TMUX_NODE_ID": "hq-laptop",
  "CC_TMUX_PORT": 8377,
  "CC_TMUX_BIND": "127.0.0.1,<this-node-tailscale-ip>:8389",
  "CC_TMUX_ALLOWED_HOSTS": "<this-node-tailscale-ip>",
  "CC_TMUX_TOKEN": "",
  "CC_TMUX_PUBLIC_URL": "",
  "CC_TMUX_HOSTS": [
    {"id": "local", "label": "Local"},
    {"id": "work", "label": "Work", "url": "http://<peer-tailscale-ip>:8389"}
  ]
}
```

Replace the bracketed IP placeholders with the respective machines' addresses.
This example preserves loopback port 8377 and serves Tailscale port 8389 in the
same process. Bare IPv4/IPv6 addresses use `CC_TMUX_PORT`.
Bind and allowed-host values are comma-separated strings, not JSON arrays. Bind
selects network listeners; allowed hosts checks HTTP Host names and does not open
a socket. Existing environment variables override even empty config values, so
the no-pairing launcher must not inherit a token or public URL. No browser pairing
step, Tailscale Serve, or Funnel is part of this runtime. This template configures
the local/Tailscale HTTP backend; it is not a completed public-tunnel setup.

Public HTTPS via ngrok or another tunnel is pending with the parent. The parent
reports that automatic approval review blocked the tunnel invocation; explicit
user confirmation of public control exposure is pending. No live HTTPS endpoint
is claimed. Record the
actual assigned URL only after it is verified, and validate forwarded host/scheme
and browser-origin handling against this middleware. Do not assume
`CC_TMUX_PUBLIC_URL` is a no-auth tunnel setting: the current implementation
requires a token when that variable is set. The no-pairing contract must remain
intact when the tunnel integration is finalized.

Give each HQ a distinct stable node ID. A target with only `id` and `label` probes
the current machine; `ssh` probes an existing alias using batch-mode SSH and
`python3`; `url` contacts another HQ. SSH probes send source over stdin and install
no daemon. Configure SSH trust separately when using that option.

Run one application process per HQ: locks, action receipts and caches are
process-local. Aggregation does not transfer a peer's worker registry or history
ownership. `CC_TMUX_MANAGE_WORKERS=0` is a preview/operational switch, not a
second-daemon architecture or a read-only API mode.

Adopted sessions retain their existing launcher, process, account, and permissions.
Discovery does not enroll them in the worker registry, revive them, or reap them.
The fleet may also show managed workers, marked `managed`; worker launch/kill and
routines still operate on the coordinator's local worker namespace. Fleet IDs
encode host plus pane/server/process birth identity. They survive polling and
reconnects to the same process, but are not durable conversation IDs after restart.

## Mobile API

### HQ peer cascades

Configured peer `url` values are HTTP(S) origins without credentials, query or
fragment, and with no path other than `/`. Session list/detail/action requests
cascade; worker spawn/kill, routines, one-shot requests and push preferences
remain local to the HQ being called.

Each hop prefixes display host paths and wraps route IDs, preserving
`owner_node_id`, `owner_session_id` and `origin_id`. Duplicate origins are merged,
preferring a reachable copy over an unreachable one. Treat IDs as opaque: changing
a peer route can change a frontend ID even for the same owner. Writes preserve
the revision and request ID through the route; the owner checks the exact pane.

`X-HQ-Path` carries visited node IDs. Cycles, repeated IDs and excessive depth
return 508 (eight-node traversal limit). Peer HTTP calls make one attempt, do not
follow redirects, time out after 20 seconds, and limit responses to 8 MiB. A
transport failure can leave action delivery unknown; no automatic retry occurs.
The current peer client sends no pairing credentials, matching the requested
no-token runtime.

An unavailable peer retains its last successful snapshot in memory when one
exists. Previously non-ended records become `unreachable`, `stale=true`, and
`online=false`; confirmed ended records remain ended. This reachability cache
does not survive restart. A peer never seen successfully contributes an explicit
host error, not invented session history.

### Endpoints

All mutation requests require `Content-Type: application/json`, including empty
bodies (`{}`) for logout and test push. The contracts live in
[server.py](src/cc_tmux_api/server.py), [fleet.py](src/cc_tmux_api/fleet.py), and
[fleet_probe.py](src/cc_tmux_api/fleet_probe.py).

| Method/path | Request or response |
|---|---|
| `GET /api/auth` | `{required, authenticated}`; public |
| `POST /api/auth/login` | `{token}` → `{ok}` and signed pairing cookie; public, rate limited |
| `POST /api/auth/logout` | `{}` → `{ok}`; clears this browser's cookie |
| `GET /api/sessions` | `{node_id, hosts, sessions, updated_at}`; hosts report `available`, lifecycle and optional `error` |
| `GET /api/sessions/{id}` | Session detail, including `tail`, `messages`, recognized `menu` or null, and `transcript_status` on live probe detail |
| `POST /api/sessions/{id}/action` | `{action, revision, request_id, text?, key?, index?}` → `{ok, id, request_id, revision, refresh_required}` |
| `GET /api/push` | `{available, public_key, reason?}` |
| `POST /api/push/subscribe` | `{subscription}` → `{ok}`; browser PushSubscription JSON |
| `DELETE /api/push/subscribe` | `{endpoint}` → `{ok}` |
| `GET /api/push/preferences` | `{muted: [session_id, ...]}` |
| `POST /api/push/preferences` | `{session_id, muted}` → current preferences |
| `POST /api/push/test` | `{}` → `{sent, failed, pending}`; explicitly sends to enrolled subscriptions |

Session summaries include `id`, `host`, `host_label`, `host_path`, `title`,
`workdir`, `managed`, `federated`, `owner_node_id`, `owner_session_id`, `origin_id`,
`status`, `activity`, `status_emoji`, `has_question`, `attention_kind`, `revision`,
`event_id`, and `notification_eligible`, alongside lifecycle fields below.
Session `updated_at` is the latest known positive event timestamp, tab timestamp,
or mapped transcript modification time, and is null when none is known. Polling
alone does not advance it. Top-level and host `updated_at` values remain snapshot
observation times. A failed host observation is not evidence that its sessions ended.

Live probe detail reports `transcript_status`: `available` (parsed messages),
`identity_missing` (no exact conversation UUID), `file_missing` (no mapped file
found), or `parser_empty` (mapped file yielded no readable messages). Empty
conversation views explain the status and embed terminal output with an Open
terminal control; non-live views label any retained output as last-known.
Transcript recovery uses a bounded, unique cc-notify route tied to the exact
pane/socket/cwd and current process ancestry. A descendant Claude UUID cannot
be attributed to its outer session; there is no newest-file/cwd-only fallback.

### Lifecycle and activity

| Lifecycle | `alive` | Meaning and controls |
|---|---|---|
| `alive` | `true` | Process observed in a successful census; controls still require current identity/revision and recognized input state |
| `ended` | `false` | Successful census confirmed a previously observed process absent; retained summary is read-only |
| `unreachable` | `null` | Availability cannot be established; retain last-known context and disable controls |

`last_seen` is the coordinator time of the last live observation. `ended_at` is
the first successful census confirming absence, not the exact OS exit time. Both
are Unix seconds, separate from activity `updated_at`. Confirmed ends persist
through outages; observing the same identity alive clears its end. A new process
generation has a new ID.

[session_history.py](src/cc_tmux_api/session_history.py) owns
`CC_TMUX_STATE_DIR/session-history.sqlite3`. It stores summaries for directly
probed local/SSH hosts before peer aggregation, never transcript text, terminal
tails, menus or actionable revisions. Ended records expire after 30 days; live
and unreachable records are not age-pruned. Retained local/SSH records clear
`menu`, `control` and `revision` and suppress notification eligibility. Their
`activity` is null for non-live history records, while `last_activity` preserves
the most recent known activity (including records saved before that field was
introduced). `status`, `status_label`, `last_status`, `last_status_label` and opaque
outcome emoji retain prior context; use `lifecycle` for liveness. Live observations
refresh both `activity` and `last_activity`. Known ended detail reads are
read-only and actions return 409. Unreachable detail/action requests can return
a transport error rather than a saved transcript.

Activity and `status_emoji` describe work or an outcome, not process liveness.
Recognized live questions outrank earlier outcome metadata; the probe exposes
`has_question` and `attention_kind`. The UI separates lifecycle and activity,
prioritizes live attention items, and places pending questions above the composer.
Drafts remain available, but ordinary sends are disabled while a question is
open. Old waiting status does not make ended or unreachable sessions actionable.

### Actions and notifications

Actions are `send` (text plus Enter), `type` (text only), `key`, `select` (zero-based
option), and `submit` (recognized review screen). Text requires a verified empty
prompt, or a focused free-text option for `type`, and is limited to 16,384 UTF-8
bytes. Keys are `Up`, `Down`, `Left`, `Right`, `Space`, `Enter`, `Escape`, `Tab`,
`BTab`, and `C-c`. Supply the latest detail's `revision` and an 8–128 character
`request_id`. The UI sends individual navigation keys and refreshes between steps.

Stale identity/revision conflicts return 409; validation can return 400/422 and
unavailable hosts return 503. Successful actions require a fresh detail read;
their returned revision describes the checked screen, not an acknowledgement
from Claude. Request receipts suppress repeat execution within this coordinator
process, including uncertain failures. They do not survive restart. Neither the
UI nor transport automatically retries actions; inspect the refreshed session
after uncertainty. Local keyboard input can still race the final tmux write.

The application lifespan always starts the fleet monitor, independently of push
configuration, an open browser, or `CC_TMUX_MANAGE_WORKERS`. Each iteration calls
`fleet.snapshot()` and then waits five seconds; collection time is additional, so
this is not a fixed five-second sampling guarantee. The fleet cache lasts three
seconds. Lifecycle observations continue while the server runs without a browser.
Local/SSH collection through history commit is serialized, preventing an older
census from committing after a newer one and falsely ending sessions.

Only when push is available does the monitor pass that snapshot to
`push.observe(..., deliver=False)` to persist events. A separate asynchronous task
calls `PushService.deliver_pending` on the existing outbox, so a slow provider
does not stall lifecycle collection. Each new session is baselined silently.
Normalized eligible events enqueue short status notifications; muted/no-news
events are suppressed. The SQLite outbox persists per subscriber, retries with
backoff up to five attempts within one hour, and removes 404/410 subscriptions.
Delivery is at-least-once across a crash at provider acceptance. Endpoints are
restricted to browser push providers, with redirects disabled. Preferences are
shared server-wide, not per device. See [notifications.py](src/cc_tmux_api/notifications.py).

The service worker caches only the app shell; API/transcript data is not cached
for offline use. Drafts are stored in browser local storage. Pairing logout clears
the cookie; it does not revoke the shared token or remove push subscriptions.

## Development and validation

```bash
uv sync --dev
uv run pytest -q
node --check src/cc_tmux_api/static/app.js
node --check src/cc_tmux_api/static/sw.js
```

Backend tests mock host actions and push delivery; API tests do not run application
lifespan, except the isolated monitor regression with a mocked fleet. Do not start
the server against the real worker registry for a test:
use a temporary `BCE_WORKSPACE_DIR` and `CC_TMUX_STATE_DIR`, and set
`CC_TMUX_MANAGE_WORKERS=0`. Fleet monitoring still starts without VAPID or push;
mock fleet collection when a test runs the lifespan.

The frontend contract suite uses Playwright from an external npm environment
with Chrome installed:

```bash
MOBILE_BROWSER_ENV=/path/to/playwright-environment node tests/frontend_test.mjs
```

See [the browser evidence index](tests/browser/README.md) for the separately gated
real-preview harness and its evidence. Automated tests and desktop emulation do
not establish native iPhone keyboard behavior, locked-screen push, or live menu
action correctness. Historical implementation evidence is in
[the original devlog](docs/devlog/claude_20261008-mobile-ui-implementation.md).
Current federation/lifecycle checks and deployment limits are recorded in
[the latest handoff](docs/devlog/claude_20261008-federated-hq-lifecycle.md).

## Security

The requested backend leaves the token/public URL empty and serves HTTP on
explicit loopback/Tailscale interfaces. API access has no application
authentication in that mode; `/api/auth` returns
`{required:false, authenticated:true}`. Host allowlisting, same-origin checks and
JSON mutation requirements still apply. Public HTTPS will be supplied by an
external tunnel, with its URL and integration still pending. No Serve or Funnel
is planned. Mobile push/PWA device delivery remains unverified; server push
availability means configured, not delivered.

Optional token pairing still covers legacy worker/routine/oneshot routes as well
as sessions and push. Any paired client has the full API, including launching
permission-skipping workers; it is not a read-only role. This optional mode is
not the current deployment plan.

[security.py](src/cc_tmux_api/security.py) accepts `Authorization: Bearer <token>`
or a signed 30-day HttpOnly, SameSite=Strict cookie. Cookies are Secure with an
external URL or HTTPS request. Rotating the token invalidates existing cookies.
Login permits ten attempts per client address per minute. Hostnames are limited
to loopback names, `CC_TMUX_ALLOWED_HOSTS` and the optional public hostname; cross-origin/cross-site API
requests are rejected, and no CORS access is enabled. Treat the private config,
pairing token, and push state as secrets. The state directory is mode 700 and
private key/database files are mode 600.
