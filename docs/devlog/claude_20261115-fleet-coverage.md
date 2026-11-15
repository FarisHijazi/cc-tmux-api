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
