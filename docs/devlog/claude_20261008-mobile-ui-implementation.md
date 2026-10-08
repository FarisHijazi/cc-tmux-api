# Mobile UI implementation — 2026-10-08

The mobile upgrade is implemented as version 0.4.0 in this worktree, verified in
[pyproject.toml](../../pyproject.toml) and [uv.lock](../../uv.lock) after the parent's
`uv` version bump. This log records the source
inspection and available validation evidence; it does not certify deployment or
physical-device delivery. Runtime setup and API contracts have one home in
[README.md](../../README.md). Design history and delivery gates remain in
[mobile-control-research.md](../mobile-control-research.md). Links are deliberately
plain links, not `@` includes.

## Implemented scope

- [server.py](../../src/cc_tmux_api/server.py),
  [security.py](../../src/cc_tmux_api/security.py), and
  [cli.py](../../src/cc_tmux_api/cli.py): existing worker API plus fleet/push routes,
  shared-secret pairing, signed cookies, same-origin checks, and private JSON
  configuration loaded before import-time settings.
  Worker send compatibility changes are documented in
  [the migration guide](../../README.md#migrating-worker-sends-to-040): list results
  expose nullable `worker.control`, and sends require session identity, revision,
  and request ID in addition to name/text. Bare-name sends return 400; the route
  verifies local worker ownership and delegates input to the guarded fleet path.
- [fleet.py](../../src/cc_tmux_api/fleet.py) and
  [fleet_probe.py](../../src/cc_tmux_api/fleet_probe.py): local/SSH discovery,
  generation-bound pane identity, transcript lookup, menu recognition, guarded
  input, and process-local duplicate-action receipts. Adoption does not change
  launcher/worker ownership. No additional event listener or hook was added.
- [notifications.py](../../src/cc_tmux_api/notifications.py): persistent VAPID,
  validated browser subscriptions, session baseline/deduplication, mute state,
  retry outbox, and explicit enrollment-test endpoint. Normalized fleet metadata
  supplies eligibility; the existing API monitor drives observation/retry. Push
  bodies prefer the supplied canonical `status_label`, with whitespace collapsed
  and a 160-character limit; blank/non-string labels use the existing status
  fallback. No token vocabulary is duplicated.
- [static/app.js](../../src/cc_tmux_api/static/app.js) and
  [static/sw.js](../../src/cc_tmux_api/static/sw.js): responsive Sessions/Workers/
  Routines/Settings views, grouping/search, conversation/terminal fallback, menu
  navigation, local drafts, pairing, push enrollment and deep links. Offline
  caching is limited to shell assets; actions are not queued or automatically retried.

## Evidence at documentation handoff

The parent initially reported 95 backend tests passed. At this documentation
handoff, **`.venv/bin/pytest -q` passed 103 tests**, with one upstream
Starlette/httpx deprecation warning. `uv run pytest -q` hit a macOS
system-configuration runtime panic before starting pytest; direct execution used
the existing project environment without changing dependencies. The isolated push
suite previously passed 50 tests with mocked delivery. Relevant suites are
[server_test.py](../../tests/server_test.py),
[fleet_test.py](../../tests/fleet_test.py), and
[notifications_test.py](../../tests/notifications_test.py). Their mocks are not
proof of real tmux input or external push delivery.

`node --check` passed for `static/app.js` and `static/sw.js`. All local Markdown
links in the four assigned documents resolved, and the tracked README diff passed
`git diff --check`; the other three documentation paths are currently untracked.

After the canonical-label change, the push suite passed 57 tests and Ruff passed
for the notification module/tests. Added cases cover failure/blocked labels,
whitespace normalization, length limits, and absent/invalid-label fallback.

[frontend_test.mjs](../../tests/frontend_test.mjs) is the mocked browser contract
suite. The separate [browser evidence index](../../tests/browser/README.md) owns
read-only preview audits and their artifacts. Consult that index for exact
coverage; screenshots/emulation do not establish physical iOS behavior. The
earlier research's manual toy experiments do not validate this implementation's
mobile action path.

The documentation pass inspected actual source and preserved worker/routine
documentation while replacing obsolete unauthenticated/research-only claims.
Only README, AGENTS, the research status, and this devlog are owned by this pass.
No commit or deployment was performed by the documentation pass.

## Remaining evidence boundaries

- Native Claude UI and live mobile text/menu actions, including stale-screen and
  interrupted-delivery cases, are not claimed complete.
- Physical iPhone install/enrollment, keyboard/composer behavior, locked-screen
  push, tap-to-session, and network-change recovery remain unverified here.
- Transcript mapping is conditional; terminal fallback is implemented. Session
  `updated_at` now uses `fleet_probe.activity_time`: the latest known positive
  event/tab timestamp or mapped transcript mtime, otherwise null. Polling does
  not manufacture activity. Host and top-level timestamps remain observation times.
- Per-process action receipts do not survive restart or coordinate multiple
  server processes. Local input can race the final terminal write. Push delivery
  can repeat across a crash after provider acceptance.

The parent will append final integrated test, browser, device, and deployment
evidence here, distinguishing each class of validation.

## Planned runtime separation

After inspecting the timestamp correction, the focused command
`.venv/bin/pytest -q tests/fleet_test.py -k 'activity_time or polling'` passed
both selected tests. This supplements the earlier handoff run, not a new full-suite run.

The private Tailscale runtime is planned as a separate process on port 8389 with
`CC_TMUX_MANAGE_WORKERS=0`, using the same upgraded cc-tmux-api source/UI. The
original HQ service retains responsibility for its background worker lifecycle.
This is not a deployment-completion claim. Configuration and the flag's limits
are documented in [README.md](../../README.md#mobile-configuration); the parent
will append final tests and native/remote-menu results separately.

## Push review and isolated Chrome smoke

The slow-provider regression also reproduced settings/mute blocking while the
outbox held its SQLite transaction during network sends. Delivery now uses a
separate nonblocking lock with short fetch/check/update transactions, releasing
the database/state lock before provider I/O. Each queued item is rechecked for
expiry/removal/mute before sending; cancelled rows cannot be resurrected by a
failed in-flight response. In-flight pushes cannot be recalled. The notification
suite passed **61 tests**, including blocked-provider success/failure cases that
verify responsive preferences, mute, subscription updates, and concurrent observe;
Ruff passed. This supersedes the notification test count below.

Reproduced P2 with a failing regression: permanent event-ID deduplication suppressed
an identical menu after a confirmed working-to-waiting transition. The notification
consumer now accepts re-entry into waiting from a known non-waiting state as a
new occurrence, even when the producer ID is reused. Repeated polls, restarts
while waiting, and empty/unknown/offline/stale observations remain suppressed.
The notification suite passed **59 tests** and Ruff passed after the fix. No
producer/parser changes or token mappings were added by this pass.

The authorized real Chrome push smoke used a fresh Playwright context at
`http://127.0.0.1:8389`, with notification permission granted only in that context.
It authenticated successfully without logging the key, confirmed secure-context
and PushManager support, and activated the real service worker. Actual
`PushManager.subscribe` failed with **`AbortError: Registration failed - permission
denied`**. No server subscription was created and `/api/push/test` was not called;
delivery therefore remains unverified. The isolated context's workers were
unregistered and the context/browser closed. Existing profiles/preferences and
subscriptions were not modified.

Script: `/private/tmp/cc-mobile-push-smoke/smoke.mjs`. Exact result:
`/private/tmp/cc-mobile-push-smoke/report-2026-10-08T17-07-32.463Z.json`.
These are private local artifacts, deliberately plain paths, not `@` includes.
The initial sandboxed Chrome launch failed; the approved launch outside the
sandbox reached the subscription failure above. This is neither external push
delivery proof nor physical iPhone evidence. The smoke has finished and no longer
requires the preview origin to remain unchanged.
