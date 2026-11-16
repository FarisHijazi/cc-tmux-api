# Codex fleet integration, 2026-11-16

`codex_remote.py` attaches to the already-running local Codex app-server's Unix
WebSocket. `fleet.py` publishes its loaded UUIDs as the `@codex` source and reuses
existing peer routes, revisions, per-target locks and delivery receipts.
`SessionHistory` remains the only lifecycle-summary writer; disappearance from
the loaded list or a disconnected daemon never confirms a thread ended.
Runtime settings and supported controls are in `README.md#existing-codex-sessions`.

Read-only compatibility checks passed on app-server 0.166.0 and 0.169.0 using
isolated VM toy conversations. Both reported direct-input support, and the
official `thread/turns/list` summary and `thread/items/list` returned the exact
readiness exchange with stable message IDs. A formerly loaded blank toy thread
subsequently returned a guarded 409, as expected. These reads do not establish
live send, interrupt or permission-response proof.

The HQ UI treats Codex as a native conversation, labels its assistant correctly,
and gates send/interrupt/respond by capabilities. It offers no terminal keys or
Claude model/Remote Control controls for Codex. MCP retains the provider and
source message IDs while keeping its existing opaque routing and tool names.

## Deployment constraints

All existing HQ owners are retained. Dev's previous Rust implementation embedded
its Python probe and could not load this adapter. The same-owner Python cutover
passed fresh API/state parity: 206 identical cases and three understood changes
(Codex UI assets, additive native send message ID, and upstream `abf7011` safe-shell
worker prompt wording). Native stream/ACK/archive, reciprocal native-state
loading, scheduler and domain-wrapper checks all passed. No native worker streams
were present in Dev's state; SQLite schemas matched and its VAPID key was intact.

Every rollout must retain private config, native state, session history and
domain wrappers, and compare existing tmux server/pane plus Claude/Codex process
identities before and after. User services must use `KillMode=process` before
restart because an existing tmux server can belong to the HQ cgroup. Rollback
restores only the previous executable while retaining that kill mode. No shared
network service or Docker runtime is part of this rollout.

## Activated release

All six HQ owners now use 0.9.0 wheel SHA-256
`fe57d370c03ddfb4a7caa51362f253e5f328df6fa8d1a8125fa8d88ab4e90a4f`.
Immutable environments are under `~/.local/share/cc-tmux-api/releases/<sha>/`;
private runtime config, state, listener and wrapper ownership remain unchanged.
Older Python environments were copied before installing the wheel and its new
dependencies. Dependency checks passed; Dev uses a fresh environment from the
frozen lock. No original environment was modified.

| Node | Existing owner | Activated HQ PID | Preserved panes |
| --- | --- | ---: | ---: |
| Dev | `cc-tmux-hq.service` | 3682767 | 10 |
| Second domain VM | Existing domain unit (see release manifest) | 1586553 | 10 |
| First domain VM | Existing domain unit (see release manifest) | 683876 | 3 |
| Sahalat | `cc-tmux-api.service` | 1667363 | 0 |
| Ftower | `cc-tmux-hq.service` | 4033838 | 0 |
| Mac | `com.local.cc-tmux-hq` | 46910 | 31 |

Each remote release contains `codex-stage.json`, `before.json` (private snapshot),
`state-backup/`, and `codex-deployed.json`, including exact preserved PID/birth
tuples. Mac evidence is `deployment.json` in the same release. Its first health
deadline expired and automatically restored the preceding owner; the second
activation passed without code changes. No cause beyond that deadline is claimed.
The second domain VM's existing wrapper needed its normal 90-second graceful-stop deadline;
the scoped `KillMode=process` preserved the tmux server and all native clients.

Remote rollback restores the preceding executable/arguments from
`codex-stage.json` in the existing `zzzz-codex-fleet.conf` drop-in, retaining
`KillMode=process`, followed by user-manager reload and only that unit restart.
Do not remove the protection before restarting. The existing original releases
and configs remain available. Mac rollback uses its recorded LaunchAgent backup.

The root snapshot after activation exposes ten loaded Codex threads: five on Mac,
four on the second domain VM and one on Dev, including the ongoing Mac conversation and
The second domain VM's existing CLI conversation. The first domain VM's existing daemon is reachable with no
loaded threads. Ftower and Sahalat still have no tmux server; their missing local
socket is an existing empty source, while their HQ peers are healthy. The existing
second domain dashboard returns HTTP 200 from both Mac and Dev.

All four reserved Claude/Codex toy details were read through their exact root
routes with unchanged workdirs and identities, then handed to the ACP test owner.
This does not itself prove live continuation or every approval edge case.
Existing native Claude records survived the root restart and showed fresh worker
heartbeats/events. Their online stream leases fluctuate during re-bridging;
stable reconnection is a separate check, not inferred from HTTP 200 or preserved
records.

The frozen source passed 462 Python tests, including 29 Codex protocol cases,
and the existing browser contract suite at 390px and 1440px with added Codex
capability, label, permission and semantic-interrupt checks. Browser mocks are
not evidence of native approval delivery.

The existing ftower MCP adapter was also switched to this wheel, preserving all
unit settings except ExecStart; PID 4038394. Its original unit backup and evidence
are `mcp-service-before.service` and `mcp-deployed.json` in the release. After the
graceful restart, public anonymous and invalid bearer calls returned 401 and an
authenticated MCP discovery succeeded. No second adapter was deployed.

## Large-registry responsiveness follow-up

The first ACP live run stopped before any write when a selected Claude detail
read timed out. Concurrent diagnostics isolated the delay to the aggregator:
Domain-owner auth/detail returned in 0.101/0.409 seconds, while even ftower loopback
auth/detail took 13.787/13.793 seconds. Its native registry contained 804 sessions
and 106 MB of JSON; synchronous whole-registry saves run inside native async
request handlers. The original session records and conversations were retained.

Changing only the existing writer from `json.dump` to one
`out.write(json.dumps(...))` preserves byte-for-byte format, private permissions,
atomic replacement and synchronous durability before ACK. On an actual-data
scratch copy on the same filesystem, serialization/write improved from 0.842 to
0.453 seconds with byte-identical output. No production state was changed by the
benchmark. Its atomic format/permissions regression passes with the 48-case native
suite. This optimization alone is not proof that the event-loop blockage is fixed;
async handler and stream lock scheduling require their own verification.
