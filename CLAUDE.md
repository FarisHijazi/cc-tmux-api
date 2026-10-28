# HQ development

Read [AGENTS.md](AGENTS.md) for ownership, runtime boundaries and validation.
Native Remote Control protocol research and live evidence are recorded in
[the native transport devlog](docs/devlog/claude_20261018-native-remote-control.md).
These are deliberately plain links, not automatic includes.
For runtime launch/configuration, use [README.md](README.md#native-remote-control)
and preserve the existing production service and HTTPS owners.
Native launch keys are private runtime files; never place their values in docs.
Computer-use protocol support, deployed consent tests and account/OS limits are recorded
in [the computer-use devlog](docs/devlog/claude_20261018-native-computer-use.md).
The native-first UI and the Remote Control protocol as claude.ai uses it (captured
requests, parity table) are in `docs/devlog/claude_20261023-native-ui-and-rc-protocol.md`.
The claude.ai upstream mirror (design, protocol mapping, test coverage, what is not
verified live) is in `docs/devlog/claude_20261023-upstream-mirror.md`.
The 2026-10-28 web UI rework (live problems found, what changed, deploy steps) is in
`docs/devlog/claude_20261028-webui-fix.md`.
Incremental polling (`/api/sessions?ended=0`, detail `after`/`commands` cursors,
measured payloads, deploy and restart steps) is in
`docs/devlog/claude_20261028-incremental-polling.md`.
