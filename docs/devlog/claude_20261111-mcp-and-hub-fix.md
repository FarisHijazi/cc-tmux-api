# Cross-VM sessions MCP, and the aggregator's 500

**Hub 500.** From at least 2026-11-10 16:35 EST the ftower aggregator answered every `GET /api/sessions` with 500 after
20 s. `fleet_probe.notify_library()` called `Path.is_file()` on cc-notify install paths from
`~/.claude/plugins/installed_plugins.json`; that file had been rewritten (root-owned, all 17 `installPath`s under
`/root/.claude/...`) by the `humain-agent` container, which mounts `/home/faris/.claude` as `/root/.claude` and runs
Claude as root. `stat` on `/root/...` raises `PermissionError`, which escaped the `except`. Fix: `_readable_file()` treats
any `OSError` as "not this candidate". Regression test `test_notify_library_skips_registry_paths_it_cannot_read`
fails without the fix. The container keeps rewriting the registry; that is the root cause and is not changed here.

**MCP.** `cc-tmux-mcp` (stdio) wraps `/api/sessions`, `/api/sessions/{id}` and `/action`. Tests in
`tests/mcp_server_test.py` run against an `httpx.MockTransport` HQ. Live: through the ftower aggregator from dev it
listed 22 live sessions on four hosts, including Remote Control.

**Deployment (ftower).** Release `352d5cb2…` (0.8.0 wheel, hash-locked requirements incl. the `mcp` extra) under
`~/.local/share/cc-tmux-api/releases/`. `hq.json` gained `100.103.162.14` (ftower tailnet) in `CC_TMUX_BIND` and
`CC_TMUX_ALLOWED_HOSTS` so other VMs reach it with the bearer token. Backups: `cc-tmux-hq.service.before-mcp-20261111`,
`hq.json.before-mcp-20261111`. Rollback: restore both, `daemon-reload`, restart `cc-tmux-hq`.

**Remote Control watchdog.** Live on Claude Code 2.1.322: `/remote-control` while connected opens a dialog
(Disconnect this session / Show QR code / Continue, default Continue), which is why typing it "does nothing".
Disconnect clears `bridgeSessionId` in `sessions/<pid>.json`; the next `/remote-control` writes a new id. On dev the
first dry run found 5 sessions without Remote Control and one bridged straight to Anthropic. Both repair paths were
run live on a throwaway session (the stale case simulated by pointing its session file at an unknown bridge id); the
HQ then showed the new bridge alive, online, upstream `connected`. Tests: `tests/rc_watchdog_test.py` (fake CLI
replaying the captured screens).
