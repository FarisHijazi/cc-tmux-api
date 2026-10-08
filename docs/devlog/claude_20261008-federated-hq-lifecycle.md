# Federated HQ and lifecycle documentation

The [ftower deployment log](claude_20261008-ftower-deployment.md) supersedes this
log's final aggregation topology and pending-public-HTTPS status. Earlier
implementation and deployment evidence below is historical.

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
The later deployment report below confirms this routing on the primary node.

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

The earlier wheel has SHA-256
`d4d715f45ea715148104e860b971ff7f5d6e5b21c3f91029c0b47faa1dca8f61`.
It predates the fixes below. The parent's corrected deployment report confirms
that this wheel was activated on the **Work-A primary** before the hold:
**PID 2621836** serves both `127.0.0.1:8377` and `100.120.250.112:8389` in one
process. Both listeners passed tokenless `/api/sessions` checks; `/hq` returned
HTTP 200, and two routines remain enabled. Existing tailscaled **PID 40278** was
left untouched. **BC, Work-B, and Sahalat are staged only.** These are actual
deployment identifiers and parent-reported checks, not illustrative examples or
checks performed by this documentation pass.

The parent is building a replacement wheel containing both review fixes below;
its activation has not been reported. This supersedes the earlier incorrect
statement that the first wheel was unactivated everywhere. No commit was made here.

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

## Corrected Mac ownership after preflight

The parent reports that the old Mac HQ plist is **disabled**, with no active
process. Mac `127.0.0.1:8377` is owned by SSH forward **PID 19721**, managed by
`work-a-hq-tunnel`, forwarding to the VM. Preserve that forward and the disabled
state. After a failed bootstrap, the original disabled plist was restored exactly.

The corrected local deployment uses persistent **`com.local.cc-tmux-hq`** at
port **8389** to replace the preview with the same generic package. Its root
configuration is `~/.config/cc-tmux-api/hq.json`, with
`CC_TMUX_MANAGE_WORKERS=0`: it observes local interactive sessions and does not
run a Mac domain wrapper or duplicate VM schedules. VM domain wrappers remain
in place. This supersedes the abandoned local-wrapper plan; it does not claim a
new successful bootstrap or endpoint check beyond the parent's report.

The abandoned local-wrapper plan's `uv sync` changed the career HQ lockfile.
The parent will restore only its own change if verified; this documentation pass
does not modify that lockfile or assert that restoration is complete.

## Verified deployment: current state

This parent-verified deployment report supersedes earlier staging and activation
status above; those entries remain historical evidence. All VM owners now run
the final wheel with SHA-256
`149da13b9c9e5d448d11a1f1538abdae6592ae2e0bf940001d1e7f74214a2133`.

| Runtime owner | PID | Verified listeners / ownership |
|---|---|---|
| Work-A primary | `2647811` | One process: `127.0.0.1:8377`, `100.120.250.112:8389` |
| BC | `2651066` | Local port `8379` |
| Work-B | `2963036` | One process: `127.0.0.1:8378`, `100.83.50.61:8378`, `192.168.0.157:8378`; root uses the LAN URL because its Tailscale address is unreachable from the Mac |
| Sahalat | `1906477` | One process: `127.0.0.1:8377`, `192.168.50.198:8377`; service enabled, `Linger=yes` |
| Mac generic HQ | `35507` | Persistent `com.local.cc-tmux-hq`: `127.0.0.1:8389`, `100.115.53.98:8389`; root revision `f462870`, no domain wrapper, `CC_TMUX_MANAGE_WORKERS=0` |

All **31 runtime package files** on the Mac were verified byte-identical to the
VM wheel. Root HTTP peer GET checks found **11 alive sessions**, **8 federated**,
with all **7 API host rows available**; the UI reports **4 leaf machines**.
These counts describe that observation, not a fixed fleet size or completion of
browser lifecycle/cascade E2E; subsequent results are recorded below.

Root state is `~/.local/state/cc-tmux-api-hq`. Local history and VAPID state were
migrated from the preview, retaining the source. No ended remote records,
subscriptions or mutes existed to migrate. The old SSH forward **PID 19721** and
disabled legacy plist remain unchanged. Preview **PID 73085** was stopped by
exact PID. These are parent-reported runtime checks; this documentation pass
performed no process, service or state changes.

## Final real-browser verification

The parent reports a real Chrome lifecycle check using only the owned local toy:
pane **%121**, Claude **PID 6697**, with exact cwd and UUID verified before
`tmux kill-session` targeted **`cc-mobile-research-toy`** alone. The session
transitioned from alive to ended while retained in the list: **11 total,
10 alive, 1 ended**. The composer and Send control were removed; an ended-session
action returned **409**, `Session has ended and is read-only`.

Evidence is `/private/tmp/hq-ended-live-report.json`, with private screenshots
`/private/tmp/hq-toy-alive.png` and `/private/tmp/hq-toy-ended.png`. The initial
harness incorrectly expected a disabled textbox; the UI removes it. The corrected
continuation passed API, UI and 409 checks. These are parent-reported results,
not browser operations performed by this documentation pass.

The real two-relay cascade's **nine actions** are also reported passing against
a fresh native result; detailed action evidence is still awaited from the browser
agent. Relay **PID 39602** was verified and stopped by exact PID. Persistent HQ
**PID 35507** remains running, with Chrome opened to `http://100.115.53.98:8389`.

## Pending parent integration

Latest parent-verified deployment supersedes the earlier build/PID snapshot:
**0.4.1 is active on all four VM units**, using wheel SHA-256
`98f9aa2f25f06fa5231eda3435f0de8f4aff7b8c32145e459b9fffd51c4416cf`.
Primary **PID 2773379** serves `100.120.250.112:8389` and loopback `8377`;
BC **2776806** serves loopback `8379`; Work-B **3015673** serves
`192.168.0.157:8378` plus its Tailscale and loopback listeners; Sahalat
**1923713** serves `192.168.50.198:8377`. Auth, sessions, workers and routes
checks are healthy with matching package bytes; both primary routines remain.
Mac persistent HQ is **PID 95054**, root revision **`3ceeadb`**, with all **31
runtime files byte-identical** to that wheel. After restart, GET checks showed
**11 total, 10 alive, 1 ended**, with all hosts available.

## Final 0.4.1 visual closure

The review-card display fix is now verified. This pass read the final artifact
`/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T18-28-29.008Z/report.json`:
all nine requests passed, native completion and current-review checks passed,
and `pageErrors` is empty. The parent visually inspected `menu-review.png` in
that directory: **Red**, **Map + Lamp**, and **Pear slices** are all visible;
the review card excludes the old `BROWSER_MENU_RESULT`, Green, Rope and Oat text.
This closes the stale-tail visual issue with the display-only active-review clip;
backend behavior is unchanged.

The fresh batch used the same owned remote toy and confirms single-select,
multi-select, custom text, review and native submission. Its recorded route is
`work-a → local`; the earlier two-relay result remains separate historical
evidence. The report notes zero mapped conversation messages with terminal
fallback and leaves physical iPhone keyboard testing pending. No physical-device
or transcript-mapping success is inferred from this menu test.

## Remaining external integration

External tunnel deployment remains pending. The parent
reports automatic approval review blocked the public tunnel invocation; specific
user confirmation of exposing the control API publicly is pending. No tunnel was
started by this documentation pass and no live HTTPS endpoint is claimed. No public
URL is recorded because none was verified for this handoff. The current optional
`CC_TMUX_PUBLIC_URL` setting still requires a token; the parent must validate
the final tunnel's host/scheme/origin handling while preserving the requested
no-pairing runtime. This pass added no application certificate feature or tunnel
configuration. No source files, runtime config, GUI state or commits were changed.
