# Fleet coverage verification, 2026-11-15

Two existing 0.4.2 domain services skipped Claude processes when tmux
reported their foreground command as a shell. Backported only `target_process`
from upstream commit `d8cf3fa`, preserving each domain wrapper and dependencies.
The old module failed two foreground regressions; both patched modules passed all
12 existing shell/ancestry discovery cases. No input was sent to user panes.

| Existing owner | Live tmux sessions before/after | Immutable prepared release |
| --- | --- | --- |
| First domain node | 0 / 2 | `dc0428fbeebe5850d294a75a1535a5c4d13a0aec22671b747cb34dc2c9fbe4ac` |
| Second domain node | 3 / 6 | `c87f67422bf7e499ad7e2f58f17922b3c46ee6c7868073dd5ef48dfd8841c215` |

Releases are under `~/.local/share/cc-tmux-api/releases/`; manifests there record
source paths, source hashes, units and deployment evidence. The same probe hash
on both is `c93f84917b53c22819456a04cfcc9635814966dd2e816f109f8a40cb60fcfa92`.
Each existing unit's `91-fleet-discovery.conf` changes only `ExecStart` and sets
`KillMode=process`. Original `90-shared-wheel.conf` environment files, working
directories, application args, state and registries remain in effect.

The first node's tmux server belonged to the service cgroup while its old kill mode was
`control-group`. The override was loaded and effective `KillMode=process` verified
before restarting that exact unit. All four and seven respective panes retained
their pane PID and tmux server identity. The units were active with no restart loop.
Rollback must restore the previous `ExecStart` in the 91 override while retaining
`KillMode=process`, reload the user manager, then restart only the owning unit.
Original releases are preserved. Do not remove the first node's override and restart with
the old kill mode: that would terminate the existing tmux server.

The aggregate subsequently reported domain nodes 2 and 6, dev 8 and Mac 1
live tmux sessions. Native Remote Control records can duplicate tmux sessions;
row totals are not distinct conversation counts. Ftower and Sahalat peers are
reachable but have no local Claude process or tmux server, so their local source
reports the missing socket. No duplicate service was introduced.

The second domain node's existing worklog reconciler reports an invalid YAML note containing an
unquoted colon; this is unrelated to discovery and was not changed.

## Spinner guard change

`fleet_probe` now carries exact and send-only screen hashes in its opaque revision.
Only `send` accepts known spinner glyph/elapsed-time changes above an empty prompt.
Menus, drafts, output, cursor position, copy mode, identity, stopped/background
processes and other actions remain guarded. The final foreground state is checked
again immediately before writing. Fleet tests pass 103/103; full suite 363/363.
This code validation does not establish deployment or live busy-session delivery.

## Spinner deployment

The three Python VM peers now use immutable copies of their preceding environments,
with only the spinner guard and the already-existing unreadable-plugin-path guard
backported into `fleet_probe.py`. Each exact module passed all 103 fleet regressions.
Their probe SHA-256 is
`ed8a082a559ef8f52a2c649fa609c0241e4c84259fcc4bd55095e523c985a17d`.

| Node | New immutable release |
| --- | --- |
| First domain node | `d03fc1e35869e93d46c6a4d9ac7723962e250652fa01d284369b013c7a0d88f4` |
| Second domain node | `a645907dee9fa1aaad18615ec003673b96413c19d4ac295c41798173d0aa4e17` |
| Empty VM peer | `8f4136a00f4841fee3bb441589faf0ec39a516ba003944082ba89c0a97606a05` |
| Mac | `84140531f120c40ed57ae4f40b0ac143130f048b80a10ab8266a559bb9e7fc81` |

VM units select these through `92-spinner-guard.conf`, preserving `KillMode=process`.
All eight domain-node interactive Claude PIDs survived unchanged. The empty peer
had no Claude or tmux processes. The Mac's same `com.local.cc-tmux-hq` LaunchAgent
now uses an immutable copy of its existing venv/package plus the same probe change.
Its prior plist is `launchagent-before.plist` in that release. A bootstrap attempted
immediately after bootout returned I/O error 5; repeating bootstrap after the old
job exited succeeded. One HQ process/listener remains, with the original tmux and
interactive Claude PIDs unchanged. All peer APIs were subsequently healthy.

The root aggregator was not restarted: all current tmux session actions route to
their peer owner, while root-native control has its separate revision protocol.

The dev node's Rust owner embedded the probe at compile time. Built an isolated
worktree at the exact existing Rust commit `ecc929f`, applying only the probe
change and existing unreadable-path guard. All 10 Rust unit tests and 103 probe
regressions passed before installation. The binary is
`~/.local/lib/cc-tmux-api-rust/cc-tmux-api-ecc929f-affea8a`, selected by the same
unit's `zz-fleet-probe.conf` with `KillMode=process`; its SHA-256 is
`49b282ffdc9096ed7677e6227912e8061e3621e1de7958d448c847b3b3230ea4`.
All nine preexisting panes and interactive Claude PIDs, including the test toy,
survived the restart. The unit was active with zero automatic restarts. Its probe
matches the upstream source bytes; Python backport hashes differ only in blank
lines between the three inserted helper functions.

## Streaming input follow-up

Live public MCP testing exposed the spinner-only limit: both sending and
interrupting failed with pre-delivery stale-screen conflicts during a 100-sentence
toy response. Captured consecutive screens showed text streaming without a spinner,
an unchanged empty input box/cursor, and a custom footer whose elapsed time ticked.

The revised guard records a separate working-input revision only while the existing
classifier/hook policy says working and the exact empty box contains the cursor.
Only `send` and `C-c` may compare that revision. Their input, foreground and copy-mode
checks run on both observations; the canonical prompt reader checks emptiness again
before writing. Menus, user drafts, idle/waiting transitions, changed identity and
other keys retain exact-screen guards. Definitive pre-write validation conflicts
now say `refresh before acting`, matching existing safe-retry clients.

Transcript messages additionally preserve source identity for existing clients to
reconcile growing messages. Row UUID takes precedence over provider message ID;
text list blocks use the original index as a suffix. ID-less rows retain their
previous text-only fallback, and hidden/tool content remains excluded. This is an
extension of `transcript_messages`, not a second history reader.
The probe also accepts the existing semantic `interrupt` action and translates
it to `C-c`; otherwise the MCP/ACP action would pass stale-screen checks only to
fail as unsupported. Both semantic interrupt and explicit `key=C-c` use the same
working-input guard.
