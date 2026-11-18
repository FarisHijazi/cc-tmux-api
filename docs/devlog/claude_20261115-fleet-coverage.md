# Fleet coverage verification, 2026-11-15

The existing two domain wrappers skipped shell-labelled Claude panes. Reusing
upstream `target_process` from `d8cf3fa` restored their live tmux coverage from
0→2 and 3→6. Its unique foreground ancestry checks still reject ambiguous,
background, stopped and nested candidates. No user pane received test input.

The final aggregator check found all five configured peers reachable, with 19 live
tmux sessions: domain nodes 2 and 7, Mac 1, dev 9 (including the isolated test
session). The additional domain session started during this work and was preserved.
The root and empty VM peer have no tmux server or local Claude process; their local
source reports a missing socket while the peer endpoint remains healthy. Native
Remote Control rows may represent the same conversations, so they must not be
added to tmux counts as distinct sessions.

## Control and transcript ownership

`fleet_probe` owns every terminal write. Its opaque revision carries an exact
screen hash plus an optional working-input hash. Only `send`, semantic `interrupt`
and explicit `key=C-c` can compare the latter, with the same process/pane identity,
foreground state, copy mode, cursor-in-input and canonical empty-input checks.
The existing decoration-aware prompt reader treats dim queue hints as empty;
literal human and slash-command drafts remain ineligible. Menus and other actions
retain exact-screen guards. Final foreground and empty-input validation runs again
before writing; definitive pre-write conflicts say `refresh before acting` so
clients can safely refresh without retrying uncertain delivery.

Hookless Claude sessions can stream without a spinner. The probe therefore reads
the existing publisher's exact `sessions/<pid>.json` through shared profile
discovery, requiring process start marker, PID domain, stable tmux window/pane and
timestamps newer than process birth. Linux PID domain uses `/etc/machine-id` and
the PID namespace, matching the installed CLI; it is not the kernel boot UUID.
Darwin converts the inventory birth time to the CLI's UTC, space-padded-day format,
including when the host uses a different timezone or the day is a single digit.
Publisher busy state never overrides a menu or waiting state and never supplies
conversation identity. This adds no scanner, poller or terminal writer.

The existing transcript reader now preserves row UUID, otherwise provider message
ID; text blocks suffix their original block index. This lets existing consumers
replace growing messages without merging distinct identical messages. ID-less
records keep their old fallback; hidden/tool content stays excluded.

## Active deployments

All four Linux peers run probe source through `4a62869`, SHA-256
`5e79694bab26a552600679e7cb4f43232951b387df1c1df60471ce30ad5e07c3`.
The Mac additionally runs the Darwin birth-format fix `e08dbf4`, probe SHA-256
`15f2749fa76a1702f7606a18d3b011e3d773c27d02fd61d8a69a1b7dcf224a27`.
The four Python peers use immutable copies of their preceding environments, with
the probe replaced. Wrappers, config, state, dependencies and listeners remain
under the same owners. Releases live in `~/.local/share/cc-tmux-api/releases/`.

| Existing owner | Active release |
| --- | --- |
| First domain node | `31a767ecef8874b1877dbb04a9251273025e7dc8502262af400d072ca5964760` |
| Second domain node | `5471121884aa2f1a828ea3e32108d4761c78fcfbe49181ae5e84bcfcf7d3e1e3` |
| Empty VM peer | `d974dcbc3e9a2008d86528d7793dde19b621aad8cfe77bdaac9d896340437cf2` |
| Mac LaunchAgent | `1ca134f48dede8fdee508b0207eaf19510ec5c8ff27b29df151832fbe8f636bc` |

The dev Rust owner embeds the probe. Its isolated build retains Rust commit
`ecc929f`, with only probe changes, and runs
`~/.local/lib/cc-tmux-api-rust/cc-tmux-api-ecc929f-4a62869`, binary SHA-256
`6fb0370a4d86ced38cc1f4c80061bae6b48dac9b5d428dbb96959e6394b3432e`.
The root aggregator was not restarted; its tmux actions route to peer owners.

Every deployment compared existing pane/server identities and interactive Claude
PIDs before and after. All survived. One domain unit originally owned the tmux
server in a `control-group` cgroup: `KillMode=process` was loaded and verified before
its first restart. All VM overrides retain that setting. Never roll it back by
removing the override and restarting under the old kill mode. Restore the preceding
`ExecStart` while keeping `KillMode=process`, reload the user manager, and restart
only that owner. Original releases and per-release manifests remain available.
The Mac release holds `launchagent-before.plist` for its same LaunchAgent owner.

## Validation

The final Python suite passes 415 tests (155 fleet cases); the dev Rust suite passes
10 tests. Coverage includes stale/reused PID and wrong-pane publisher records,
conflicting profiles, permission/menu priority, human/slash drafts, foreground
transitions, stable transcript IDs and semantic interrupt translation.

Only a new `tcc`/`claude-accounts` toy on dev received live input. Actual Claude.ai
OAuth produced `CLAUDE_AI_MCP_OK_20261115` in that existing session. The final public
MCP busy send and semantic interrupt both returned working-input receipts while
the publisher reported busy. The transcript then recorded `[Request interrupted
by user]`, followed by the queued message's assistant reply
`BUSY_QUEUED_OK_20261115`; the same original process returned idle. This confirms
interruption during generation, not merely delivery after completion. The toy is
kept alive for the coordinated T3 continuation test.
