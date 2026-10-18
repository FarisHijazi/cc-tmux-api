# Native computer-use consent through HQ

## Plan and ownership

Extend the existing [native transport](../../src/cc_tmux_api/native_remote.py)
and [consent UI](../../src/cc_tmux_api/static/app.js), preserving fleet action
guards and session history. Do not introduce a desktop controller, another
permission store, or a second proxy. First verify the current native protocol,
then implement app/flag consent, test it in real Chrome with a protocol peer,
and verify initialization against an actual Claude process. The original
[native transport record](claude_20261018-native-remote-control.md) owns launch,
authentication, federation and deployment background.

## Current protocol versus old source

The user-requested [older source snapshot](https://github.com/yasasbanukaofficial/claude-code/tree/a371abbe75ffa0d0a3c92290e2bbf56a7ef54367/src/utils/computerUse)
uses a local JSX permission dialog. Installed Claude Code 2.1.302 additionally
supports native Remote Control dialog transport; the older code alone would
miss this capability. The protocol schema was checked against the installed
binary and then its capability handshake was exercised with a real CLI.

HQ sends `control_request` with subtype `initialize` and
`supportedDialogKinds: [computer_use_approval]`, once per worker epoch. Only
[server startup](../../src/cc_tmux_api/server.py), whose UI implements this kind,
activates the declaration. The CLI may restore `pending_user_dialog_requests`
and pending tool permissions in its initialization reply. HQ accepts restoration
only for its own initialization request, deduplicates it and validates the whole
batch before mutation.

`request_user_dialog` carries `dialog_kind: computer_use_approval` and a payload
with requested apps, resolved bundle/display names, existing grants, proposed
tiers, requested flags and optional OS-permission state. HQ displays new grants
unchecked. Previously approved apps stay visibly approved and cannot be revoked
by this dialog. The backend constructs grants from the exact pending request,
preserves read/click/full tiers and rejects unknown apps or unrequested flags.
Clipboard read, clipboard write and system-key combinations are separate choices.

Replies use the existing correlated success envelope with
`{behavior: completed, result: {granted, denied, flags}}`, or
`{behavior: cancelled}`. In the current CLI, cancellation settles the dialog's
empty-grant default and dismisses its concurrent local prompt; it does not open
a fallback prompt or revoke previous grants. An OS-permission retry sends no
grants and lets native Claude recheck the host. HQ never edits macOS permissions.
Unknown dialog kinds are not advertised or approved.

## Validation and limits

The complete Python suite passes 283 tests, including 41 native protocol tests.
Ruff passed and the 0.5.1 wheel built successfully. Real Chrome frontend contracts
passed at 390 and 1440 pixels, including explicit selections, unavailable apps,
unrequested flags, failed action retention, cancellation and OS-permission retry.

An authenticated **simulated native peer**, using actual HTTP and SSE against
the real preview backend, drove three browser actions: selected app plus clipboard
read, cancellation, and a zero-grant OS-permission retry. All returned 200 and
the wire replies matched expectations; no browser errors occurred. This did
not execute desktop actions. Evidence is under the private runtime directory
`tmp/cchq-computer-use/preview/evidence/dialog-contract/`.

A fresh, unmodified interactive Claude Code 2.1.302 process used the existing
account launcher with a session-owned inference router. It accepted HQ's
initialization declaration, returned a correlated control response and marked
the event processed exactly once. Evidence is `real-cli-initialize.json` under
the same private preview evidence directory.

Actual computer control is **not verified**. The available full login is Team;
the second saved key has inference scope only and no subscription metadata.
The real interactive `/mcp` test did not expose `computer-use`.
[Anthropic's current requirements](https://code.claude.com/docs/en/computer-use)
restrict native CLI computer use to interactive macOS Pro/Max accounts, with
Accessibility and Screen Recording permission. A full eligible login is needed
before enabling the server and testing a harmless desktop app. No subscription,
feature, account or OS-permission gate was bypassed. Source Claude configuration
and credentials were left untouched.

The inference proxy already preserves supported base64 image tool results in
its existing Codex translator; 29 existing tool-result tests passed in the
proxy's locked Rust test suite. No new model backend was added. HQ's transcript
is still text-only: this change does not add screenshot viewing or a live desktop.
The accidental video-player requests were cancelled; no player or site changes
were made for them.
