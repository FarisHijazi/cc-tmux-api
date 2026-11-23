# HQ development

Read [AGENTS.md](AGENTS.md) for ownership, runtime boundaries and validation.
Native Remote Control protocol research and live evidence are recorded in
[the native transport devlog](docs/devlog/claude_20261018-native-remote-control.md).
These are deliberately plain links, not automatic includes.
For runtime launch/configuration, use [README.md](README.md#native-remote-control)
and preserve the existing production service and HTTPS owners.
`codex_remote.py` owns the existing Codex app-server connection. `fleet.py` routes
its `@codex` source through the same locks, receipts and peer federation; do not
add a second tmux writer or infer Codex identity from a working directory.
`SessionHistory` retains unloaded or disconnected Codex threads as unreachable,
not ended. Explicit detail may read persisted history for an exact canonical UUID
absent from the loaded list only when its owner confirms `notLoaded`; that archive
view is `read_only`, `lifecycle=ended`, `alive=false`, `online=true`, with no controls.
Never resume an archived read, turn a transport/read error into empty history,
or allow that detail fallback into action validation or live discovery.
The Rust deployment needs equivalent integration or an explicit
same-owner cutover; changing the Python fleet alone does not update it.
Tests disable automatic Codex discovery in `tests/conftest.py`; protocol tests
must supply isolated sockets or fakes before explicitly enabling it.
Codex runtime settings are in `README.md#existing-codex-sessions`; rollout and
version compatibility evidence is in `docs/devlog/claude_20261116-codex-fleet.md`.
Codex denial uses the request's offered negative decision: `decline`, or `cancel`
when decline is unavailable. Never widen approval or send an unoffered decision.
The UI shares native control rendering but respects per-action capabilities;
Claude Remote Control links and model settings remain capability-gated.
Native provider identity is explicit in both summaries and selected detail:
`provider=claude|codex` is separate from transport. Fleet consumers must use
that owner-supplied identity, never infer an engine from titles or connection names.
All six HQ readers run 0.9.7; the public MCP adapter remains on the unchanged
0.9.5 wheel, with release evidence recorded in the Codex devlog;
Dev now uses the same Python owner after explicit Rust API/state parity.
Native launch keys are private runtime files; never place their values in docs.
Native registry routes and SSE lock access run off the HTTP event loop and outside
FastAPI's shared request pool. Keep each mutation, lock and durable save together;
never acknowledge a worker event before its database transaction commits.
`NativeRemote` is the sole native persistence owner: `native-remote.sqlite3` stores
native payloads and per-session observations/receipts in the same transaction.
Ordinary Mod batches update only observations; UUID binding/rotation updates both
atomically. Use explicit changed-record/deletion commits and preserve FK ownership.
The first startup atomically imports legacy JSON; subsequent startups never fall
back to it. Preserve private database/journal permissions, FULL synchronous commits,
live session object identities and failure rollback. The old JSON is an immutable
migration snapshot, not a second writer or a safe current-state rollback source.
README owns migration, backup and downgrade requirements.
Computer-use protocol support, deployed consent tests and account/OS limits are recorded
in [the computer-use devlog](docs/devlog/claude_20261018-native-computer-use.md).
The native-first UI and the Remote Control protocol as claude.ai uses it (captured
requests, parity table) are in `docs/devlog/claude_20261023-native-ui-and-rc-protocol.md`.
The claude.ai upstream mirror (design, protocol mapping, test coverage, what is not
verified live) is in `docs/devlog/claude_20261023-upstream-mirror.md`.
The `cc-tmux-mcp` MCP server (`src/cc_tmux_api/mcp_server.py`, `[mcp]` extra, mcp SDK 2.x `MCPServer`) is a thin
HTTP client of an HQ; keep fleet logic in the HQ, never in the MCP. Deployment: `docs/devlog/claude_20261111-mcp-and-hub-fix.md`.
`cc-rc-watchdog` (`src/cc_tmux_api/rc_watchdog.py`) detects from session files + HQ health, never from screen text;
the screen only gates typing: idle, empty prompt, no menu, no recent client input. It never types twice into a process
whose repair failed (`~/.local/state/cc-rc-watchdog/gave-up.json`): retrying flooded a live conversation with 150
`/remote-control`s. Same devlog; double `/rc`, `--now`/`--close-menus`/`--renew-names` and naming evidence:
`docs/devlog/claude_20261123-rc-force.md`.
Public `/mcp` on cc-proxy.fhijazi.com (and legacy cchq.fhijazi.com): OIDC JWT + email allowlist,
or static token; never route `/api` around SSO. Root authorization-server discovery redirects to the
fixed Authentik issuer's OIDC metadata for Claude.ai compatibility.
The cc-proxy MCP integration reuses this adapter and fleet; `list_nodes` exposes source health, and
question responses require the revision returned by `read_session`. Deployment and validation:
`docs/devlog/claude_20261115-fleet-mcp.md` (plain path).
The fleet foreground discovery backports, guarded working-input control and
tmux-preserving service overrides are recorded in `docs/devlog/claude_20261115-fleet-coverage.md`.
`GET /api/sessions?include_ended=false` propagates live-only reads through peers;
the default retains full history. Filter only after the owner's lifecycle census,
and keep full/live snapshot and reachability caches separate. MCP discovery uses
this query and still filters locally for older peers that ignore it.
Transcript messages retain source IDs (row UUID, otherwise provider message ID;
text blocks suffix their original index). Keep this in the existing probe reader.
The probe reads Claude's existing per-process publisher for hookless busy status
and the current runtime UUID; require exact process birth/domain and tmux
window/pane before trusting it. An in-process resume changes that UUID while argv
retains launch intent, so the validated publisher takes precedence over argv.
Identity is independent of activity: waiting/shell publishers still identify the
conversation. Conflicting profiles are rejected; no cwd/title inference is used.
Darwin publisher birth markers use UTC and space-padded days, unlike the parsed
process inventory; keep that canonical conversion when comparing markers.
Profile discovery is shared with `rc_watchdog`; no additional poller owns status.
A verified publisher idle timestamp may supersede a strictly older work hook;
current spinner/menu detection and exact process/pane checks always retain priority.
The same NativeRemote owner now stores a bounded passive observer tail and Mod
acceptance receipts in its registry. `/api/sessions/{id}/events` uses exact fleet
routing and existing API security; `/v1/code/sessions/mod-events` uses launch auth
and must resolve exactly one explicit current Claude UUID, never infer or create
a session. The optional explicit native bridge ID binds that exact existing owner;
conversation rotation requires previous-UUID CAS and a fresh producer. Preserve
that binding across native init's cse placeholder, reject stale producer rebinding,
and save the identity atomically with its event batch. Mod events cannot enter
`_ingest`, `_queue`, transcript storage or liveness updates. Ordinary observations
and initial bridge-to-UUID binding do not change revisions. Canonical UUID rotation
must atomically advance revision/input_revision to reject actions inspected under
the old conversation, including rotations reported by native init itself.
The same transition clears conversation caches (messages, streaming, pending
cards/control bookkeeping and context/task summary) before new ingestion. Preserve
native transport, command/delivery lanes and observer/deduplication continuity.
Native batch capacity preflight must apply that transition to its pending-card
preview too. Initial qualification and repeated bindings never clear history.
Native and Mod acceptance wake readers only after a
durable save; preserve the session object's identity when rolling back a failure.
Producer UUID/sequence high-watermarks prevent an evicted receipt from becoming a
second observation. Cursor expiry/reset and oversized native events report gaps.
Long-poll requests use the server's bounded observer executor, separate from
native ingest/control offloads. Capacity is 32 with explicit 503 admission;
cancelled requests retain their slot until the underlying wait actually finishes.
README's observation contract owns the bounds and wire schema. This source
feature does not establish deployment or live interactive acceptance.
