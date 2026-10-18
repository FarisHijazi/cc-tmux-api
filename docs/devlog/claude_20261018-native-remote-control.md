# Native Remote Control behind the existing HQ UI

## Plan and owners

Replace terminal-dependent interaction for newly connected sessions with Claude's
structured Remote Control protocol. Keep [fleet](../../src/cc_tmux_api/fleet.py)
as the session API, routing, locking and receipt owner, and
[SessionHistory](../../src/cc_tmux_api/session_history.py) as lifecycle-summary
owner. Add the transport in [native_remote.py](../../src/cc_tmux_api/native_remote.py)
inside the existing FastAPI process. The [web UI](../../src/cc_tmux_api/static/app.js)
renders native approvals/questions and hides terminal-only controls. Existing
tmux sessions retain their previous transport until relaunched.

The paired change in `claude-code-with-codex/scripts/claude-account-router`
extends the existing session-scoped mitmproxy owner. The new
`account_remote_route.py` selects newly registered native sessions, strips
Anthropic credentials, injects a separate HQ launch secret, and subsequently
accepts only the session's issued worker token. Unknown sessions and unrelated
account/model endpoints retain their existing route. No global Claude settings,
system hosts or trust stores are changed.

## Research and reuse

[Official Remote Control documentation](https://code.claude.com/docs/en/remote-control)
describes continuing an interactive CLI session. Community
[Companion](https://github.com/jackneil/Claude-websocket) uses the headless
`--sdk-url` interface. The existing
[remote-control emulator](https://github.com/puxu-msft/my-claude-plugins/tree/main/claude-remote-3rd)
demonstrates native server-mode MITM against older versions, but its experimental
server broadcasts injected input to all connected workers and lacks current
environment-less `/v1/code/sessions/{id}/bridge`. It cannot safely serve this
multi-session UI unchanged. This implementation reuses FastAPI streaming and
the existing proxy transport, adding the missing session-specific protocol.

At the user's request, parallel agents inspected
[the older source snapshot](https://github.com/yasasbanukaofficial/claude-code/tree/a371abbe75ffa0d0a3c92290e2bbf56a7ef54367/src).
`cli/transports/ccrClient.ts`, `bridge/replBridgeTransport.ts`,
`bridge/bridgePermissionCallbacks.ts` and the AskUserQuestion implementation
confirmed delivery acknowledgements, request-correlated permission maps and
question-text-to-answer mappings. Older source is not proof of current behavior:
installed Claude Code 2.1.302 additionally classifies messages without
`client_platform` as peer-agent messages. Human HQ input supplies
`client_platform: web_claude_ai`; it is never added to worker-origin messages.
The snapshot is dated April 5, 2026, but does not identify an exact Claude release:
it references build-time `MACRO.VERSION` and lacks a package manifest. Source
files were read, not executed. Delivery status is monotonic; only `processed`
suppresses replay, while `received` and `processing` remain replayable.

## Observed protocol

Real interactive Claude 2.1.302 registers with `POST /v1/code/sessions`, supplying
`bridge`, title and cwd/model config. It expects `{session:{id}}`, then exchanges
`POST /{id}/bridge` for worker token, epoch, expiration and API base URL. Worker
state uses GET/PUT `/worker`, events use POST `/worker/events`, and inbound
commands use SSE `/worker/events/stream`. SSE frames carry `client_event`,
sequence number, event ID and SDK payload. Delivery updates contain worker epoch
and `{event_id,status}` entries. Control replies retain the native request ID.

The backend separates launch authentication from per-session worker authentication,
enforces epochs, supports one stream lease, retains bounded replay and tracks
acknowledgements. Pending requests are queued by request ID, not overwritten.
An offline stream clears controls without fabricating an end. Explicit archive
ends the session; archived protocol state follows thirty-day retention. Browser
actions still pass through existing fleet guards and peer routing. Native content
is private on disk; index responses exclude transcripts. Persistent permission
rule updates are not exposed by the UI.
Conversation rendering follows HQ's existing text-block contract. Thinking,
tool-result blocks and partial token deltas are not rendered; native structured
approvals and questions are handled separately. This is not complete native UI
parity, nor a claim that terminal parsing was the only possible failure source.

## Validation record

Automated coverage includes native registration/authentication, epoch rotation,
single-stream routing, acknowledgements/replay, malformed batches, pending
requests, bounded state, lifecycle, fleet action receipts and peer forwarding.
The existing Python suite and mobile/desktop frontend contracts also run.
Final automated checks: 265 Python tests passed, including 23 native protocol
tests. Ruff passed for the changed Python modules and tests; the 0.5.0 wheel
built successfully. The real-Chrome frontend contract suite passed at 390 px
and 1440 px, including legacy controls and native questions/permissions.

The first real CLI connected to the emulator and reported Remote Control active.
Real Chrome sent one prompt, rendered `HQ_NATIVE_OK`, answered the native question
with Blue and sent an interrupt. The actual CLI confirmed Blue and interruption.
Its follow-up inference failed on an older, already-running model router with a
missing-tool-call error; that run is not a successful full completion. The test
also exposed the human-provenance field above. Subsequent verification uses a
fresh session-owned model router. Initial browser automation attempted to fill
before detail readiness; no POST was sent, and the harness now waits for controls.

Final protocol review found that retried native control events can carry a
`request_id` without a UUID. The [backend](../../src/cc_tmux_api/native_remote.py)
now deduplicates these by control type and request ID, so a delayed worker POST
cannot resurrect an answered permission. The [native regression suite](../../tests/native_remote_test.py)
passes 23 tests, including persisted deduplication, processed-only replay
acknowledgements and a 10 MiB event batch with its JSON envelope.

The fresh interactive CLI used a session-owned model router. Real Chrome sent
one prompt, displayed `HQ_NATIVE_OK`, submitted Blue once, and displayed the
completed `HQ_ANSWER Blue`. The CLI independently showed the human prompt,
native question answer and final response. A separate four-action browser test
approved one Write request and denied another: the approved private test file
contained the expected text and the denied file remained absent. Both native
permission cards and final model responses were checked.

Backend restart preserved seven messages without replay, showed an unreachable
session with controls disabled while offline, then accepted a single new prompt
and displayed `HQ_RECONNECTED`. The first shutdown revealed that an unbounded
Uvicorn graceful drain leaves a long-lived SSE connection open after releasing
the listener. [Server startup](../../src/cc_tmux_api/server.py) now bounds that
drain to five seconds. The retained old preview process was identified and
terminated by its exact owned PID before the reconnection check.
The final build exited after a single SIGTERM in 5.21 seconds with a live SSE;
nine conversation messages remained unchanged across that restart.

Private browser evidence is under `tmp/cchq-native-preview/browser-evidence/`:
`native-e2e-final`, `native-permissions` and `reconnect-result.json`. These are
runtime artifacts outside the repository and contain actual session values.

## Deployment and installed launcher

The Mac root checkout was fast-forwarded to `181e02c` and synchronized with
`uv sync --locked`. The existing `com.local.cc-tmux-hq` launchagent was restarted
with its existing config. A separate 0600 registration key is stored at
`~/.config/cc-tmux-api/native-token`; only `CC_TMUX_NATIVE_TOKEN` was added to
the private config. Other settings and legacy session discovery remain enabled.
The config backup is `~/.config/cc-tmux-api/hq.before-native-20261018.json`.

The paired model-proxy checkout was fast-forwarded to `a7fa286`. The local
`~/.local/bin/claude-hq` shortcut delegates to the existing account launcher,
starts a private inference router, reads an access-only snapshot of the current
Claude login, and selects the Mac native backend. Example:

```sh
claude-hq -- --model gpt-5.6-sol
```

No files in the source Claude login directory were modified. Both isolated live
test CLIs exited normally; the existing inference router was never restarted.

The ftower aggregator uses immutable wheel release
`00c8cbd7d9008e288e7dbe00914d645e11a1f97e7232343c90e1cec5bf11a6cf`
under `~/.local/share/cc-tmux-api/releases/`. Dependencies were hash-locked and
checked before restarting only `cc-tmux-hq.service`. Its source unit remains
`/home/faris/media-server/compose/cchq/cc-tmux-hq.service`; the prior unit/config
backups have suffix `.before-native-20261018`. The previous `521d2afa…` release
remains available for rollback. No peer, HTTPS or identity-provider setting changed.
ftower also has its own 0600 `~/.config/cc-tmux-api/native-token` and matching
`CC_TMUX_NATIVE_TOKEN` config entry, enabling authenticated local native ingress;
federation itself does not require this launch secret.

Real Chrome opened the deployed ftower UI over a private SSH tunnel, found the
Mac session through `host_path=[macbook,@native]`, sent exactly one prompt and
rendered the completed `HQ_DEPLOYED` response at 390 px and 1440 px. The actual
CLI independently displayed the same prompt and response. No terminal tab,
horizontal overflow or browser errors were observed. Evidence is
`browser-evidence/deployed-federation/report.json` and its two screenshots in
the private preview directory. Both services returned healthy API responses;
unauthenticated native registration returned 401. The public hostname's `/`
and `/api/auth` still redirect to Authentik (302). Authenticated public browser
access was not re-tested; private federation and the public authentication gate
were checked separately.
After the test CLI exited, the federated record became ended with every control
disabled while retaining the response. The temporary preview process and exact
owned SSH forward were removed; the pre-existing SSH master was preserved.
Final runtime evidence is `tmp/cchq-native-preview/deployment/result.json`.

For rollback, restore the previous ftower unit and restart that service after
`daemon-reload`; preserve its persistent state. On the Mac, restore the config
backup and select the previous verified checkout/release before restarting the
same launchagent. Restoring config alone does not roll back application code.
