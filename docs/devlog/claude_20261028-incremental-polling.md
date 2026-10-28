# Incremental session list and detail polling (2026-10-28)

Follows `docs/devlog/claude_20261028-webui-fix.md` on the same branch.

## Problem (measured on ftower, read-only GET)

- `GET /api/sessions` was 840 KB every 8 s: 573 rows, 532 of them ended.
- A working native session's detail was 0.6–1.05 MB (476–500 messages), polled
  every second. Of the remaining ~14.5 KB, 13 KB was `slash_commands`.

## Design

Every parameter is optional and the default response is unchanged, so peers
(`fleet._peer` never sends them), old clients and old servers keep working.

- `GET /api/sessions?ended=0` drops ended rows and adds `ended_count`.
  The UI asks for that list unless the filter is Ended or All sessions.
- `GET /api/sessions/{id}?after=<message id>` returns only the messages after that
  id, marked `messages_after`. An unknown id (pruned, or messages without `id`)
  returns the full list without the marker, which the client treats as a reset.
- `&commands=<digest>` omits `slash_commands` while the returned
  `slash_commands_digest` still matches.
- Native messages already had stable ids. Transcript (tmux) messages now carry the
  record `uuid` as `id` (`fleet_probe.transcript_messages`). Peers apply this only
  once they run this version, since each HQ probes its own hosts.
- The trimming is done in the HTTP route (`server.incremental_detail`), after
  `fleet.detail`, so it covers native, tmux, ended and peer-forwarded sessions alike.
  A hub still fetches a peer's full detail; only the browser response shrinks.
- Only native tool calls change after they are sent (their result arrives later),
  so the client's cursor is the message just before the first tool call that is
  not `done`, or the last message. The client replaces everything after the cursor
  with the delta. `useResource` reads the query at request time, so a moving cursor
  does not reset the resource.

## Measured payloads (local hub peered to production, 2026-10-28)

A local hub (new code, own state directory, `CC_TMUX_MANAGE_WORKERS=0`, ftower as
its only peer through an SSH forward, GET only) served the production data.
Forwarded IDs are longer, so its full list is larger than ftower's own 840 KB.

| Request | Before | After |
| --- | --- | --- |
| Session list every 8 s | 907 KB (573 rows) | 70 KB (41 rows, `ended_count` 532) |
| Working native detail, per 1 s poll | 0.59–1.06 MB | 1.6–2.2 KB average (max 8.7 KB when messages arrived) |
| Idle native detail with no messages | 14.2 KB | 1.6 KB (`slash_commands` omitted) |
| tmux detail | ~2.3 KB | unchanged (tail-dominated) |

After 15 polls, the merged message list matched a fresh full fetch for every
session checked. The real UI loaded against that hub (non-GET aborted) sent
`?ended=0`, one full detail, then `?after=…&commands=…` polls of 1.6–5.2 KB,
with no page errors.

## Tests

- `uv run pytest -q`: 316 passed (6 new: ended filter, cursor delta, unknown cursor,
  id-less messages, command digest, oversized cursor, transcript ids).
- `tests/frontend_layout_test.mjs` (its mock mirrors `server.py`): PASS at 390 and
  1440 px, including list requests, the cursor waiting before an open tool call,
  merge without duplicates, a finished tool shown as done, and the slash menu
  with omitted commands.
- `tests/frontend_test.mjs` (its mock ignores the new parameters, i.e. an old
  server): PASS at 390 and 1440 px.

## Deploy (not done)

`server.py` changes route signatures, so `cc-tmux-hq.service` must restart.
`fleet_probe.py` is read from disk on every probe and `static/` on every request,
so both take effect without a restart, but copy all three together.

```sh
R=$(systemctl --user show -p ExecStart cc-tmux-hq.service | grep -o '/home/[^ ]*/releases/[0-9a-f]*' | head -1)
P=$R/venv/lib/python3.12/site-packages/cc_tmux_api
for f in server.py fleet_probe.py static/app.js; do cp -p "$P/$f" "$P/$f.before-incremental"; done
# from the worktree:
scp src/cc_tmux_api/server.py src/cc_tmux_api/fleet_probe.py ftower:"$P/"
scp src/cc_tmux_api/static/app.js ftower:"$P/static/"
systemctl --user restart cc-tmux-hq.service
```

A restart (per README and the native/mirror devlogs): native SSE streams are cut
within the 5 s bounded drain, native sessions show unreachable until their CLI
reconnects, stored messages are kept, upstream mirrors show `disconnected` until
the next CLI call, process-local action receipts are lost, and tmux panes and
their fleet IDs are untouched. Rollback:
copy the `.before-incremental` files back and restart again.
