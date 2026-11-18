# Fleet MCP through cc-proxy

Extended the existing adapter with node health, native question responses, observed revisions for tmux
option selection, and action receipts. Uncertain transport failures surface an explicit no-retry error.
OIDC tokens require exp/sub/iss/aud; metadata advertises email and offline access scopes. Existing static
tokens remain supported for authorized machine clients. Fleet discovery and terminal writes stay in HQ.

Validation: the combined branch passed all 415 tests, including 30 MCP tests. Those automated
tests do not write to real sessions.

The existing ftower `cc-tmux-mcp.service` was upgraded in place to wheel SHA256
`66e5fd1de4ef926b47cab9f80437c8d9476f6037bc88afa97cdbf408d31f905d`, installed under the existing
hash-named releases directory with locked dependencies. Its canonical resource origin is
`https://cc-proxy.fhijazi.com`. Previous unit: `cc-tmux-mcp.service.before-cc-proxy-20261115` beside
the active user unit. Restore that unit, daemon-reload and restart only this adapter for rollback.
cc-proxy owns the relay and public route, documented in its corresponding devlog.

Claude.ai fell back to same-origin authorization discovery when reconnecting its existing CCHQ
connector. The root authorization-server metadata route now redirects to the fixed Authentik OIDC
metadata URL; cc-proxy relays its Location header only on this discovery route. The existing connector
completed Authentik sign-in and displayed Connected. No extra connector was created.

Live public SDK validation through cc-proxy initialized all eight tools, listed configured nodes and
read a session. An isolated tcc/claude-accounts toy received a prompt and returned the requested
assistant marker through the same opaque tmux session identity. Anonymous requests remain 401;
the dashboard and non-MCP paths remain behind browser SSO. Final targeted suites: 30 passed.

The real Claude.ai connector subsequently listed nodes and sessions, read the isolated toy, sent
one prompt, and read the exact requested assistant reply in the original session. The exchange
returned to idle; no other session was written.

Public MCP delivery was also verified during a streaming response: a message queued successfully,
the semantic interrupt produced an interruption record, and the queued message received its exact
assistant marker. The original process and opaque session identity survived. The leaf probe changes
and final deployed release hashes are recorded in `claude_20261115-fleet-coverage.md`.
