# Upstream mirror: native sessions on claude.ai too (0.7.0)

## Why

`tcc` sessions bridge to HQ (`native_remote.py`), so claude.ai and the Claude app
could not see them. The user chose to keep HQ as the CLI's only bridge and have
HQ itself register each session with Anthropic's real Remote Control. Rejected:
headless wrappers (hapi/happy/paseo) lose the TUI and drop the session off
claude.ai; a pure controller hub fails whenever Anthropic's relay does. If the
relay is down, HQ control keeps working and the mirror retries in the background.

## Design

`src/cc_tmux_api/rc_mirror.py` is a Remote Control *worker* toward Anthropic,
one `_Link` per session with two daemon threads:

- **pump** drains an outbox (max 1000 items, oldest dropped with a log line):
  register (`POST /v1/code/sessions`, then `/bridge`), forward items, heartbeat,
  re-bridge at 80% of `expires_in`, and `PUT /worker` the current state once per
  new epoch before anything else. Network errors, 5xx and 401/408/409/429 back
  off exponentially (1 s → 60 s) and retry the same item. Any other 4xx drops
  that item with one log line per path and status (what the real CLI does with a
  non-retryable status), so one refused batch never blocks what is queued
  behind it. 410 anywhere, or 404 on `PUT /worker` or the heartbeat, ends
  mirroring; a 404 on an events path only drops the item, because it may mean
  the endpoint (`/worker/internal-events`) does not exist upstream.
- **listener** reads `GET /{cse}/worker/events/stream` with `from_sequence_num`
  and `Last-Event-ID` set to the persisted cursor and injects each
  `client_event` frame. Other frame kinds (`ephemeral_event`, `session_update`,
  `delivery_update`, `catch_up_truncated`) are skipped; a frame with no `event:`
  line counts as a client event.

401/409 re-bridge once per call. `bridge()` itself makes no worker call, so a
worker token Anthropic keeps refusing costs at most one bridge per backoff
window per thread (pump and listener), not a recursion.

| Local event (CLI → HQ) | Upstream call |
|---|---|
| `POST /v1/code/sessions` (create) | starts the link; `x-hq-private: 1` sets `session.private` and skips it |
| `PUT /{sid}/worker` | `PUT /{cse}/worker` with the same `worker_status` / `external_metadata` |
| `POST /{sid}/worker/events` or `internal-events` | same operation, non-duplicate items verbatim |
| HQ UI `send` (`act`) | `POST /{cse}/worker/events` with the user payload minus `client_platform`, `server_received_wall_ms`, `session_id` |
| `POST /{sid}/worker/events/delivery` | `POST /{cse}/worker/events/delivery` for events that came from upstream, with upstream ids |
| heartbeat / delivery / diagnostics / stream GET | ensures the link exists (this is how mirroring resumes after a restart) |
| HQ UI `respond` (permission answered in HQ) | `POST /{cse}/worker/events` with `control_cancel_request {request_id}`, as the CLI does for a terminal-side answer, so claude.ai and the app drop the prompt |
| `POST /{sid}/archive` | `POST /{cse}/archive` (200/404/409/410 all count as done) |
| create carrying `x-hq-mirror` | 508: an account proxy without the passthrough routed the mirror's own registration back to HQ. Refusing it stops a phantom-session loop; the mirror retries with backoff and starts working once the proxy is updated |

Upstream → local: `user`, `control_request`, `control_response` and
`control_cancel_request` go through `NativeRemote._queue`, so its size limits
apply. A full queue (503) stops the stream and replays from the cursor later,
with a backoff that keeps growing until an event is injected again; an
oversized event is skipped with a log line. Users' messages enter HQ's
transcript, and an upstream `control_response` clears HQ's pending prompt. The
local→upstream event id map lives in `session.upstream.delivery` and is pruned
together with `acknowledged`.

Auth: the mirror holds no Anthropic credential. Launch calls send
`Bearer proxy-account-<CC_TMUX_RC_UPSTREAM_ACCOUNT>`, worker calls the worker JWT
(memory only, never on disk). Every request carries `x-hq-mirror: 1` so the
account proxy forwards it to Anthropic instead of routing it back to HQ.

Lock discipline: hooks run under NativeRemote's lock and only enqueue (plus
starting a thread on first use). Mirror threads reach NativeRemote only through
`upstream_snapshot/state/activity/update/inject`, each a short critical section
with no network inside.

Persisted per session (`native-remote.json`): `upstream.{id, session_url,
status, cursor, delivery}`. Status is `connecting`, `connected`,
`reconnecting` (back to `connected` as soon as a send succeeds while the stream is
up), `disconnected` (CLI silent for 10 min, or any HQ restart, since no link survives
one; the next CLI call resumes) or `ended` (upstream 404/410 or an `end_session` control request; the HQ session
stays alive and is never mirrored again). Heartbeats are only sent while the CLI
was seen in the last 90 s, so a dead CLI shows disconnected on claude.ai.

Fleet rows carry `private` and `upstream: {session_url, status}`; the session bar
links "claude.ai" (with the status when not connected) or shows "Private".

## Configuration

`CC_TMUX_RC_UPSTREAM_PROXY` enables it; `CC_TMUX_RC_UPSTREAM_CA` is the proxy's
CA bundle; `CC_TMUX_RC_UPSTREAM_ACCOUNT` defaults to `personal`. Read in
`native_remote.get_backend()`; `cli.py --config` already accepts any
`CC_TMUX_*` key. `httpx` became a runtime dependency.

## Verification

- `uv run pytest -q`: 310 passed (289 before + 21 in `tests/rc_mirror_test.py`),
  stable over repeated runs. The fake upstream is an `httpx.MockTransport` with a
  real streaming SSE body. Covered: register (body, placeholder auth,
  `x-hq-mirror`, state PUT), forwarding CLI events/state and HQ-typed messages,
  inject + delivery id mapping, upstream answer clears HQ's prompt, heartbeat,
  re-bridge after 401 with the new epoch/JWT, stream reconnect with cursor
  resume and no double injection, private skip, restart resume of the same
  upstream session from the persisted cursor, archive and controller end,
  a hung upstream not delaying CLI requests, env configuration.
- Review round (same day), one regression test each, every one failing on the
  pre-fix code: a refused 4xx batch is dropped and later state/archive still go;
  404 on `PUT /worker` ends mirroring; a permanent 401 makes 10-11 bridge calls in
  0.5 s at 0.1 s backoff (719 before, ending in RecursionError); non-client
  frames do not reconnect the stream; status returns to `connected` after one
  retried send; an HQ answer posts `control_cancel_request` upstream; a full
  local queue reconnects at most 5 times in 0.8 s (backoff grows); a looped-back
  mirror create gets 508 and creates nothing; a failed first state PUT is
  retried and the stream still opens; a restart shows `disconnected`. The fake
  upstream now replays emitted events after `from_sequence_num` like Anthropic.
- `node --check` on `app.js`.

## Not verified live

Nothing ran against api.anthropic.com. Unproven: that Anthropic accepts a
worker registered this way and shows it on claude.ai; that
`/worker/internal-events` exists upstream; the exact `end_session` signal;
whether claude.ai minds the CLI's replies to HQ-only control requests (e.g. the
computer-use `initialize`) appearing in its log. The proxy side
(`x-hq-mirror` passthrough, `x-hq-private` marking) lives in
claude-code-with-codex. Server-generated title adoption (brief item 8) is not
implemented. HQ interrupt sends no upstream cancel: the CLI posts its own
`control_cancel_request` when an interrupt aborts a pending prompt, and that
flows up with its other events (inferred from the CLI posting cancels on its
outbound path, not observed). Deploy order still matters for a clean start:
update the account proxy first; with the old proxy the 508 guard stops the
loop but nothing is mirrored.
