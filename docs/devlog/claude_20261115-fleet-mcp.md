# Fleet MCP through cc-proxy

Extended the existing adapter with node health, native question responses, observed revisions for tmux
option selection, and action receipts. Uncertain transport failures surface an explicit no-retry error.
OIDC tokens require exp/sub/iss/aud; metadata advertises email and offline access scopes. Existing static
tokens remain supported for authorized machine clients. Fleet discovery and terminal writes stay in HQ.

Validation: 348 full-suite tests passed before the final empty-revision and OAuth metadata regressions;
the final targeted MCP suites passed 30 tests. No real session was written by those tests.

The existing ftower `cc-tmux-mcp.service` was upgraded in place to wheel SHA256
`1a6cf2ba11943bed5637788f8610846fd9a174ebdda8b1c559083cd445ff6533`, installed under the existing
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
