# Native session capacity leak (2026-11-26)

Symptom: ftower HQ answered every `POST /v1/code/sessions` with 503 "Native session
capacity reached". All 1000 rows counted toward the cap, archived or not, and only
archived rows older than 30 days were ever pruned.

Root cause: routine workers (`routines.tick` / `workers.reap_stale` on the routine
worker host) end each run with `tmux kill-session`. The CLI then archives its Remote Control
session only some of the time: `hq-reply-radar-1` (every 2h) left 71 of 352 sessions
unarchived, and the rest sat as archived rows for 30 days. A live check on that host
showed no worker-side fix: `/exit` archived 0 of 7 sessions and `kill-session` 3 of 9.
So the HQ owns the fix.

Fix (`native_remote.py`): `_prune` archives sessions silent for `ABANDONED_SECONDS`
(7 days), so SessionHistory records them as ended; create counts only unarchived
sessions toward `MAX_SESSIONS` and evicts the oldest archived rows to stay under it.
SessionHistory keeps its own 30-day summaries in a separate database, unchanged.
