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

All six HQ owners and the existing public MCP adapter run 0.9.5, wheel SHA-256
`45925b3503d12b9d6c5982df3952096cb86ab71650eea701eb789f45a5c0cbfa`.
Releases live at `~/.local/share/cc-tmux-api/releases/<sha>/`. Private config,
native state, session history, push keys, wrappers and listeners are retained.
Older environments were copied before installing the wheel; dependency checks
passed. No preceding environment was modified.

| Node | Existing owner | Activated HQ PID | Preserved panes |
| --- | --- | ---: | ---: |
| Dev | `cc-tmux-hq.service` | 775878 | 10 |
| Second domain VM | Existing domain unit (release manifest) | 2216259 | 10 |
| First domain VM | Existing domain unit (release manifest) | 2617098 | 3 |
| Sahalat | `cc-tmux-api.service` | 1873597 | 0 |
| Ftower | `cc-tmux-hq.service` | 359421 | 0 |
| Mac | `com.local.cc-tmux-hq` | 75672 | 31 |

Each activation compared every existing Claude/Codex/tmux PID and process birth,
pane/server identity and live non-native route. All protected processes and panes
survived; the observed Codex unload during 0.9.5 verification is recorded below.
All VM units retain
`KillMode=process`; this is essential because a preexisting tmux server can belong
to an HQ cgroup. User sessions were never restarted. Each follow-up used a fresh immediate
baseline; user activity between upgrades changed counts without replacing any
process protected by that activation. During the preceding deployment, the first root census briefly
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
The existing MCP adapter is PID 362339; `mcp-service-override-before.conf` and
`mcp-deployed.json` record its existing kill-mode protection and changed ExecStart. Authenticated public
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

After the final 0.9.3 rollout, three rounds each ran five concurrent reads: auth, native Claude
detail, two reads of the same remote Claude detail, and a fleet snapshot. All 15
returned HTTP 200. Auth took 0.026–0.229s; native detail 0.047–0.430s; remote detail
0.628–1.080s; snapshots 0.254–2.615s. No peer was unavailable. Evidence is
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

Regressions cover timestamp ordering, publisher identity rejection, busy/menu
priority and metadata propagation.
The affected existing owner now returns idle through the root API for the same
already-interrupted UUID and process birth, without another Stop. Its activation
preserved all 15 protected processes, ten panes and thirteen live routes.
Every existing owner and the MCP adapter now use that same immutable release.
All four reserved toy details returned idle/alive after activation; public
authenticated MCP discovery returned 39 current routes and both anonymous and invalid
bearer calls returned 401. No second interrupt was sent to prove the status fix.

## Offered denial and isolated bridge recovery

A native approval may offer `cancel` instead of `decline`. Deny now prefers
`decline`, otherwise uses an explicitly offered `cancel`; an unoffered negative
decision fails before writing. Command and file approvals retain their exact
request/revision guards, and Allow never broadens to another offered choice.

One isolated Claude bridge had explicitly disabled and archived during the
earlier HTTP stall. The existing proxy reply-ID join identified its original
conversation; worker payloads themselves carry the remote ID. Recovery used the
same process, a verified empty input and one guarded `/remote-control` command.
Its new publisher bridge ID and seven exact original transcript UUIDs proved the
link. That bridge reconnected normally across the 0.9.3 owner restart. A test
window was temporarily enlarged to reveal its input, then restored; no user
conversation was toggled. A separate owned test draft was cleared once through
the existing guarded key action after exact full-text comparison, preserving
its process and restoring verified empty input.

## Validation and live-test boundary

The 0.9.5 source passes 519 Python tests and scoped Ruff checks. The unchanged browser contract suite passed
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
`~/.codex/tmp/fleet-mcp/codex-fixture-manifest-093-20261116.json` on the operator Mac.
Continuation, reload, interrupt and approval E2E results belong to the cc-proxy
integration report; readiness and preserved identities alone do not prove them.

## Runtime UUID correction, 2026-11-17

An in-process Claude resume can leave the launch UUID in argv while the existing
per-process publisher reports the current conversation UUID. Six running sessions
were affected: five returned metadata-only histories and one returned an older,
nonempty conversation. The probe now prefers the publisher UUID only after its
PID, process birth, PID domain and tmux window/pane match the observed process.
Waiting and shell activity do not invalidate that identity. Conflicting profiles
are rejected; fallback discovery retains its existing identity checks.

Seven regression cases failed against the previous reader. The corrected source
then read all 21 running Claude sessions across the three VMs with nonempty
histories, first through a read-only probe and then through their deployed root
HQ routes. All 21 original PID/birth pairs survived. No session received input or
signals for this validation. The existing Linux reader units used verified
`KillMode=process`; the public MCP reader now uses the same setting and release.
Authenticated initialization and eight-tool listing returned 200; anonymous
initialization returned 401. No new service or listener was introduced.

Private evidence lives in the operator's `~/.codex/tmp/fleet-mcp/`:
`vm-inventory-20261117.json`, `vm-readiness-after-094-20261117.json`,
`runtime-uuid-live-readonly-validation-20261117.json` and each
`codex-deploy-094-<node>.json`. UUID differences against the initial inventory
represent the six corrected runtime identities, not replaced worker processes.

## Live-only discovery, 2026-11-17

MCP previously downloaded every retained peer session before filtering live rows.
On the large domain owner, that transferred 614,989 bytes for 446 rows when only
13 were live (19,603 bytes). `include_ended=false` now propagates through HQ peer
reads and returns only live rows. Owner lifecycle observation still sees the full
census, full history remains the HTTP default, and full/live cache entries are
separate. A receiving HQ and MCP still filter older peers that ignore the query.
Nine new regression cases cover query propagation, history persistence, cache
separation and older-peer compatibility.

The deployed root returned 68,358 bytes for 44 live rows versus 2,499,831 bytes
for 1,652 full-history rows (36.6 times less data). The live read took 1.996 seconds,
then 0.009 seconds from cache. Private authenticated MCP discovery took 0.048
seconds versus a prior 34.42-second sample. These are measured samples, not a
fixed latency guarantee; the network and existing cache make timings variable.
All configured peer HQs were healthy; the empty local tmux sources on Ftower and
Sahalat still report their preexisting missing sockets.
The configured T3 ACP client read the canonical public MCP in 7.406 seconds;
two complete automatic inventory passes took 7.697 and 7.896 seconds with 44
successful import/deduplication checks each, versus earlier passes near 48 seconds.
The existing fixture UUID, history, assistant marker and run state stayed intact.
A generic urllib benchmark received Cloudflare 403 from both hosts; it is not
counted as successful public verification. No authentication or WAF policy changed.

The rollout preserved current process births and panes without worker input.
Twenty of the original 21 Claude processes still matched afterwards. The old
reply-radar routine had ended before this rollout and its existing scheduler
created a different process/pane; that replacement is recorded separately and
also survived activation. Existing lifecycle-management configuration was kept.
The domain HQ still owns its task/routine namespace; the ordinary interactive
sessions are not assigned to that lifecycle owner by T3 or fleet discovery.

One Codex thread, `01a1e46a-e592-70a2-98cf-dedf2a2df9ad`, remained loaded immediately
after its owner upgrade but later became unloaded during root verification.
The healthy owner returned 409 `Codex thread is not loaded`; its history remains
unreachable and its Codex process births are unchanged. The cause is unknown.
No load, resume, input or restart was attempted on that thread.

Private rollout evidence is `codex-deploy-095-<node>.json`,
`codex-verify-095-ftower.json`, `mcp-deploy-095-ftower.json` and
`vm-processes-after-095-20261117.json` in the operator's existing artifact directory.
The root also stores `discovery-latency-095-before.json` and
`discovery-latency-095-after.json` under its existing private state directory.

## Explicit archived detail (0.9.6 source update)

A T3 selection outlived a finished Codex subagent's loaded lifetime. The healthy
owner still exposed its exact persisted turns/items, but HQ rejected the detail
before reading history. Explicit detail now requires both absence from the loaded
catalog and `thread/read` confirmation of the exact UUID as `notLoaded`, then
reads persisted history with the same bounded reader. It returns an ended,
read-only archive view with no controls. Catalog lifecycle summaries still keep
missing threads unreachable; the archive view neither resumes nor republishes
them as live. Owner failures remain errors and actions still require loaded state.

The regression failed before implementation. Focused protocol/fleet tests cover
native message identity, stale pending controls, no resume/write RPCs on archived
reads, rejection of every supported action with the archive's current revision,
wrong owner identity, transport/read failures and unchanged live discovery. Live
activation evidence must be recorded separately; source tests do not prove a
deployment or browser integration.
