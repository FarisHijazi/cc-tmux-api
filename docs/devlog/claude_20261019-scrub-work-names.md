# Work names scrubbed from history

The repo is public, so its full history was rewritten (`git filter-repo
--replace-text`, case-preserving whole words) to replace the two employer names
with `work-a` / `work-b`, and the work commit email with the personal one.
Trees are otherwise unchanged; local branches/worktrees were repointed with
`git reset --keep`.

`tests/browser/toy_actions.mjs` ssh'd to a host alias that became `work-a`; it
now reads `CC_REMOTE_HOST` (default `work-a`). The other browser fixtures are
recorded one-off identities and were left as rewritten.
