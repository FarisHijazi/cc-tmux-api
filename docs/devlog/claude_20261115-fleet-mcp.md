# Fleet MCP through cc-proxy

Extended the existing adapter with node health, native question responses, observed revisions for tmux
option selection, and action receipts. Uncertain transport failures surface an explicit no-retry error.
OIDC tokens require exp/sub/iss/aud; metadata advertises email and offline access scopes. Existing static
tokens remain supported for authorized machine clients. Fleet discovery and terminal writes stay in HQ.

Validation: 348 full-suite tests passed before the final empty-revision and OAuth metadata regressions;
the final targeted MCP suites passed 30 tests. No real session was written by those tests.

The existing ftower `cc-tmux-mcp.service` was upgraded in place to wheel SHA256
`6b53a2f9887cf6f38ce5663d3edf79604cfc82a976692cd2e9ce00c4a7a9a7a7`, installed under the existing
hash-named releases directory with locked dependencies. Its canonical resource origin is
`https://cc-proxy.fhijazi.com`. Previous unit: `cc-tmux-mcp.service.before-cc-proxy-20261115` beside
the active user unit. Restore that unit, daemon-reload and restart only this adapter for rollback.
cc-proxy owns the relay and public route, documented in its corresponding devlog.
