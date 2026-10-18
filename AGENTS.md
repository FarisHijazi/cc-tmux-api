# Agent instructions

This checkout implements the same HQ application across machines, with cascading
peer reads/writes and lifecycle-aware control of existing Claude Code tmux sessions.
[README.md](README.md) owns detailed runtime configuration and API contracts;
[the research](docs/mobile-control-research.md) preserves design rationale and
delivery gates. [The federation/lifecycle devlog](docs/devlog/claude_20261008-federated-hq-lifecycle.md)
tracks the current plan and verification; earlier devlogs remain history, not
proof of current deployment. These are deliberately plain links, not `@` includes.

The requested runtime has no app pairing token and no Serve/Funnel. HQ serves
HTTP; the existing homelab Cloudflare Tunnel and Traefik own public HTTPS.
The persistent ftower aggregator is published at the requested hostname; see
[the ftower deployment log](docs/devlog/claude_20261008-ftower-deployment.md) for
runtime ownership and verification. Application certificate handling remains
outside this design. Public origins can be configured without an app token.
The public hostname now uses Traefik's `require-authentik@file` middleware on
every path, verified against the live route on 2026-10-18. Preserve that gate;
private peers remain token-free. The deployment owner's
`provision/traefik/config/cchq.yml` selects the middleware, and
`compose/authentik/CLAUDE.md` owns the shared authentication service. The
[auth/discovery follow-up](docs/devlog/claude_20261008-auth-and-discovery.md)
records the earlier Basic Auth deployment, not the current gate. See
[the native transport devlog](docs/devlog/claude_20261018-native-remote-control.md)
for current verification. Credentials remain outside this repository.
Sahalat runs 0.4.4: shell-labelled panes require a unique ancestry-safe Claude
with its own foreground process marker, never just a foreground parent shell.
The old Sahalat session lacks current UUID evidence; preserve its terminal
access without guessing a conversation identity from stale events or cwd.
The public deployment passed actual session-detail and mobile/desktop browser
checks plus a native-verified nine-action remote question batch; physical iPhone
push delivery remains unverified. Preserve that distinction in future reports.
Replace the old HQ deployment using the same application; use the
CLI's embedding path for domain routes in one process, including listeners with
different ports (see README configuration). Do not invent a permanent
second mobile service or duplicate a registry's worker lifecycle owner. The parent
has verified 0.4.2 on all VM owners and persistent Mac HQ with matching runtime
package bytes; 0.4.1 review-card visual verification passed. Real-browser lifecycle
verification passed, including removed ended-session input and action rejection;
earlier two-relay cascade checks and the final fresh nine-action remote menu batch
passed. The devlog records artifact evidence and remaining device-test limits.
Source changes alone do not establish readiness.
Conversation messages must exclude system-injected user-role records using their
origin metadata, not text matching. Human quotations of task notices remain human
messages. See [the notice-filter incident](docs/devlog/claude_20261008-system-notice-filter.md).
On the Mac, preserve the existing SSH forward and disabled legacy HQ service.
The persistent generic HQ has replaced the preview, with worker management disabled.
The Mac now serves local sessions only; ftower connects directly to all four peers
and also discovers its own local sessions. Domain wrappers remain on the VMs.
The ftower deployment log records aggregation ownership; its authentication
claims are superseded by the verified Authentik route described here.

Existing ownership boundaries:
- `native_remote.py` owns optional native Remote Control v2 transport state,
  authenticated per-session worker streams and structured pending controls.
  The existing fleet remains the action/receipt owner; SessionHistory remains
  the lifecycle-summary owner. Native disconnect is never evidence of end.
  Keep human input provenance, single stream leases, epochs and delivery ACKs
  aligned with the tested CLI. Native routes require their separate launch or
  worker credential even when browser pairing is disabled. Read
  `docs/devlog/claude_20261018-native-remote-control.md` on demand (plain path).
  `CC_TMUX_NATIVE_ONLY=1` excludes tmux, SSH and peer discovery for isolated tests.
  Mac and ftower native ingress/aggregation now run 0.5.1; production
  federation was verified with a new interactive session and real browser input.
  `computer_use_approval` uses native `request_user_dialog` and explicit app/flag
  consent, not generic tool allow/deny. Advertise only supported dialog kinds;
  keep Pro/Max eligibility, OS permissions and prior session grants intact. Read
  `docs/devlog/claude_20261018-native-computer-use.md` on demand (plain path).
- `src/cc_tmux_api/workers.py` owns the unattended worker namespace and its revival
  and reaping. Preserve the imported routine rescheduling and worker `; exit`
  behavior; see README for the contract. Ordinary interactive sessions must not
  become reaper-owned workers.
- `src/cc_tmux_api/cli.py` must load config, then import an embedded wrapper before
  `server`, so wrapper defaults precede worker/routine import-time settings.
- `tcc` remains the launch/resume owner for interactive sessions.
- The sibling `cc-notify` project owns hook event production and status vocabulary;
  fleet reads its metadata/presentation policy. Do not copy suffix parsing into
  the UI or add a hook/SSH event listener for push.
- `src/cc_tmux_api/fleet.py` owns configured local/SSH/peer aggregation, cascade
  routing and cycle checks, process-local action locks/receipts and snapshot caches.
  `fleet_probe.py` owns identity/revision checks and input to the exact existing
  pane, including bounded exact-route transcript identity recovery. Preserve
  nearest-Claude ancestry checks; do not infer an outer session's identity from
  a child's UUID or a directory's newest transcript. Forward guarded actions to
  the owning peer; never introduce another
  terminal write path or automatically retry an uncertain delivery.
- `src/cc_tmux_api/session_history.py` is the sole persistent lifecycle-summary
  owner for directly probed local/SSH hosts. Apply it before merging peer rows;
  the peer owns those rows' history. An unreachable host cannot prove an end.
  Keep lifecycle separate from activity/outcome emoji and live question priority;
  no controls for ended/unreachable records. The server lifespan always runs the
  shared fleet monitor, including without push configuration or a browser. Push
  consumes its snapshots only when available; delivery runs separately against
  the existing outbox. Preserve the fleet's collect-through-history-commit lock;
  do not add another census poller/history writer.
- `src/cc_tmux_api/notifications.py` owns persistent VAPID/subscriptions, mute
  preferences, event deduplication, and delivery retries. `security.py` owns
  optional pairing and host/same-origin API enforcement, including legacy worker routes.
  Worker sends must use the fleet's guarded path and `worker.control` identity/
  revision; never restore bare session-name sends. The migration contract is in
  [README.md](README.md#migrating-worker-sends-to-040).
- `src/cc_tmux_api/static/` owns the UI and shell-only service worker. Never cache
  API/transcript responses or queue/retry terminal actions. Preserve drafts on
  failed sends and gate empty states on a completed initial request.

Keep tests isolated from real sessions and the default registry. Startup revives
workers unless `CC_TMUX_MANAGE_WORKERS=0`; that flag does not disable worker API
mutations. Use temporary registry/state directories for preview servers, and
remember the lifespan always starts fleet monitoring even without VAPID or push.
Run `uv run pytest -q`;
frontend and browser commands are in README and [tests/browser/README.md](tests/browser/README.md).
Backend tests mock tmux/SSH/push; tests that exercise the lifespan must also mock
fleet collection (the no-push monitor regression does this). Do not claim physical
iPhone, native Claude UI, or live action validation from mocked tests or desktop
screenshots. Coordinate any live input with the owner of the reserved test pane.
