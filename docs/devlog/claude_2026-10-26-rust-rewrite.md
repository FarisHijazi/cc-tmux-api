# Rust rewrite of cc-tmux-api (branch `rust-rewrite`)

Crate: `rust/` (binary `cc-tmux-api`, axum + tokio; deps: serde_json, regex,
rusqlite (bundled), ureq, hmac/sha2, base64, chrono, serde_yaml, include_dir,
getrandom). Not deployed anywhere. The Python package is unchanged and still
the production code.

## What is ported (same env/config contract, same files on disk)

| Python module | Rust | Notes |
|---|---|---|
| `cli.py` / `server.main` | `main.rs` | `--config` JSON → env (existing env wins), multi-address `CC_TMUX_BIND`, `bind_address` rules |
| `server.py` | `server.rs` | every route; pydantic-style 422s (status + field, not pydantic's exact text) |
| `security.py` | `security.rs` | pairing cookie (same HMAC format, so cookies survive a swap), bearer, host/origin/JSON guards, rate limit, uvicorn `FORWARDED_ALLOW_IPS` handling |
| `workers.py` | `workers.rs` | spawn/kill/list (2 s cache)/revive/reap/GC, `@hq_owner` nesting, same registry + prompt files |
| `routines.py` | `routines.rs` | frontmatter (YAML 1.1 `yes/no/on/off` honoured), schedules, toggle, `.state.json` |
| `fleet.py` | `fleet.rs` | hosts config, local/SSH probe, HTTP peers, cascade/cycle checks, receipts, locks, 3 s cache |
| `fleet_probe.py`, `vendor_amux.py` | embedded verbatim (`include_str!`) | the probe already runs as `python3 -` on every host; re-implementing it would fork the pane-inspection logic |
| `session_history.py` | `history.rs` | same SQLite file/schema/rows |
| `vendor_amux._detect_claude_status` | `amux.rs` | line-for-line port (workers' status + fleet decoration) |
| static UI | embedded with `include_dir` | single self-contained binary |

Session IDs are byte-identical to Python's (`py.rs` reproduces `json.dumps`
escaping), so browser links, receipts and history rows stay valid across a swap.

## Not ported

- Native Remote Control (`native_remote.py`, `CC_TMUX_NATIVE_TOKEN`) and the
  claude.ai mirror (`rc_mirror.py`). The Rust binary **refuses to start** when
  `CC_TMUX_NATIVE_TOKEN` is set rather than silently dropping native sessions;
  `@native` routes answer 503.
- Web Push delivery (VAPID keys, RFC 8291 encryption, outbox). `/api/push`
  reports `available: false`; mute preferences and unsubscribe still work
  against the same `push.sqlite3`; subscribe/test answer 503.
- `--app` (embedding a Python ASGI app) — exits with a usage error.

## Parity test

`parity/api_parity.py` runs both servers side by side on random ports against
one scratch tmux server (`tmux -L <socket> -f /dev/null` via a PATH wrapper),
a scratch HOME, and `parity/fake_claude.sh`, and compares status + normalized
JSON for every case: auth/security, static files, sessions/detail/actions
(real delivery into a pane, receipt replay), workers spawn/list/send/kill,
routines, oneshot, push storage, federation (Python peer and Rust peer, each
aggregated by both), startup revival and the routine scheduler.

Result: **117 passed, 0 failed, 1 known difference** (`GET /api/push`), stable
over 3 consecutive runs. One earlier run had a single timing flake in
"peer down after it was seen", not reproduced since.

```
OLD=~/Projects/cc-tmux-api/.venv/bin/cc-tmux-api python3 parity/api_parity.py
```

Unit tests: `cd rust && cargo test` (6 tests).

Harness gotchas: tmux starts pane shells as login shells, so `/etc/profile`
resets PATH and hides the fake `claude`. The harness sets `default-command
/bin/sh` on the scratch server. And the probe only accepts `send` against an
empty prompt box in the last 12 screen rows, so the fake draws one at the bottom.

## Cutover plan (needs approval, not done)

1. Build on the target machine: `cargo build --release` in `rust/` (the
   binary embeds the probe + UI, so only `python3` must exist on probed hosts,
   same as today).
2. Only instances **without** `CC_TMUX_NATIVE_TOKEN`, push (`CC_TMUX_VAPID_SUBJECT`)
   or `--app` can switch. This box's `cc-tmux-hq.service` (hq-dev, port 8389)
   sets `CC_TMUX_NATIVE_TOKEN`, so it **cannot** switch yet.
3. For an eligible instance (e.g. the Mac's dedicated worker instance on 8378,
   after checking its config): point the launchd plist / systemd `ExecStart` at
   the Rust binary with the same `--config` file, restart that one service,
   then check `GET /api/workers` and one spawn+kill from the caller.
4. Rollback: restore the previous `ExecStart`/plist line and restart. State
   (registry, routines, `session-history.sqlite3`, `push.sqlite3`) is shared and
   format-compatible both ways.
