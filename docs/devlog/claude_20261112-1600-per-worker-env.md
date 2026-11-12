# 2026-11-12 16:00: per-worker env, kept across revive

**Why**: a long-running `/loop` watcher wanted `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=30` (compact at 30% of the window, not
near full). Nothing let one worker carry its own env, and `resume_registered()` after a reboot started every worker
with the server's env only.

**What**:
- `spawn(..., env={...})` and the API's spawn action (`"env": {...}`) take a worker's own env.
- Names must match `[A-Za-z_][A-Za-z0-9_]*`, checked before anything is created. A bad name means `ok: false`.
- It is passed as `tmux new-session -e K=V` and saved as `meta.json` `"env"`.
- `resume_registered()` reads it back, so the revived worker gets the same env. A hand-edited entry with a bad name is
  skipped.

**Tests**: `tests/worker_env_test.py` (spawn passes and saves it, no flag without env, a bad name creates nothing,
revive reapplies it). The full suite passed: 314.
