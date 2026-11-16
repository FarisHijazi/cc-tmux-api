# Codex fleet integration, 2026-11-16

`codex_remote.py` attaches to the already-running local Codex app-server's Unix
WebSocket. `fleet.py` publishes loaded UUIDs through the `@codex` source and
reuses existing peer routes, revisions, target locks and delivery receipts.
`SessionHistory` remains the sole lifecycle-summary writer; an unloaded thread
or disconnected daemon is unreachable, not proof that its conversation ended.
Runtime settings and supported controls are in `README.md#existing-codex-sessions`.

Read-only compatibility passed on app-server 0.166.0 and 0.169.0 using isolated
VM toy conversations. Direct input, `thread/turns/list` summaries and
`thread/items/list` returned the readiness exchange with stable message IDs.
A subsequently unloaded blank toy thread correctly returned a guarded 409.
The existing updater upgraded the dev daemon before deployment; its CLI stayed
running. This is separate from the HQ upgrade.

HQ shares native conversation/control rendering for Codex and respects action
capabilities. It labels the assistant correctly and offers neither terminal keys
nor Claude model/Remote Control controls for Codex. MCP retains provider and
source message IDs, with unchanged opaque routes and tool names. No new Codex
process, session writer, permanent service, tunnel or T3 fork was added.

## Current deployment

All six HQ owners and the existing public MCP adapter run 0.9.2, wheel SHA-256
`4bc9b37f7281a4d100f32a7044005514f95e67df39ec1db25bb5feb734f9969f`.
Releases live at `~/.local/share/cc-tmux-api/releases/<sha>/`. Private config,
native state, session history, push keys, wrappers and listeners are retained.
Older environments were copied before installing the wheel; dependency checks
passed. No preceding environment was modified.

| Node | Existing owner | Activated HQ PID | Preserved panes |
| --- | --- | ---: | ---: |
| Dev | `cc-tmux-hq.service` | 116015 | 10 |
| Second domain VM | Existing domain unit (release manifest) | 2190032 | 10 |
| First domain VM | Existing domain unit (release manifest) | 933642 | 3 |
| Sahalat | `cc-tmux-api.service` | 1683288 | 0 |
| Ftower | `cc-tmux-hq.service` | 4076127 | 0 |
| Mac | `com.local.cc-tmux-hq` | 71060 | 33 |

Each activation compared every existing Claude/Codex/tmux PID and process birth,
pane/server identity and live non-native route. All survived. All VM units retain
`KillMode=process`; this is essential because a preexisting tmux server can belong
to an HQ cgroup. User sessions were never restarted. During the preceding deployment, the first root census briefly
missed one peer; the same running owner recovered with all 34 preceding live
non-native routes, without a network or service change. The earlier Mac activation
hit its health deadline and restored the old owner; its retry passed unchanged.
Those transient checks are retained rather than counted as initial successes.

Dev changed from its existing Rust binary to Python under the same service after
fresh isolated API/state parity: 206 identical cases and three understood changes
(Codex UI assets, additive native send message ID, and upstream `abf7011` safe-shell
prompt wording). Native stream/ACK/archive, reciprocal native-state loading,
scheduler and embedded-wrapper checks passed. Its native worker state was empty,
SQLite schemas matched and the VAPID key remained intact. No duplicate Codex
implementation or service was introduced.

Remote release evidence: `codex-stage.json`, private `before.json`,
`state-backup/`, `service-override-before.conf`, and `codex-deployed.json`.
Mac evidence: `deployment.json` and `launchagent-before.plist`.
The existing MCP adapter is PID 4076465; `mcp-service-before.service` and
`mcp-deployed.json` prove its only unit change was ExecStart. Authenticated public
discovery succeeded; anonymous and invalid bearer requests returned 401.

Rollback restores the preceding executable from the release manifest into the
existing `zzzz-codex-fleet.conf`, retaining `KillMode=process`, then reloads the
user manager and restarts only that unit. Mac uses its saved LaunchAgent; MCP
uses its saved unit. Do not remove kill-mode protection before restarting.
Preceding releases and all durable state remain available. Shared proxy, tunnel,
Docker and machine lifecycles are outside this rollout.

## Large-registry responsiveness

The initial ACP live run stopped before any write when a selected detail read
timed out. Concurrent reads isolated the delay to the aggregator: its auth and
peer detail both took 38.7 seconds while the owning VM returned auth/detail in
0.115/1.516 seconds. Its native registry had 804 sessions and 106 MB of JSON;
synchronous saves and registry-lock waits occupied the HTTP event loop.

The existing writer now uses `out.write(json.dumps(...))`, retaining exact JSON
format, private permissions, atomic replacement and persistence before ACK.
On an actual-data scratch copy, serialization/write improved from 0.842 to 0.453
seconds with byte-identical output. No history was dropped or compacted.
Native request handlers and every SSE registry-lock operation now run off the
HTTP event loop, separately from FastAPI's request pool. Stream registration and
shielded cleanup preserve epoch/lease checks even if setup is cancelled.

After the final 0.9.2 rollout, three rounds each ran five concurrent reads: auth, native Claude
detail, two reads of the same remote Claude detail, and a fleet snapshot. All 15
returned HTTP 200. Auth took 0.010–0.247s; native detail 0.026–2.057s; remote detail
0.416–0.791s; snapshots 0.320–5.446s. No peer was unavailable. Evidence is
`concurrent-read-validation.json` in the root release. Native stream leases
reconnected; earlier bridge IDs also underwent explicit archive/recreation, so
record preservation must not be misreported as all old bridges remaining online.

## Interrupted-session status

A real ACP Stop reached an isolated Claude process and its transcript recorded
interruption, but HQ still reported working: an older prompt hook overrode the
existing publisher's newer idle transition. The probe now carries the verified
publisher timestamp and lets strictly newer idle supersede only that stale work
hook fallback. Equal, older or absent timestamps retain working; live spinner,
question and permission detection retain priority. PID, birth, domain and exact
pane/window checks are unchanged. A read-only candidate probe on the already
interrupted process returned idle without sending another input.

The 0.9.2 source passes 490 Python tests, including timestamp ordering,
publisher identity rejection, busy/menu priority and metadata propagation.
The affected existing owner now returns idle through the root API for the same
already-interrupted UUID and process birth, without another Stop. Its activation
preserved all 15 protected processes, ten panes and thirteen live routes.
Every existing owner and the MCP adapter now use that same immutable release.
All four reserved toy details returned idle/alive after activation; public
authenticated MCP discovery returned 41 routes and both anonymous and invalid
bearer calls returned 401. No second interrupt was sent to prove the status fix.

## Validation and live-test boundary

The final 0.9.2 source passes 490 Python tests. The browser contract suite passes
at 390px and 1440px with Codex capability, label, permission and semantic-interrupt
checks. Mocked browser/protocol tests do not establish live approval delivery.

The final root snapshot included eleven loaded Codex threads, including the
ongoing Mac conversation, the original domain-VM CLI conversation and both
isolated Codex toys. The first domain VM's existing daemon is reachable with no
loaded threads. Ftower and Sahalat have no local tmux server; their missing local
socket is an existing empty source, while their HQ peers are healthy. The existing
second domain dashboard returned HTTP 200 from both Mac and Dev.

All four reserved Claude/Codex toy details were read through their exact root
routes with unchanged workdirs and identities, then handed to the ACP test owner.
The final private fixture manifest is
`~/.codex/tmp/fleet-mcp/codex-fixture-manifest-092-20261116.json` on the operator Mac.
Continuation, reload, interrupt and approval E2E results belong to the cc-proxy
integration report; readiness and preserved identities alone do not prove them.
