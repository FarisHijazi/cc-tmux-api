# Mobile control research

Prepared an isolated `research/mobile-control` worktree from `092fc54`.
The conclusions, sources, proposed ownership boundaries, and delivery gates are
in [mobile-control-research.md](../mobile-control-research.md).

Completed source review of cc-tmux-api, cc-notify, and the installed tcc function;
read-only SSH/tmux metadata checks on the requested laptop/VM fleet; upstream
comparison; and a source inspection of claude-wormhole at
`18e387c5b9e8b2cd1baa228663410e23ab555e4e`. No upstream installer was run.

An isolated native Claude toy verified single-select, multiselect, free text,
two-question batches, and cancellation over tmux. Its private captures and
transcript-availability snapshots are retained in
`/private/tmp/cc-mobile-research/`; the exact procedure is in
`/private/tmp/cc-mobile-research/docs/devlog/claude_20261008-1549-askuserquestion-research.md`.
These are deliberately plain paths, not `@` includes. They are local experimental
artifacts, not runtime dependencies of this repository.

No app implementation, service deployment, global hook update, or live-session
input was performed by this worktree. Actual iPhone push and native Claude browser
UI behavior remain unverified. Product preferences are pending in the conversation.
