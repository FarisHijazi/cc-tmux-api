# Mobile control of existing Claude Code sessions

Research and implementation date: 2026-10-08. This is the 0.4.0 worktree.
[README.md](../README.md) owns detailed configuration/API contracts;
[the federation/lifecycle devlog](devlog/claude_20261008-federated-hq-lifecycle.md)
owns current verification and deployment limits. The baseline review, upstream
comparison, original proposal and earlier devlogs are historical evidence, not
current deployment instructions. Links are deliberately plain, not `@` includes.

## Current implementation status

The source now supports local/SSH discovery and HQ peer cascades for session
reads and guarded writes. Lifecycle summaries have a dedicated persistent owner;
each HQ annotates its directly probed hosts before peers are merged. The UI
separates alive/ended/unreachable from activity/outcome emoji and prioritizes
live questions. Workers and adopted interactive sessions retain distinct owners.
Exact fields, history semantics and routing limits belong in the
[README contracts](../README.md#mobile-api), not a second API description here.

The intended deployment runs the same HQ package everywhere. VM domain wrappers
embed it in one process; the Mac's generic HQ replaces the preview and observes
local interactive sessions with worker management disabled. Preserve the Mac's
existing SSH forward and disabled legacy service; runtime identifiers belong in
the handoff devlog. The runtime has no app pairing; HQ serves HTTP and an external tunnel
such as ngrok will provide public HTTPS. Tunnel URL and deployment remain pending.
No Serve/Funnel or application-managed certificate feature is part of the plan.

Updates still use polling, not a WebSocket event stream. Runtime identity is not
a conversation ID across process restarts. Action receipts remain process-local;
push state and lifecycle summaries are durable. Transcript mapping still requires
explicit identity, with terminal fallback when unavailable.

Lifecycle observation is driven by the server's always-running fleet monitor,
not browser polling or push enrollment. Push delivery remains conditional on
configuration. See the [monitor contract](../README.md#actions-and-notifications).

Current test results belong in the latest devlog. The
[browser evidence index](../tests/browser/README.md) separately records prior
real-app toy actions, menus and reconnect tests; those do not validate a newly
deployed federation or physical iPhone. The
[handoff devlog](devlog/claude_20261008-federated-hq-lifecycle.md) records the
parent-reported primary activation and the other nodes' staging status.

## Confirmed requirements

- Primary device: iPhone, used while walking.
- Same HQ on each machine, aggregating peer HQs with cascading reads and writes.
  Replace the old HQ while keeping one worker lifecycle owner per registry.
- No app pairing token. HTTP backend with public HTTPS supplied by ngrok or
  another external tunnel; assigned URL pending. No Serve/Funnel.
- Adopt already-running laptop and VM sessions launched with `tcc`. No required
  replacement launcher, restart, resume, or single Claude account.
- Group by host and working directory. Preserve session identity across reconnects.
- Separate alive/ended/unreachable from activity; reuse `cc-notify` presentation
  and opaque outcome tokens without making an outcome imply process exit.
- Phone notifications and usable single-select, multi-select, and free-text
  answers to Claude's `AskUserQuestion` are core requirements.

The implementation provides conversation and terminal views, opt-in browser push,
and shared per-session mute preferences. Coordinator placement and device delivery
remain deployment/validation concerns; a sleeping coordinator cannot monitor.

## Native UI reference, observed separately

A matching-account isolated native Remote Control toy demonstrated a question
card above the composer, numbered single-choice rows with descriptions, an inline
Other field, a multi-question progress badge, multi-choice checkboxes and explicit
Next/Submit controls. Generic free text and two checked choices were submitted
through the native browser and the toy's CLI resumed. These observations inform
interaction design; they do not test HQ's implementation or justify copying
native account restrictions into HQ.

Private evidence remains under `/private/tmp/cc-native-reference/observations.md`
and its linked captures, not committed transcript content. The reference did not
establish native mobile layout, push delivery, reconnect, or persistent tool
collapse. Current HQ action evidence is owned by the browser index above.

## Baseline implementation: verified from source

The baseline is commit `092fc54` (version 0.3.0).

| Responsibility | Existing owner | Consequence |
|---|---|---|
| Unattended workers, revival, reaping | [workers.py](../src/cc_tmux_api/workers.py) | Keep its prefix/owner checks; adopting a session must not enrol it in automatic revival or reaping. |
| HTTP API and lifecycle | [server.py](../src/cc_tmux_api/server.py) | Startup revives workers. Do not start a research server against the real registry. |
| Dashboard at baseline | `src/cc_tmux_api/static/app.js` at `092fc54` | Worker cards polled every eight seconds; other tmux sessions were read-only. No host aggregation or push. |
| Interactive launching | The user's existing `tcc` shell function | Fresh sessions get a UUID in Claude argv; resume/continue use different paths. tmux names derive from directory basenames and can be reused. |
| Hook events and presentation | [cc-notify](https://github.com/FarisHijazi/cc-notify) | Reuse this owner for status and event publication. |

The baseline API had no authentication and deliberately bound loopback. Its spawn
path disables `AskUserQuestion` and skips permissions for unattended workers.
Those defaults are inappropriate for an interactive mobile-session mode. The
baseline worker `send_text` path also did not verify delivery or distinguish a
text prompt from an open menu. The upgraded HTTP send route now delegates to
guarded fleet actions instead of this bare-name path.

Live metadata inspection reached the laptop and all three requested VMs. Local
Claude panes can report a version string as `pane_current_command`, rather than
the word `claude`; a command-name-only detector would miss them. Hub panes are
views of other sessions and must not appear as duplicate Claude conversations.

The installed macOS Tailscale app reported no Serve configuration. The standalone
Homebrew CLI did not return promptly; the app-bundled CLI did. No Serve settings
were changed.

## Existing applications

These are source/documentation findings, not a hands-on endorsement. Upstream
feature claims are distinguished from properties checked in code.

| Candidate | Fit for existing tmux sessions | Remaining mismatch |
|---|---|---|
| [ssh-to-go](https://github.com/awkto/ssh-to-go) | Documents SSH discovery, multiple hosts, xterm.js terminal attachment, authentication. | No documented Claude conversation rendering, directory grouping, or built-in agent push. |
| [claude-wormhole](https://github.com/ssv445/claude-wormhole) | Closest terminal/PWA option: source calls `tmux attach-session`, lists existing sessions, groups directories, and uses web-push. | Local host only; independent status hooks; no structured question cards established. |
| [HAPI](https://github.com/tiann/hapi/blob/main/docs/guide/how-it-works.md) | Rich chat, machine/project organization, question controls, PWA/native clients. | Documented CLI starts/manages agents; no demonstrated live adoption of arbitrary existing tmux processes. |
| [Happy](https://github.com/slopus/happy) | Native mobile experience and structured question forms. | Requires its managed session lifecycle; no documented arbitrary-process adoption. |
| [tlive](https://github.com/y49/tlive) | Hook-based remote approvals can work with ordinary Claude sessions. | Its live terminal requires wrapping the command; not a complete tmux-fleet replacement. |
| [Native Remote Control](https://code.claude.com/docs/en/remote-control) | `/remote-control` can enable access inside an existing interactive session. | Claude account/organization coupling remains, and it does not discover arbitrary SSH/tmux sessions. |
| [MuxPod](https://github.com/moezakura/mux-pod) | Direct SSH/tmux client. | Android, not the requested iPhone. |
| [tap-to-tmux](https://github.com/flavio87/tap-to-tmux) | Notification-to-SSH/tmux deep links, including an iOS Blink workflow. | Separate terminal app instead of a unified conversation/host/project inbox. |

The two similarly named `claude-code-web` projects were also checked:
[vultuk](https://github.com/vultuk/claude-code-web) launches its own PTYs, while
[fafawlf](https://github.com/fafawlf/claude-code-web) lists important remote/mobile
features as future work. Neither establishes adoption of the user's running fleet.
[Companion's migration](https://github.com/The-Vibe-Company/companion/blob/main/docs/repository-migration.md)
also makes older comparisons unreliable: current code and the historical Claude
frontend are different products.

### Closest candidate, inspected locally

`claude-wormhole` source was cloned read-only at
`18e387c5b9e8b2cd1baa228663410e23ab555e4e` (package 1.6.0).

- `server.ts` attaches node-pty to an existing local tmux session and forwards it
  over WebSocket. This verifies adoption, rather than merely historical resume.
- `src/lib/tmux.ts` enumerates local sessions and reads state files created by its
  own hooks. It does not aggregate SSH hosts.
- Its restart helper launches `cld --resume`, so attachment compatibility does not
  imply that all management operations preserve `tcc`.
- Its WebSocket upgrade path checks session-name syntax/existence but contains no
  authentication check. The project documents private-network deployment.
- The documented installer touches hooks/tmux/Tailscale. It was not run.

Do not repeat upstream claims that tmux processes themselves survive a reboot:
resurrection restarts processes and resumes available conversations.

## Question menus and status integration

The existing cc-notify emitter publishes `ts`, `host`, `sid`, `event`, `token`,
`color`, `title`, `cwd`, `base`, `branch`, `tmux`, `ntype`, and `msg` to its JSONL
event file. `PreToolUse(AskUserQuestion)` becomes `menu`; `PostToolUse` becomes
`tool`. It discards `tool_input` and `tool_use_id`.

Status policy lives in `cc-lib.sh:cc_present`; remote collection lives in
`bin/cc-remote-bridge`. The current Mac has remote emitting disabled, so assuming
the remote event file contains local events is incorrect. The bridge's short replay
and timestamp-only deduplication are not a durable mobile inbox or reconnect log.

An important policy gap: menu events update status, but the bridge's banner path
handles only notification/stop events. Simply copying existing banner rules would
not guarantee a phone alert for `AskUserQuestion`. Recommend explicitly alerting
on new question IDs while preserving quiet polling/idle suppression.

The [official hook reference](https://code.claude.com/docs/en/hooks#tools-that-require-user-interaction)
supports answers through a synchronous `PreToolUse` result: return `allow` and
`updatedInput` containing the original `questions` plus an `answers` map. This is
a supported future-question integration point, not an API for a menu that has
already opened after its hook returned. Holding that hook also delays the normal
terminal question UI. Async hooks cannot retroactively supply the decision.

Therefore validate two paths separately:

1. Existing open menus: identify the actual menu and control the exact tmux pane.
   Do not assume the pending tool input is available in the transcript: the
   experiment below found it missing until submission. Selected/focused terminal
   state needs observation even when structured input is available.
2. Future questions: optionally extend cc-notify to publish structured questions
   and correlation IDs. Only choose a blocking hook answer transport if its local
   terminal behavior is acceptable.

Never inject ordinary prompt text into an unrecognized menu. Reuse cc-notify's
`bin/cc-prompt-state` for its existing prompt-empty check. A per-pane lock prevents
two phone actions interleaving; it does not lock out a human typing locally.
Verify the targeted question before each action and show explicit uncertainty if
the terminal changed. Do not promise atomicity against simultaneous local input.

## Original implementation proposal

The original recommendation was to extend cc-tmux-api and cc-notify if conversation-first operation is
required. Choose an existing terminal client if terminal-first operation is enough.
No evaluated project demonstrates the complete requested combination as-is.

```text
iPhone home-screen app
    HTTPS over Tailscale
    authenticated mobile routes in cc-tmux-api
        snapshot + reconnectable updates + push subscriptions
        host/directory/session index
        existing tmux command owner
            laptop and configured SSH hosts
        cc-notify publication contract
            lifecycle + outcomes + question correlation
```

- Keep managed workers and adopted sessions distinct. Mobile routes require
  explicit configured hosts, independent of unattended worker prefix ownership.
- Identify conversations by configured host ID and Claude session UUID. Identify
  the current runtime separately by tmux socket/server generation and pane ID.
  Resolve transcript paths from captured metadata; never guess the newest file in
  a directory. Resumed/forked/nested sessions need explicit mapping tests.
- Reuse or extend the existing cc-notify transport/publication owner. Do not add
  a second stream per host merely to feed the mobile page. A headless coordinator
  requires separating collection from macOS banner/focus side effects.
- Persist push deduplication and subscriptions; preserve cursor/replay semantics
  across reconnects. Never automatically retry an ambiguously delivered answer.
- Record host reachability/freshness separately from the last session outcome.
  An unreachable host must not look empty or completed.
- Authenticated same-origin API, CSRF/origin checks on writes and WebSockets,
  loopback service behind private HTTPS. No Claude account required for the app.
- Do not cache transcripts in the service worker. Cache only the app shell/assets.

### Original mobile interaction proposal

Attention inbox first, followed by host/directory grouping and search. Each row
shows title, source host, directory, latest meaningful outcome, freshness, and
unread activity. Tap into readable messages with folded tool details. Keep the
composer within thumb reach; preserve drafts through backgrounding and reconnects.

Question cards show full descriptions, single/multi-selection, free text, and one
explicit send action. A separate terminal view exposes navigation, Space, Enter,
Escape, Tab, and interrupt controls for unrecognized dialogs. Do not resize every
live desktop session to phone width just to render the conversation view.

The [iOS Web Push requirements](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/)
include adding the app to the Home Screen and granting notification permission
from a user gesture. [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)
provides private HTTPS. Notification enrollment and locked-phone delivery still
require testing on the actual iPhone; desktop emulation cannot prove them.

## Original delivery gates (historical)

These were the original proposal's gates, including the superseded authenticated
HTTPS deployment assumption. Use the current handoff for today's runtime and
verification status; this list is not a new rollout prescription.

1. Discover all configured hosts and existing sessions, with correct identity,
   grouping, explicit offline errors, and no startup mutation of those sessions.
2. Demonstrate real single/multi-select and free-text answers in an owned toy
   session. Test menu changes, repeated taps, local answers, and interrupted sends.
3. Build the phone UI on the existing service; test real loading/errors, keyboard
   layout, draft retention, screen-reader labels, and reconnects in WebKit.
4. Wire cc-notify events, durable deduplication, and notification deep links.
5. Deploy authenticated HTTPS to the chosen coordinator and verify on the iPhone:
   locked-screen push, tap-to-session, answer, Wi-Fi/cellular transition, laptop
   sleep, and one unavailable VM. A sleeping laptop's own sessions stay offline.

## Historical research validation and limits

The earlier research recorded source review and read-only host discovery. Its
manual native tmux single-select
and multiselect were tested successfully in an isolated Claude Code 2.1.294 toy
session. Single-select chose Green; multiselect chose Alpha and Gamma, with a
review stage before final submission. Both were verified in tool results.

The pending tool input/ID was absent from the transcript at the initial menus,
after toggling selections, and at the multiselect review screen. It appeared after
submission. This was safe mode with custom integrations disabled: do not generalize
the result to every Claude configuration, but do not build on unverified early
transcript availability either.

Follow-up tests in the same owned toy verified:
- Free text: `Oat biscuit, lightly salted` arrived intact, including its comma.
- Two-question batch: a single answer of South plus multiselect Map/Rope arrived
  together in the matching tool result.
- Escape: the question was rejected with an error tool result, not silently
  answered with the highlighted option.
- An empty free-text submission also rejected the question in this test. Mobile
  forms should distinguish an empty draft from an intentional cancellation.

These were earlier manual terminal experiments, not validation of the implemented
mobile API or UI action path. Captures can
briefly lag input. The owned toy was left at an idle prompt, and its transcript
evidence was inspected to verify the answers.

That research did not inspect the native Claude remote-control browser UI or
deploy the application. Its no-deployment conclusion applies to the research
phase. Current federation verification and pending deployment are tracked in
[the latest devlog](devlog/claude_20261008-federated-hq-lifecycle.md); subsequent
app action runs are recorded in the browser evidence index. These historical
terminal experiments do not establish physical iPhone or deployed-federation
correctness.
