# 2026-11-11: unattended workers keep Bash plain so safety hooks never stall them

**Problem.** Safety hooks block any shell command they cannot read with confidence. A blocked command waits for a
human approval, and in an unattended `hq-*` worker nobody is there to give it, so the worker stalls just as it
would on a question. The observed case: the stream on-call worker running inline `$VAR` / `$(...)` commands.

**Change.**
- `HEADLESS_SYSTEM_PROMPT` gains a "SAFETY HOOKS" paragraph:
  - one plain command per Bash call;
  - no inline `$VAR`, `${VAR}`, `$(...)` or backticks; hardcode the values, or write a script file and `bash` it;
  - no long `cd && ...` chains;
  - never `rm`, `kill` or `xargs` on a variable, glob or pattern.
- `HEADLESS_PROMPT_NOTE` (prepended to every task prompt) carries the one-line version.
- The `$(cat ...)` in the launch command is typed by the server, not the model, so it stays.

**Tests.** `uv run pytest -q tests`: 310 passed.

**Rollout.** It applies to workers spawned or revived after the running release picks up this commit.
