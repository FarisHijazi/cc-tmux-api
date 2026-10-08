# Federated HQ and lifecycle documentation

Date: 2026-10-08. Documentation scope: `README.md`, `AGENTS.md`,
`docs/mobile-control-research.md`, and this log. No deployment or commit is part
of this task. The parent is still changing and deploying the implementation.

## Design plan

Run the same HQ application on each participating machine and replace the old HQ
deployment. Configured HQ peers aggregate downstream sessions and forward reads
and guarded writes through a cascade to the owning node. Embedding must retain
one process and one worker-lifecycle owner.

Keep session lifecycle (`alive`, `ended`, `unreachable`) distinct from activity,
outcome emoji and question priority. A successful census can confirm an end;
failure to reach a host cannot. Preserve enough history to explain disappeared
sessions without retaining actionable stale controls.

The requested runtime has no app pairing token. HQ serves HTTP on explicit
loopback/Tailscale addresses; ngrok or another external tunnel provides public
HTTPS. Tunnel selection/public URL are pending with the parent. No Serve or
Funnel is planned. Optional authentication code is not a pairing requirement.

## Documentation plan

1. Inspect the evolving source and existing tests before publishing contracts.
2. Make [README.md](../../README.md) the only detailed configuration/API reference.
3. Update [AGENTS.md](../../AGENTS.md) with ownership boundaries and point
   [research](../mobile-control-research.md) to current contracts while preserving
   the original proposals as historical evidence.
4. Recheck source changes and record actual checks at handoff. Deployment remains
   pending unless the parent supplies verified deployment evidence.

## Verified implementation and ownership

Inspected the CLI, HTTP server/security middleware, fleet coordinator/probe,
lifecycle store and UI before writing the current README contract. Peer reads
and guarded writes route back to the owner; lifecycle history is applied to
direct local/SSH observations before peer aggregation. Worker/routine ownership
remains local. The CLI exposes `--app main:app` and multiple explicit listener
addresses. Detailed fields, limits and configuration examples are intentionally
centralized in [README.md](../../README.md#mobile-api).

## Validation at handoff

Before the monitor follow-up below, `.venv/bin/pytest -q` passed **173 tests** in 0.95 seconds, with one upstream
Starlette/httpx deprecation warning. A subsequent collection check found the
same 173 tests, including:

- [Federation tests](../../tests/federation_test.py): multi-node owner routing,
  cycle/depth rejection, duplicate origins, outages, guarded forwarding and
  no automatic retry after uncertain delivery. Nodes and HTTP are mocked; this
  is not a deployed multi-machine test.
- [History tests](../../tests/session_history_test.py): persistent summary
  transitions, missing/unavailable census handling, sticky ends, independent
  hosts, retention, reappearance and no transcript/control persistence.
- [Server tests](../../tests/server_test.py): cascade context forwarding and
  tokenless access through an explicitly allowed private host, plus existing
  optional-auth and action contracts. Route tests skip the application lifespan;
  the later monitor regression isolates it with a mocked fleet.

Source hashes for the inspected CLI/server/fleet/probe/history/security/UI files
were unchanged through that initial contract check. Bind-address
and embedding behavior were source-inspected, not exercised by deploying a
listener. No real tmux actions, browser operations or external push tests were
performed in this documentation task.

The four assigned documents passed local link/anchor and JSON-example checks,
trailing-whitespace checks, and `git diff --check`. The validation helper lives
outside the repository at `/private/tmp/validate-hq-docs.mjs`. Earlier native UI
reference and app/browser results are separate evidence, linked from the research
and [browser index](../../tests/browser/README.md); they do not certify federation
deployment or physical iPhone behavior.

### Monitor follow-up

Verified the parent's updated `server.py` lifespan: fleet collection always runs,
with five seconds of sleep after each iteration, while `push.observe` is called
only when push is available. Lifecycle history therefore no longer depends on
VAPID configuration or a browser making requests. README owns the timing and
test-isolation contract; AGENTS and research link or summarize the ownership.

`.venv/bin/pytest -q tests/server_test.py` passed **19 tests** in 0.38 seconds,
with the same upstream warning. This includes
`test_lifecycle_monitor_runs_without_push`, which enters the isolated lifespan,
verifies a mocked fleet snapshot occurs, and fails if unavailable push is invoked.
This is a focused follow-up, not a new full-suite or deployed-runtime result.

## Imported baseline and embedding follow-up

The parent imported `routines.py` and `workers.py` from remote commit `7b4f369`,
preserving the 0.3.1 finished-routine rescheduling fix and 0.3.2 worker `; exit`
behavior. This pass verified both files have no diff against that commit and
inspected the relevant code. README now
describes closing worker sessions and clearing finished routine runs, correcting
the older surviving-pane assumption. These imports were the parent's code work.

The CLI now loads config, imports the `--app` wrapper module, then imports
`server`. Wrapper defaults therefore precede worker/routine import-time settings.
The [CLI regression](../../tests/cli_test.py) uses an isolated subprocess wrapper
fixture to verify prefix/routine defaults and the app spec without opening a
listener; workdir ordering follows the same inspected import path.

The parent reported a **174-test full-suite pass before the CLI change**. This
pass independently ran `.venv/bin/pytest -q tests/cli_test.py tests/server_test.py`:
**20 passed** in 0.73 seconds, with the same upstream warning. This focused result
does not claim a post-CLI full-suite run or deployed embedded-app validation.

## Transcript and history follow-up

Inspected exact-route UUID recovery and live-detail `transcript_status` handling
in `fleet_probe.py`, the UI's embedded terminal fallback, and history's
`activity=null`/preserved `last_activity` behavior. README owns the field meanings.
Recovery requires a unique bounded route with exact runtime mapping and nearest
Claude ancestry; regressions reject nested-child attribution and reused PIDs.

The probe owner reported a read-only live check of an existing Mac Ansible session:
the exact UUID was recovered with **19 real messages**. This is owner-reported
probe evidence, not a browser/deployment test run by this documentation pass.
No UUID or transcript content from that real session is copied into these docs.

This pass ran the routine, fleet and history test modules together with
`.venv/bin/pytest -q`: **101 passed** in 0.13 seconds. The parent also
reported the isolated routine regression suite's **8 passing cases**; those cases
are included in this combined run. The routine fixture uses temporary markdown
and persisted scheduling state with all worker operations stubbed, preserving
upstream rescheduling behavior without acting on real workers. These checks do
not establish a new full-suite pass or deployed readiness.

## Earlier backend validation reported by parent

After the transcript/history follow-up, the parent reported **201 backend tests
passed in 1.16 seconds**, Ruff passing for `src` and `tests` excluding
`vendor_amux.py`, and `git diff --check` passing. These parent-verified results
precede the bind changes and final validation below.

## Per-listener ports follow-up

Inspected the server's bind parser and socket setup: each endpoint can override
the default port, including bracketed IPv6; one server process receives all
listeners. [README configuration](../../README.md#configuration-env-vars) owns
the syntax and a generic mixed-port example.

The parent's target VM plan preserves HQ loopback port 8377 and the existing
tailscaled private listener unchanged, while HQ also binds its Tailscale address
on port 8389. The planned root peer URL is `http://100.120.250.112:8389`.
This records the requested runtime routing, not a completed cutover.

The parent reports nine added bind-parser cases and **29 targeted tests passing**.
This documentation pass independently reran
`.venv/bin/pytest -q tests/cli_test.py tests/server_test.py`: **29 passed in 0.70
seconds**, with the same upstream deprecation warning.
The earlier 201-test full-suite result predates this addition.

## Final verification and staged build

The parent reports **210 backend tests passed in 1.06 seconds** after all backend
changes, with Ruff passing for `src` and `tests` excluding `vendor_amux.py`.
The real dual-port fixture at `/private/tmp/hq-bind-smoke.py` returned HTTP 200
on ports **64240 and 64241**, both served by **PID 59507**. The exact child process
was then stopped cleanly. This verifies the fixture's shared-process listeners,
not activation of the target HQ deployment.

The frontend agent reports full checks passing at **390px and 1440px** after
federation leaf-count and label normalization changes. These frontend and backend
results are owner-reported; this final documentation pass did not rerun tests.

The wheel was built and is being staged, with SHA-256
`d4d715f45ea715148104e860b971ff7f5d6e5b21c3f91029c0b47faa1dca8f61`.
It is **not activated** and predates the fixes below. Activation is paused pending
a newly built wheel. Verified deployment notes will follow activation. No commit
was made here.

## Final-review monitor fixes

Both fixes are present in the inspected source: the monitor calls
`push.observe(..., deliver=False)` to persist events, while a separate async task
calls `PushService.deliver_pending` against its existing owned outbox. Fleet also
locks local/SSH collection through history commit, preventing out-of-order census
commits from falsely marking sessions ended. Peer HTTP requests stay outside
that lock. README owns the current monitor contract.

The parent reports **90 server/push tests passing**, including a blocked-provider
regression that verifies a second census proceeds. The prior 210-test result
predates these final-review fixes; no new full-suite or fleet-lock regression
result is claimed here. This documentation follow-up only checks documentation.

## Pending parent integration

The old-HQ replacement and external tunnel deployment remain pending. The parent
reports automatic approval review blocked the public tunnel invocation; specific
user confirmation of exposing the control API publicly is pending. No tunnel was
started by this documentation pass and no live HTTPS endpoint is claimed. No public
URL is recorded because none was verified for this handoff. The current optional
`CC_TMUX_PUBLIC_URL` setting still requires a token; the parent must validate
the final tunnel's host/scheme/origin handling while preserving the requested
no-pairing runtime. This pass added no application certificate feature or tunnel
configuration. No source files, runtime config, GUI state or commits were changed.
