# Persistent homelab HQ deployment

## Plan and ownership

The user requested moving the aggregation entry point to `ftower`, publishing
`https://cchq.fhijazi.com`, and retaining the requested no-pairing runtime. Reuse
the existing homelab Cloudflare wildcard tunnel and Traefik file provider. Run
the same HQ package as a persistent user service, with direct peers for the Mac,
Sahalat, Work-A, and Work-B, plus local ftower sessions. Make the Mac peer local-only
once the central aggregator works, preserving its existing state.

The homelab repository owns service/proxy configuration; this repository owns
application behavior. No new tunnel, Docker restart, or shared proxy restart is
needed. See [README configuration](../../README.md#configuration-env-vars) and the
[prior deployment handoff](claude_20261008-federated-hq-lifecycle.md).

## Application change

Version 0.4.3 lets a valid `CC_TMUX_PUBLIC_URL` coexist with an empty token.
`security.py` still enforces allowed hosts, same-origin API access, JSON mutation
content types, and no-store API responses. Session mutations still use the
existing owner/revision/request-ID path. Optional token authentication remains
available. HTTPS alone supplies no access restriction in the requested runtime.

Regression tests cover same-origin anonymous reads/actions and rejected foreign
hosts/origins, cross-site requests, malformed origins, and non-JSON mutations.
The existing browser harness accepts one explicitly supplied HTTPS deployment
origin; it continues rejecting credentials, paths, queries, and fragments.

## Validation so far

- 227 Python tests passed; Ruff passed excluding the unchanged vendored module.
- Wheel `cc_tmux_api-0.4.3-py3-none-any.whl` SHA256:
  `521d2afae5ccde045e9bc1b854960f75b89be80ee95eaef79bc99d6c0f72c582`.
- From ftower: Mac, Work-A, Work-B LAN, and Sahalat HQ APIs responded HTTP 200.
  Work-B's Tailscale address timed out; the verified existing LAN route is used.
- ftower already has Python 3.13.5, uv 0.11.9, user systemd running, and linger
  enabled. Its port 8389 was unused before deployment.
- The requested hostname already resolves through the existing wildcard tunnel
  and returned Traefik's 404 before adding an HQ route.

## Live deployment

- Persistent unit: `/home/faris/.config/systemd/user/cc-tmux-hq.service`, linked
  to `/home/faris/media-server/compose/cchq/cc-tmux-hq.service`; enabled and active.
  Its executable belongs to the hash-named 0.4.3 release. PID 1960394 was verified
  listening on `127.0.0.1:8389` and `172.17.0.1:8389`.
- Runtime configuration: `/home/faris/media-server/compose/cchq/hq.json`.
  Node `hq-ftower`, worker management disabled, separate persistent state at
  `/home/faris/.local/state/cc-tmux-api/ftower`. Existing linger was retained.
- Existing wildcard DNS/tunnel needed no changes. The new hostname-specific
  Traefik file is `/home/faris/media-server/provision/traefik/config/cchq.yml`;
  it routes to `http://host.docker.internal:8389` and reloaded without a restart.
- `https://cchq.fhijazi.com/api/auth` returned HTTP 200 and
  `{required:false,authenticated:true}`. Cloudflare returned `DYNAMIC`, and API
  responses carry `Cache-Control: no-store`.
- The Mac's existing `~/.config/cc-tmux-api/hq.json` now lists local sessions only.
  Backup: `hq.before-ftower-20261008.json` beside it. Its generic launchagent was
  restarted; history/push databases and legacy domain service/SSH forward were
  preserved. VM HQ packages remain 0.4.2; the compatible public-origin change is
  needed only on the new 0.4.3 aggregator.
- The final private/root census found 12 unique session origins, 10 alive and
  two ended, across five available machines. Sahalat is available with no Claude
  sessions. Counts describe the observation, not a fixed inventory.
- Push configuration reports available with a generated persistent VAPID key.
  Physical iPhone notification delivery has not been tested.
- Public guard checks returned 403 for a foreign Origin and 415 for a non-JSON
  mutation aimed at an invalid session ID; neither test sent input to a pane.

## Public browser and action verification

Real Chrome used the published HTTPS origin, with iPhone 390px and desktop
1440px viewports. Actual session APIs supplied all data; no mocked responses or
inputs to user-owned sessions were used.

- All 12 session details and owner routes passed. Host/directory grouping,
  search/recovery, conversation/terminal views, viewport bounds, and offline
  recovery passed. The footer showed five connected machines.
- An early Mac detail request returned a transient 503. Subsequent complete
  iPhone/desktop detail checks succeeded, and an independent ftower census
  confirmed all peers available with zero service restarts. No persistent cause
  was established; this is not a claim that network outages cannot recur.
- The first attention-count assertion compared an old census while the separate
  toy test opened a menu. A focused phone retest compared the current UI response
  and passed with three attention rows. Ended details removed the composer and
  controls. No product change was needed for that test race.
- The reserved Work-A toy completed a fresh nine-action batch through ftower:
  single-select, multiselect, custom text, current-only review, and submit. Native
  terminal output verified the actual chosen answers and fresh completion.
- HTTPS was a secure context, the origin's `/sw.js` registered and activated,
  and browser push/notification APIs and server VAPID configuration were
  available. An isolated desktop subscription attempt was denied permission;
  no subscription was created. Neither desktop delivery nor physical iPhone
  installation/notification delivery is claimed.

Evidence under `/private/tmp/cc-mobile-browser/evidence/`:
`2026-10-08T20-52-24.024Z/report.json` (complete detail/layout checks and noted
attention race), `https-phone-recheck-2026-10-08T20-53-42.726Z/report.json`
(focused passing phone/ended checks), `toy-2026-10-08T20-52-12.732Z/report.json`
(native-verified actions), and `https-push-2026-10-08T20-51-56.501Z/report.json`
(secure-context/push capability, delivery limitation). Screenshots are beside
their reports. The published URL was opened in the user's Chrome.
