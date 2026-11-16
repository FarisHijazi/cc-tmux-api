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

All existing HQ owners are retained. The dev owner currently uses Rust; its
embedded probe cannot load the Python Codex adapter. A same-owner Python cutover
requires fresh API/state parity before activation. The preflight found no native
worker streams in its state, matching SQLite schemas and an intact VAPID key.

Every rollout must retain private config, native state, session history and
domain wrappers, and compare existing tmux server/pane plus Claude/Codex process
identities before and after. User services must use `KillMode=process` before
restart because an existing tmux server can belong to the HQ cgroup. Rollback
restores only the previous executable while retaining that kill mode. No shared
network service or Docker runtime is part of this rollout.
