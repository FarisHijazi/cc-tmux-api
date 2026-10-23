# Native-first HQ UI, and the Remote Control protocol as claude.ai uses it

## What changed (0.6.0)

Native sessions now get the controls claude.ai gives a Remote Control session,
driven by the same structured messages, with no terminal parsing.

- **Session bar.** Current model with a switch box, permission mode, context
  used/window, effort, 5h/7d usage, and any error Claude returned for a change.
  The model and mode shown are only what Claude confirmed: an `init`/`status`
  event, the reply to our `set_model`/`set_permission_mode`, or the worker's own
  `external_metadata` (`context_usage`, `effort_level`, `task_summary`).
- **Transcript.** Tool calls render as cards holding their input and output
  (paired by `tool_use_id`), thinking is collapsible, slash commands show as
  commands and their local output as a note, and the reply streams live from
  `stream_event` text deltas (polled every second while working).
- **Actions.** `set_model` and `set_permission_mode` send control requests.
  Typed `/model x` in the composer becomes `set_model`; a chat message starting
  with `/model` is rejected, because Claude then also saves that model as the
  user's default in the shared `settings.json` (it happened once in testing).
- **Approvals** echo `tool_name` and `toolUseID` exactly as claude.ai does.
  **Always allow** returns Claude's own `permission_suggestions` as
  `updatedPermissions`, nothing broader. Verified live: a Write approval with
  the `setMode acceptEdits` suggestion created the file and Claude switched the
  session to `acceptEdits` by itself.
- **Staleness.** Every streamed token bumps the session revision, so commands are
  only stale when they predate the last human input or bridge reconnect; answers
  to a request still need the exact revision. Revisions parse as ASCII digits.
- Metadata from the worker is clipped and allow-listed before it is stored.

Non-Claude models: the account proxy makes sessions first-party, and Claude Code
2.1.30x then validates `set_model` against its catalog (`Mt()` in the binary
returns `recognized` for every id only for non-first-party providers). The
launcher adds the Codex model through `ANTHROPIC_CUSTOM_MODEL_OPTION`
(default `gpt-5.6-sol`); with it, `set_model gpt-5.6-sol` succeeded mid-session
and the next reply came from GPT.

## Protocol reference (captured 2026-10-23, Claude Code 2.1.304)

Method: a throwaway CLI with an official Remote Control session ran behind a
logging mitmproxy (every request, response, SSE chunk, secrets redacted), while a
second Claude session drove the claude.ai tab for the same session through
Claude in Chrome and recorded the web client's requests. The browser tool shows
URLs but not bodies; the bodies below are what arrived at the CLI unchanged.

### CLI ⇄ Anthropic bridge (what HQ emulates)

| Call | Body / reply |
|---|---|
| `POST /v1/code/sessions` | `{title, bridge:{}, tags:["remote-control-repl"], config:{cwd, model}}` → `{session:{id:"cse_…", session_url, status, …}}` |
| `POST /v1/code/sessions/{id}/bridge` | `{}` → `{worker_jwt, worker_epoch, expires_in:46800, api_base_url}` |
| `GET/PUT /v1/code/sessions/{id}/worker` | PUT `{worker_epoch, worker_status?, external_metadata}`; metadata keys seen: `permission_mode`, `model`, `effort_level`, `is_ultracode_active`, `context_usage{used_tokens,max_tokens}`, `cross_session_inbound`, `rate_limit_info`, `pending_action(s)`, `task_summary` |
| `POST /v1/code/sessions/{id}/worker/events` | `{worker_epoch, events:[{payload}]}` → `{has_subscribers, results:[{event_id, sequence_num, duplicate}]}` |
| `GET /v1/code/sessions/{id}/worker/events/stream` | SSE of `client_event` frames, `:keepalive` comments |
| `POST /v1/code/sessions/{id}/worker/heartbeat` | `{session_id, worker_epoch, supports_heartbeat_probe, current_interval_seconds, idle_seconds}` → `{heartbeat_interval_seconds:20}` |

### What the web client sends down the stream

| Web action | Payload the CLI receives | Reply |
|---|---|---|
| open session | `control_request {subtype:"initialize"}` (sent twice) | `commands`, `agents`, `models:[]`, `current_model`, `current_permission_mode`, pending requests |
| context meter | `control_request {subtype:"get_context_usage"}` after each change | per-category breakdown, `totalTokens`, `maxTokens`, `percentage` |
| send | `user` with `client_platform:"web_claude_ai"`, `server_received_wall_ms`, `trace_context` | normal turn |
| model picker | `control_request {subtype:"set_model", model}` | `success` |
| mode | `control_request {subtype:"set_permission_mode", mode}` | `success` |
| approve | `control_response {behavior:"allow", toolUseID, tool_name, updatedInput}` | |
| question | same, `updatedInput.answers {question: label}` | |
| stop | `control_request {subtype:"interrupt"}` | `success` |
| (session start) | a `servers` control request injecting claude.ai's own MCP server | rejected: "Server-injected MCP servers are disabled" |

`initialize` returns no models even to claude.ai; the web picker comes from its
own `/api/organizations/{org}/model_selector/cc`. `can_use_tool` requests carry
`display_name`, `description`, `tool_use_id` and `permission_suggestions`.

### Web client ⇄ claude.ai

It reads one long-lived `GET /v1/sessions/{session_…}/events?after_id=…`, polls
`GET /v1/code/sessions/{session_…}` many times a minute, writes everything as
`POST /v1/code/sessions/{cse_…}/events`, and keeps the sidebar live with
`GET /v1/code/sessions/watch` plus `client/presence` and `heartbeat_check`.

## Parity with claude.ai

| claude.ai | HQ |
|---|---|
| model picker, mode picker (Auto, Manual, Accept edits, Plan) | same requests; same labels plus Don't ask and Bypass |
| approval card: Deny / Always allow / Allow once | same three, from Claude's own suggestions |
| question card with Other / Skip | structured answers (existing) |
| tool calls as summaries with output | cards with input and output |
| streaming text | 1 s polling of the live delta |
| context % and weekly-usage banner | context used/window and 5h/7d in the bar |
| effort submenu, Ultracode, fast mode | shown when the worker reports effort; not settable |
| server-generated session titles, rename, fork, share, Changes/Files panes, attachments, dictation, queued messages | not implemented |

The official web interrupt stalled on "Stopping" for over a minute in this
capture; HQ's interrupt returned the session to idle in about two seconds.

## Router side (claude-code-with-codex)

`--serve` now accepts `--remote-control-backend`, so every `--via` client's native
session reaches the hub. Its routing map persists in the proxy state dir;
without that a proxy restart sent live sessions' HQ worker tokens to Anthropic.
Folder trust accepted inside a launcher profile survives the next launch.

## Verification

- 289 HQ tests, 46 router tests; `node --check` on `app.js`.
- Real browser (Playwright, chromium-1248) against the dev HQ and then prod, on a
  session started by a real `tcc`: Bash tool card, Sonnet → GPT switch with the
  next answer "GPT", Plan and Bypass modes, interrupt (working → idle in 2.2 s).
  The CLI's own status line showed `gpt-5.6-sol` and bypass permissions.

## Deployment

ftower hub: release `b81b62fb…` (0.6.0), unit backup
`cc-tmux-hq.service.before-native-ui-20261023`; earlier releases kept for
rollback. Dependencies were installed with `uv pip`, not hash-locked as before.
The dev node runs the same version. The ftower account proxy runs the updated
launcher and addon (`account_remote_route.py.bak-20261023` beside it).
