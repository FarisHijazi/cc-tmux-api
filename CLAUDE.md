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
The Mac HQ reader runs 0.9.6; the other five HQ owners and public MCP adapter remain
on the 0.9.5 wheel, with both releases recorded in the Codex devlog;
Dev now uses the same Python owner after explicit Rust API/state parity.
Native launch keys are private runtime files; never place their values in docs.
Native registry routes and SSE lock access run off the HTTP event loop and outside
FastAPI's shared request pool. Keep each mutation, lock and durable save together;
never acknowledge a worker event before its atomic registry replacement finishes.
Computer-use protocol support, deployed consent tests and account/OS limits are recorded
in [the computer-use devlog](docs/devlog/claude_20261018-native-computer-use.md).
The native-first UI and the Remote Control protocol as claude.ai uses it (captured
requests, parity table) are in `docs/devlog/claude_20261023-native-ui-and-rc-protocol.md`.
The claude.ai upstream mirror (design, protocol mapping, test coverage, what is not
verified live) is in `docs/devlog/claude_20261023-upstream-mirror.md`.
The `cc-tmux-mcp` MCP server (`src/cc_tmux_api/mcp_server.py`, `[mcp]` extra, mcp SDK 2.x `MCPServer`) is a thin
HTTP client of an HQ; keep fleet logic in the HQ, never in the MCP. Deployment: `docs/devlog/claude_20261111-mcp-and-hub-fix.md`.
`cc-rc-watchdog` (`src/cc_tmux_api/rc_watchdog.py`) detects from session files + HQ health, never from screen text;
it types only into idle sessions with an empty prompt, and never twice into a process whose toggle failed
(`~/.local/state/cc-rc-watchdog/gave-up.json`): retrying flooded a live conversation with 150 `/remote-control`s. Same devlog.
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
