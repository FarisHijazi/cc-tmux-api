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
The target origin is restricted to `http://127.0.0.1:8389`.
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
