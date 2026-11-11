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
The `cc-tmux-mcp` MCP server (`src/cc_tmux_api/mcp_server.py`, `[mcp]` extra, mcp SDK 2.x `MCPServer`) is a thin
HTTP client of an HQ; keep fleet logic in the HQ, never in the MCP. Deployment: `docs/devlog/claude_20261111-mcp-and-hub-fix.md`.
`cc-rc-watchdog` (`src/cc_tmux_api/rc_watchdog.py`) detects from session files + HQ health, never from screen text;
it types only into idle sessions with an empty prompt. Same devlog.
