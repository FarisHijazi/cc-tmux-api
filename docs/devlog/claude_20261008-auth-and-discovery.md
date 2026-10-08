# Public authentication and missing-session investigation

The user requested a browser login popup at the public hostname and reported
missing VM sessions. This supersedes the no-login state in the
[earlier deployment log](claude_20261008-ftower-deployment.md).

## Public gate

Extended the existing Traefik hostname router with its built-in Basic Auth
middleware, realm `HQ`, a bcrypt `usersFile`, and `removeHeader: true`. The same
gate protects every path, including APIs and static assets. The existing mounted
configuration directory and file-provider reload were reused; no shared proxy
or application restart was required. Private HQ-to-HQ traffic remains token-free.

The password is stored privately on the operator laptop, never in this repository.
Remote bcrypt files are mode 600 and git-excluded. The service's homelab
`compose/cchq/README.md` and `AGENTS.md` document configuration and rotation.

Real public tests: unauthenticated `/`, `/api/auth`, `/api/sessions`, `/api/push`,
and `/sw.js` all returned 401 with a Basic challenge; incorrect credentials also
returned 401. Authenticated Chrome displayed actual Work-B, Work-A, MacBook and
ftower groups, with all five hosts reachable and enabled live-session composers.
No terminal input was sent. Evidence:
`/private/tmp/cchq-ftower-deploy/auth-browser-report.json` and
`authenticated-phone.png`. The first harness run used an incorrect exact-text
footer selector; using the actual footer element passed.

## Discovery investigation

The public API and fresh UI already included Work-B and Work-A. Sahalat was
reachable but incorrectly returned zero sessions. Its `aqar-reverse-1` tmux pane
reported `bash`, while an actual Claude child shared that terminal's foreground
process group and showed a live Claude prompt. `target_process` rejected all
shell-labelled panes before checking the process tree.

Plan: allow this case only when the unique, ancestry-verified Claude process is
positively foreground and not stopped/dead. Keep shell prompts, background
Claude children, ambiguous ownership, and nested-agent routing protected.
Version 0.4.4 implements this foreground check and rejects stopped/dead candidates.
Eleven added regression cases cover foreground/background shell children,
stopped/zombie/dead processes, unrelated terminals/ancestry, ambiguous siblings,
and nested foreground children. All 238 tests and Ruff passed (excluding the
unchanged vendored module).

## Deployment and final verification

Only the affected Sahalat owner was upgraded to wheel SHA256
`21f4d7f69631edb039509ea84c6a5303f3bb8ac0c6181ddd0a9ecb0740243172`.
Its `cc-tmux-api.service` used PID 2048575, zero restarts, on both existing :8377
listeners. Runtime configuration remained byte-identical. Deployment evidence is
`/private/tmp/hq-sahalat-044-upgrade/README.md`. The ftower aggregator remains on
0.4.3; no aggregator upgrade is needed to consume the corrected peer discovery.

Authenticated public Chrome then showed all five groups: Work-B five sessions,
Work-A four, MacBook three, ftower one, and Sahalat one (14 total, including
ended records). Actual detail reads and enabled composers passed for live Mac,
Work-B, Work-A and Sahalat sessions. No inputs were sent to any user session.
Anonymous and wrong-password requests remained blocked. The final browser
report at the evidence path above records these counts and zero page errors.

Sahalat's live session remains `identity_missing` for transcript history. Its
current Claude PID has neither an explicit session UUID argument nor an open
UUID-named JSONL descriptor; there are no cc-notify route/tab files, and exact-pane
hook events predate this process's start. No stale event, newest transcript, or
cwd-only guess was substituted. The terminal and guarded controls are available;
conversation history is not. This limit was explicitly reported to the user.
