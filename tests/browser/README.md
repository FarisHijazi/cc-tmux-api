# Mobile browser evidence index

This directory owns browser scripts and their evidence index only. The contract
comes from [the mobile research](../../docs/mobile-control-research.md).
Links are deliberately plain links, not `@` includes.

## Runtime and launch

`mobile_e2e.mjs` uses the upstream Playwright library from an external npm package
directory (`MOBILE_BROWSER_ENV`, default `/private/tmp/cc-mobile-browser`).
No existing repository browser harness was present when this was prepared.
App-specific assertions must use the real application; there is no mock API.
Playwright's [fresh browser contexts](https://playwright.dev/docs/browser-contexts)
and [device emulation](https://playwright.dev/docs/emulation) provide isolation.
No existing personal Chrome profile is read, copied, or attached.

Runtime check, without contacting the app or tmux:

```sh
node tests/browser/mobile_e2e.mjs --self-test
```

After parent readiness, the harness checks `/api/auth`. If `required` is false,
it skips login and never reads a key file. For the current no-pairing preview:

```sh
MOBILE_READY=1 MOBILE_EXPECT_NO_PAIRING=1 node tests/browser/mobile_e2e.mjs
```

If a future preview requires pairing, supply its runtime key file:

```sh
MOBILE_READY=1 MOBILE_KEY_FILE=/path/to/runtime-key node tests/browser/mobile_e2e.mjs
```

Optional settings: `MOBILE_HEADED=1`, `MOBILE_UI_PATH`, `MOBILE_READY_SELECTOR`,
`MOBILE_EVIDENCE_DIR`, `MOBILE_AUDIT=1`. The latter enables the real-data
interactions in [read_only_audit.mjs](read_only_audit.mjs): grouping, search,
attention filtering, detail tabs, offline/online, dark mode, and composer bounds.
Draft typing is allowed only when the reserved toy is discovered, and never sent.
The allowed origins are the persistent HQ `http://100.115.53.98:8389`, the
parent-owned ephemeral relay `http://127.0.0.1:8391`, and the historical local
preview `http://127.0.0.1:8389`. The persistent HQ is now the default.
One additional HTTPS origin can be explicitly allowlisted at runtime with
`MOBILE_APPROVED_ORIGIN`; this does not select the target or bypass readiness.
Both the approved origin and `MOBILE_BASE_URL` must be canonical origins with
no credentials, path, query, or fragment (a trailing slash is allowed).
Read-only smoke mode logs in via `/api/auth/login` only when required, checks `/api/sessions`,
waits for the UI's successful sessions response, and captures 390×844 and
1440×1000 views plus a shortened mobile viewport. UI API writes are blocked.
No secret, cookie jar, request bodies, or trace is written to evidence.
Screenshots can contain actual session information: keep them local and review
before sharing. Evidence defaults to timestamped directories under the temp env.

## Verification status

The harness self-test is explicitly synthetic runtime validation, not app evidence.
An API success and screenshots alone do not prove grouping or interactions.
Each run writes `report.json` with passed/failed checks and remaining coverage.

| Goal | Required real evidence | Current status |
| --- | --- | --- |
| Mobile/desktop layout | Actual UI screenshot and overflow assertion | Passed real Chrome audit |
| Host/project filters | Actual discovered sessions grouped/filtered | Passed; reserved toy absent |
| Text send | UI send, actual API result, native toy echo | Passed on local and remote owned toys |
| Single/multi/free text | Real Claude menus, UI actions, native result | Passed fresh full batch using Continue, no keyboard workaround |
| Draft/reconnect | Type draft, offline/online, retain draft and recover | Passed on actual owned toys |
| Stale revision | Old revision rejected without native input | Passed on local owned toy |
| Explicit errors | Actual API failure visibly represented | Passed: stale 409 and remote empty-prompt 409 |
| Keyboard | Real iPhone keyboard and composer visibility | Desktop emulation cannot prove |

Reserved toy: `cc-mobile-research-toy:0.0`, pane `%121`, cwd
`/private/tmp/cc-mobile-research`. Read-only identity inspection confirmed all
three on 2026-10-08, with command `2.1.294`. Recheck immediately before any input.
The read-only harness never sends input. The separately gated
[toy_actions.mjs](toy_actions.mjs) is explicitly authorized to interact with the
owned toys only; no further approval is needed. It verifies native identity and
restricts browser writes to the exact discovered toy action URL.

## Preparation log — 2026-10-08

Prepared a readiness-gated harness and this evidence index within the assigned
directory. Root package metadata, application code, and other agents' files are
outside this task's ownership. No commit was made. Parent/subagent messaging and
spawn tools are unavailable in this worker's exposed tool set.

Validation: `node --check` passed. Chrome runtime self-test passed both viewport
captures and overflow assertions, with no page errors, at
`/private/tmp/cc-mobile-browser/evidence/2026-10-08T16-46-16.639Z/report.json`.
An invocation without `MOBILE_READY=1` failed before launching Chrome as intended.
The initial sandboxed Chrome launch failed; the approved unsandboxed launch
passed using a fresh temporary profile. These screenshots are synthetic runtime
checks only. The real app server was not contacted.

## Live preview audit — 2026-10-08

After parent readiness, authenticated read-only Chrome checks ran against the
real preview. Evidence root:
`/private/tmp/cc-mobile-browser/evidence/2026-10-08T16-56-19.062Z/`.
`report.json` records the actual results; its initial static pending list still
includes grouping even though the individual grouping checks passed. Subsequent
runs remove that completed entry automatically.

- Eight sessions; all four configured hosts reported available.
- Both viewports passed host/directory grouping, real-title search, no-match
  recovery, attention filter, conversation/detail navigation, terminal tab,
  horizontal overflow checks, and offline-banner recovery; no page errors.
- Inspected `iphone.png`, `desktop.png`, conversation and dark-detail views.
  Clear hierarchy, readable text, sensible truncation, and stable bottom composer.
- `iphone-short-composer.png` shows the actual UI at 390×480, with Send bounds
  and hit testing passing. An earlier immediate-resize screenshot contained a
  capture artifact; waiting for paint and capturing at CSS scale resolved it.
  This is not evidence of physical iOS keyboard behavior.
- Reserved toy `%121` was absent from both API snapshots. Read-only tmux
  reinspection confirmed its expected session, cwd, and command. Its tty
  `/dev/ttys019` has one process, PID 6697, with executable basename `2.1.294`.
  Backend owner should investigate discovery before action tests proceed.
- The sampled real session has terminal output but displays “Conversation not
  available.” This is explicit fallback behavior; transcript mapping remains
  unverified. No claim of conversation rendering success is made.
- [fleet_probe.py](../../src/cc_tmux_api/fleet_probe.py) currently sets session
  `updated_at` to probe time; [app.js](../../src/cc_tmux_api/static/app.js)
  displays it as “Just now.” Recommend distinguishing last checked from last
  activity, or supplying an activity timestamp to that label.

No API action or tmux input was sent. No draft was typed because the reserved toy
was absent. Product files were not modified; findings are for parent/owners.

## Interactive results — 2026-10-08

Local toy discovery was subsequently fixed by the backend owner. Local evidence:
`/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T17-01-48.511Z/`.
Actual UI send returned 200 and the prompt appeared in the native pane. Reload
and offline return retained the local draft. A second UI submission using the
previous revision returned real HTTP 409, displayed the error, retained its draft,
and did not reach tmux. Claude could not answer: “You've hit your weekly limit ·
resets Oct 11 at 10am (Europe/Prague).” The runner originally reported only an
echo timeout; its quota recognition now includes weekly limits. A later cursor
control attempt returned 409 and was not automatically retried.

The parent explicitly authorized a separate remote toy. Created with
[create_remote_toy.sh](create_remote_toy.sh) only after verifying name absence:
host `work-a`, session `cc-mobile-browser-toy-20261008:0.0`, pane `%37`, PID
`2265987`, command `claude`, UUID `c620be5c-5c58-4ad6-a039-97fe1673fa6f`, cwd
`/tmp/cc-mobile-browser-toy-c620be5c-5c58-4ad6-a039-97fe1673fa6f`. Safe mode,
strict MCP config, only AskUserQuestion, default permission mode. The separate
native-reference agent's toy is never targeted.

Remote first-send evidence:
`/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T17-04-41.009Z/`.
API returned 409: “Text requires a verified empty Claude prompt or focused
free-text option.” Actual screenshot displayed the error and retained draft.
Native capture showed the normal prompt with gray suggestion text, no trust
dialog, and no inserted browser text. The original response body was not logged;
the evidence report explicitly distinguishes screenshot diagnosis from raw body.
Subsequent runs record API response bodies. Backend owner was notified; identity
guarantees must remain strict.

Once that guard is fixed, run `toy_actions.mjs` with `MOBILE_TOY_ACTIONS=1`,
`MOBILE_REMOTE_TOY=1`, `MOBILE_MENU_TESTS=1`, and the runtime `MOBILE_KEY_FILE`.
[remote_menus.mjs](remote_menus.mjs) sends one real three-question batch through
the UI, selects Green and Map/Rope, inserts a custom snack answer, and verifies
the model result in native text without fabricating transcript identity.

## Real menu batch result

The empty-prompt backend fix enabled the browser to send its first real batch.
Initial menu screenshots/actions:
`/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T17-09-46.407Z/`.
Green selection and Map/Rope toggles worked, but the original “Review selection”
button sent Enter on focused Rope, toggling it off instead of advancing. The
native batch has a separate unnumbered Next row. Reported to both owners.

The same batch was completed through the actual UI, restoring Rope then using
explicit Right keyboard navigation to Snack. Custom answer insertion,
confirmation, and review submission succeeded. Evidence:
`/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T17-14-00.239Z/`.
The report's final failure is specifically API transcript verification, not menu
delivery. A separate read-only verification captured native model output and
exact answers at
`/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T17-15-44.832Z/`:
`BROWSER_MENU_RESULT_1791479390303`, Green, Map/Rope, and “Oat biscuit, lightly
salted”. `native-toy-tail.txt` is direct tmux evidence. The API has zero messages
for this safe-mode session; UUID mapping is not inferred from a recent file.
The expected terminal fallback is distinguished from verified native completion.

The mobile review screenshot omits the chosen answer summaries even though the
native review displays them. It provides Cancel/Submit controls. Recommended
frontend improvement: present those actual summaries before confirmation.

The final updated read-only audit passed at
`/private/tmp/cc-mobile-browser/evidence/2026-10-08T17-10-13.305Z/`.
Local conversation mapping now works, with its actual quota message rendered.
Its working status despite the quota notice is a remaining classification issue.
Remote Left/Right controls both returned 200; the first harness full-screen
equality assertion was invalid because built-in suggestions and status redraw.
The assertion now checks that no conversation messages were added.

## Final full-batch pass

After frontend and backend owners aligned Continue with the native Next row,
a fresh complete batch passed without keyboard-navigation workarounds:
`/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T17-17-49.097Z/`.
All nine actual app action requests targeted verified remote toy `%37`/PID
`2265987`. Native output confirms the fresh marker
`BROWSER_MENU_RESULT_1791479872692` and Green, Map/Rope, and “Oat biscuit, lightly
salted”. The report lists all successful actions and draft/reconnect checks.

Key actual UI screenshots: `menu-multi-selected.png`, `menu-custom-inserted.png`,
`menu-review.png`. Direct native evidence: `native-toy-tail.txt`. The session is
left idle after its successful final model response. The separate reference toy
and all user sessions received no input from these tests.

At that run, the remaining visual improvement was that the review card omitted
their summary; the native review contains the actual values. The safe-mode API
transcript remains unmapped and correctly uses terminal fallback. Physical iOS
keyboard, installed-PWA push, and HTTPS secure-cookie checks are not certified
by these HTTP Chrome runs. All browser contexts closed on completion; ready for
the parent to switch the preview to HTTPS.

See [REFERENCES.md](REFERENCES.md) for the eight primary-source recommendations
on native Remote Control patterns and iPhone PWA behavior.

## Latest-source final audit — 17:25 UTC

Parent restarted preview PID 53164 on port 8389 with server/notification changes.
Reverified remote toy `%37`, PID 2265987, cwd and command before interacting.
Only that owned toy was eligible for test input; no Tailscale operations occurred.

- Read-only phone 390×844 and desktop 1440×1000 audit passed:
  `/private/tmp/cc-mobile-browser/evidence/2026-10-08T17-24-56.437Z/report.json`.
  Eleven sessions across four available hosts; grouping, search, filters,
  terminal/detail navigation, draft reload, offline recovery, overflow, and
  reduced-height composer geometry passed. Actual light/dark screenshots saved.
- A prior run at `2026-10-08T17-24-18.348Z` failed desktop offline-banner timing
  after reload. Waiting for detail readiness before setting offline resolved the
  harness race; the original failed evidence remains retained.
- Fresh full remote menu E2E passed with nine actual UI action requests and zero
  page errors:
  `/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T17-25-17.754Z/report.json`.
  Continue, stable custom focus, collapsed composer, and review/Submit worked.
  Native marker `BROWSER_MENU_RESULT_1791480322358` confirms the new model result:
  Green, Map/Rope, and “Oat biscuit, lightly salted”. Native output is saved in
  `native-toy-tail.txt`; final result assertions isolate text after the new marker.
- Review now includes real answer text. Remaining visual issue:
  `menu-review.png` initially shows an older result because the summary renders
  the entire terminal tail. Recommend extracting the active review block, so
  current choices appear first. This is a UX finding, not a submission failure.
- Other useful actual screenshots: `menu-continue.png`,
  `menu-custom-inserted.png`, `iphone.png`, `desktop.png`, and
  `iphone-short-composer.png` in their respective evidence directories.

Safe-mode remote transcript still correctly falls back to terminal when its UUID
cannot be mapped. The newly added markdown renderer loaded without page errors;
these toy screenshots do not certify rendering of mapped markdown conversations.
Physical iPhone keyboard/PWA push and HTTPS secure cookies remain outside these
HTTP desktop-Chrome checks. Both runs closed their isolated contexts. No product
files changed and no commits were created.

## No-pairing verification — 17:34 UTC

After the parent removed the token and restarted the local preview, clean Chrome
phone/desktop contexts passed without a key file, login request, or pairing cookie.
Both `/api/auth` responses had `required:false`; the actual inbox rendered with
no pairing screen. Evidence:
`/private/tmp/cc-mobile-browser/evidence/2026-10-08T17-34-03.287Z/report.json`.
Actual screenshots: `iphone.png` and `desktop.png` in that directory. No toy
requests or API action writes were sent. This was a focused no-pairing smoke run;
the earlier full menu batch remains the interaction evidence.

## Federation deployment preparation

[federation_checks.mjs](federation_checks.mjs) extends the existing harness using
the routed identity contract in [fleet.py](../../src/cc_tmux_api/fleet.py).
These commands are prepared, not executed deployment evidence. Wait for parent
readiness; parent owns starting the relay with its unique node ID and worker
management disabled. This harness starts no servers and changes no networking.

Read-only phone/desktop audit of persistent HQ, including every actual session:

```sh
MOBILE_READY=1 MOBILE_EXPECT_NO_PAIRING=1 MOBILE_ALL_DETAILS=1 MOBILE_AUDIT=1 \
MOBILE_MIN_PEER_HOPS=1 MOBILE_BASE_URL=http://100.115.53.98:8389 \
node tests/browser/mobile_e2e.mjs
```

After the parent starts the loopback relay, verify two peer hops, every detail,
unchanged owner identities, and the same origin set as the root:

```sh
MOBILE_READY=1 MOBILE_EXPECT_NO_PAIRING=1 MOBILE_ALL_DETAILS=1 MOBILE_AUDIT=1 \
MOBILE_MIN_PEER_HOPS=2 MOBILE_COMPARE_ROOT=http://100.115.53.98:8389 \
MOBILE_BASE_URL=http://127.0.0.1:8391 node tests/browser/mobile_e2e.mjs
```

Only after the parent's explicit run signal, send ONE fresh batch through the
relay to the existing owned remote toy. No second root batch is needed:

```sh
MOBILE_READY=1 MOBILE_TOY_ACTIONS=1 MOBILE_EXPECT_NO_PAIRING=1 \
MOBILE_REMOTE_TOY=1 MOBILE_MENU_TESTS=1 MOBILE_FRESH_BATCH_ONLY=1 \
MOBILE_MIN_PEER_HOPS=2 MOBILE_BASE_URL=http://127.0.0.1:8391 \
node tests/browser/toy_actions.mjs
```

The runner verifies native session/pane/cwd/command/PID before every browser
action, unwraps the routed ID to its owner identity, and allows POST only to
that discovered toy's action endpoint. Fresh-batch mode blocks a second model
prompt. Single/multi/custom answers and native result must pass; writes are
never automatically retried. No key file is read when auth is not required.

Transcript diagnosis corrected the earlier historical safe-mode descriptions:
outer toy UUID `c620be5c-…` has no transcript file; the six-message transcript
`a4ba347f-…` belongs to nested Claude PID 2292649 on a different tty. It must not
be substituted for the outer session. Current detail audits record the backend's
typed `transcript_status`; native result verification does not fabricate mapping.

Preparation validation: JavaScript syntax checks, bounded route-decoding and
origin allowlist checks, and closed readiness gates. No live deployment requests,
Chrome launch, tmux input, or model batch was performed during preparation.

## Persistent HQ and two-forwarder pass — 18:17 UTC

After parent go-ahead, real clean Chrome contexts passed both 390×844 and
1440×1000 audits. All 11 sessions were alive; eight arrived through HTTP peers.
Root evidence: `/private/tmp/cc-mobile-browser/evidence/2026-10-08T18-14-49.171Z/`.
Relay evidence: `/private/tmp/cc-mobile-browser/evidence/2026-10-08T18-15-29.311Z/`.
Every detail preserved its owner identity. Relay/root origin sets matched;
VM routes traversed two forwarders. Root screenshots show the clean four-host
footer despite the API also exposing peer host records. Mac ansible returned
19 mapped messages with `transcript_status=available` through both routes.

Fresh relay batch: `/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T18-15-32.834Z/`.
All nine actions returned HTTP 200 via `root → work-a → local`, owner
`hq-work-a`. Native pane/PID, tmux generation, session creation and Claude
birth were checked before input. Fresh native marker
`BROWSER_MENU_RESULT_1791483338377` confirms Green, Map/Rope and
“Oat biscuit, lightly salted”. No other pane received input; no local toy prompt.
Zero page errors. `menu-review.png` still initially displays an older result
from the terminal-tail summary; current answers exist but are not shown first.

[controls_audit.mjs](controls_audit.mjs) verified all nine keyboard controls are
enabled for each of 11 sessions at both widths without pressing them (22 checks).
Evidence: `/private/tmp/cc-mobile-browser/evidence/controls-2026-10-08T18-17-06.849Z/`.
Includes `iphone-footer.png`, `desktop-footer.png`, and phone/desktop screenshots
of the 19-message ansible conversation. All contexts closed. Relay left running
for the parent; local toy no longer has a browser-test dependency and may enter
the parent's separately owned Alive → Ended test.

## Final handoff and lifecycle transition

Browser work is complete; no further tests or commits were run after handoff.
The root and relay audits above each covered 390×844 and 1440×1000. The nine
HTTP 200 actions were: prompt send, Green selection, Map toggle, Rope toggle,
Continue, custom focus, custom insertion, custom confirmation, and Submit.
The fresh native result marker and reports above are the authoritative evidence.

Parent subsequently reported successful API/UI transition of only the owned
local toy `%121`/PID 6697 to Ended: expected fleet is now 11 total, 10 alive,
one ended. This lifecycle result is parent-reported, not an additional browser
run by this worker. Ended UI correctly removes the composer rather than leaving
a disabled textbox. Parent's continuation verifier was checking absent controls
and HTTP 409 at handoff; those checks are not claimed complete here. The remote
toy remained untouched by the parent. This worker left the relay running and
all its Chrome contexts closed.

## Narrow review-clipping retest

Preparation required parent to serve the UI patch and give go-ahead; the
completed result is recorded below.
`MOBILE_MENU_VARIANT=current-review` selects Red, Map/Lamp, and “Pear slices”
instead of the old batch answers. It skips the broad draft/reconnect audit.
Before Submit it saves `menu-review.png`, `native-review-before-submit.txt`, and
`review-card-before-submit.txt`, requires the native current answer block, and
rejects any old `BROWSER_MENU_RESULT`, Green, Rope, or Oat biscuit in the review
card. Then it submits and verifies the fresh native model marker and new answers.
An assertion failure before Submit leaves the review open; no automatic resend.

Use root directly; parent has stopped the relay. After readiness only:

```sh
MOBILE_READY=1 MOBILE_TOY_ACTIONS=1 MOBILE_EXPECT_NO_PAIRING=1 \
MOBILE_REMOTE_TOY=1 MOBILE_MENU_TESTS=1 MOBILE_FRESH_BATCH_ONLY=1 \
MOBILE_MENU_VARIANT=current-review MOBILE_MIN_PEER_HOPS=1 \
MOBILE_BASE_URL=http://100.115.53.98:8389 node tests/browser/toy_actions.mjs
```

Preparation passed syntax and offline answer-configuration checks. No browser,
app requests, native pane reads, or model input occurred during this preparation.

Parent subsequently gave go-ahead for root `http://127.0.0.1:8389` on 0.4.1.
The narrow live run passed; evidence directory:
`/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T18-28-29.008Z/`.
All nine UI actions returned HTTP 200 and no page errors occurred. The actual
390×844 `menu-review.png` visibly contains Red, Map/Lamp and Pear slices, matching
`native-review-before-submit.txt`. `review-card-before-submit.txt` contains no old
model marker, Green, Rope or Oat biscuit. This resolves the earlier stale review
summary visual finding. Submission completed and `native-toy-tail.txt` verified
fresh marker `BROWSER_MENU_RESULT_1791484113504` with the new answers. Native
identity/generation checks ran before input; only owned remote toy `%37` received
input. No broader audits or other session interactions were performed. Chrome
closed on completion; relay was not contacted or started. No commit was created.

## HTTPS deployment preparation

Deployment is not ready yet; wait for the parent's explicit run request before
contacting it. Configure the actual approved HTTPS origin at runtime; the domain
below is only a placeholder. Setting the allowlist does not launch a browser:

```sh
export MOBILE_APPROVED_ORIGIN=https://hq.example.com
export MOBILE_BASE_URL="$MOBILE_APPROVED_ORIGIN"
```

After readiness, the existing `mobile_e2e.mjs` read-only harness and
`toy_actions.mjs` fresh-batch harness accept that exact origin. Their readiness,
action gates, isolated Chrome contexts, and reserved remote toy PID/generation
checks remain required. The ended local toy is never a model-input target.
HTTPS certificate verification remains enabled. No wildcard/subdomain approval
is inferred, and malformed or non-HTTPS approved origins fail closed.

Preparation validation: JavaScript syntax, offline allowlist acceptance/rejection,
and closed harness readiness gates. No deployment requests, browser launch,
native reads, or session input during this preparation. No commit created.

## Public HTTPS deployment result — 20:54 UTC

Real isolated Chrome verified 390×844 and 1440×1000 against the runtime-approved
HTTPS origin. Fleet: 12 records, 10 alive and two ended, five connected machines.
All actual session details were read successfully in the recovery audit;
identity/routes, grouping, search, offline recovery and viewport checks passed.
Ended detail correctly removes composer and keyboard controls. Mac ansible now
has 20 mapped messages. No personal Chrome profile was accessed.

Evidence and retained failures:

- `2026-10-08T20-50-47.127Z/report.json`: initial harness incorrectly required
  live transcript status on historical ended rows. `federation_checks.mjs` now
  restricts that assertion to alive rows and selects an alive UI audit sample.
- `2026-10-08T20-51-19.866Z/report.json`: real transient HTTP 503 reading Mac
  ansible on phone; desktop subsequently passed. This failure is not erased.
- `2026-10-08T20-52-24.024Z/report.json`: both viewports read every detail;
  desktop passed fully. Phone attention expected three from its old snapshot
  but rendered four while the concurrent owned-toy batch opened a question.
- `https-phone-recheck-2026-10-08T20-53-42.726Z/report.json`: narrow sequential
  read-only recheck after toy completion passed current attention count three,
  five-host footer, and ended detail with absent composer/controls. Screenshots
  include `iphone-inbox.png`, `iphone-attention.png`, `iphone-ended-read-only.png`.

These directories are under `/private/tmp/cc-mobile-browser/evidence/`.
The two overlapping tests caused the attention assertion race; it was not a UI
defect. The transient 503 remains an observed deployment failure with recovery.

Fresh HTTPS owned-remote-toy evidence:
`/private/tmp/cc-mobile-browser/evidence/toy-2026-10-08T20-52-12.732Z/report.json`.
All nine actions returned HTTP 200, current review contains only Red, Map/Lamp,
and Pear slices, and native marker `BROWSER_MENU_RESULT_1791492738055` verifies
the final received answers. Zero page errors. Only the exact owned remote toy
received input; no extra batch was sent for the read-only recovery checks.

Secure context and push evidence:
`/private/tmp/cc-mobile-browser/evidence/https-push-2026-10-08T20-51-56.501Z/report.json`.
Both Chrome contexts were secure with activated `/sw.js`, PushManager and
Notification support, available push config/public key, and enabled notification
UI. The desktop enrollment attempt displayed “Registration failed - permission
denied”; no subscription was created or left behind. Push setup is ready but
delivery is unverified. Physical iPhone/PWA behavior remains unverified.

Temporary focused verifiers are in `/private/tmp/cc-mobile-browser/`:
`https_push_audit.mjs` and `https_attention_recheck.mjs`. No product/infra files
were edited. All Chrome contexts closed and no commit created.
