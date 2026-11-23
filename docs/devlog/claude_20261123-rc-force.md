# 2026-11-23: forced Remote Control pass, double /rc, menus, names

All measured on dev, Claude Code 2.1.329, on throwaway tmux sessions only.

**Double `/rc`.** Typing `/rc` autocompletes to `/remote-control (rc)`; in a narrow pane the row is truncated to
`❯ …emote-control (rc)`, which the completion check must accept.
- Disconnected (`bridgeSessionId` null): the first `/rc` + Enter connects within ~3 s (new id in the session file);
  the second opens the Remote Control dialog (Disconnect this session / Show QR code / `❯ Continue`); Enter keeps it.
- Already connected: each `/rc` opens the same dialog with Continue highlighted; Enter closes it and the bridge id is
  unchanged. Letters typed while the dialog is open are ignored, but the watchdog never relies on that: it answers
  each dialog before the next `/rc`.

**Screen gates.** The prompt line is `❯\xa0`; a fresh session shows a placeholder in dim (`ESC[2m`), so the capture keeps
styling (`capture-pane -e`) and drops dim text before deciding "empty". An AskUserQuestion menu sets the session file
`status` to `waiting`, replaces the prompt box and ends with `Enter to select · ↑/↓ to navigate · Esc to cancel`.
Escape on it prints "Declined to answer Claude's questions", the prompt comes back empty, and after the re-ask message
Claude showed the same menu again (`--close-menus`, live). A draft (`❯ owner draft do not touch`) was left intact.
An in-use pane: an attached client's `#{client_activity}` is its last input (attach counts); a read-only client
attached 3 s earlier made the pane skip as in use, `--force` overrode it.

**Names.** A bridge's name is fixed when it registers (`POST /v1/code/sessions`, the hub then mirrors that title
upstream once in `rc_mirror._register`; there is no title forwarding after that):
- untitled session: `<prefix>-<adjective>-<noun>`, prefix from `--remote-control-session-name-prefix`
  (`dev-rcb-wild-thompson`), default the hostname (`dev-cozy-riddle`);
- a session that already has a title registers with it (`AskUserQuestion A or B prompt` after a reconnect);
- `/rename rc-name-test-1` changed the CLI's own name and prompt border, but the live bridge kept its old title for
  over a minute; the next reconnect registered `rc-name-test-1`. Same for an auto title after the first message.
So `--renew-names` (Disconnect + double `/rc` on healthy sessions) is the rename path. Each renew issues a new bridge
id, so the app shows a new session entry and the old one ends. A non-tmux rename would need the hub to forward the
CLI's `PATCH /v1/code/sessions/{id}` title upstream; nothing calls it today, and whether claude.ai accepts a title PATCH
was not tested.
