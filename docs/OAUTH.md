# OAuth 2.1 for ChatGPT (RigorCore Dev)

## State and threat model

The MCP resource server supports two **mutually exclusive** modes:
- `bearer` (default): existing static `RIGORCORE_MCP_TOKEN` for backward compatibility.
- `oauth`: validates **RS256 OAuth access tokens** from an established identity
  provider, restricted to **one expected owner subject** and a required scope.
  The legacy static MCP token is **not accepted in oauth mode**.

The agent WebSocket still uses its distinct `RIGORCORE_AGENT_TOKEN` in either mode.
The control plane allows writing source files **without local approval** and shell
commands can escape their working directory. Any permitted OAuth token therefore
grants powerful development-system access: use only low-privilege isolated agents
and test workspaces; do not expose untrusted accounts. There is no per-tool scope
segmentation yet (all tools require the same `rigorcore:access` permission).

**Do not switch on OAuth until an external provider and a single owner identity
are configured and verified. Do not disable authentication to get ChatGPT working.**

## Identity provider (Auth0 or other compliant OAuth 2.1/OIDC provider)

Use an existing provider rather than implementing an authorization server.
The provider must:
1. Expose OAuth 2.0 Authorization Server Metadata (RFC 8414) or OIDC discovery,
   including authorization and token endpoints and `code_challenge_methods_supported: ["S256"]`.
2. Support a client registration path ChatGPT can use: **CIMD**, DCR, or a
   pre-registered public OAuth client. ChatGPT favors CIMD and can use the
   token auth method `none` for PKCE-protected public clients.
3. Issue **RS256 signed** access tokens with `iss`, `aud`, `sub`, `iat`,
   `exp`, plus `scope: "rigorcore:access"` or `permissions:
   ["rigorcore:access"]`. The token audience must match the configured
   MCP resource identifier exactly.
4. Support `resource` in authorization and token requests, targeting the MCP
   resource identifier. If your provider does not echo/associate it with the
   access token audience, use a provider feature/API configuration that does.
5. Allow only the owner's account to authorize access to this private API.
   Copy the actual **access-token `sub`** of that account into
   `RIGORCORE_OAUTH_OWNER_SUB` (not an email or arbitrary username).
6. Support refresh tokens or a renewal method if you need long-lived ChatGPT
   sessions; configure `offline_access` where required.
7. Register the **exact ChatGPT OAuth redirect URI** shown by its management
   screen. For issuers advertising RFC 9207 issuer support correctly, the stable
   callback can be `https://chatgpt.com/connector_platform_oauth_redirect`;
   otherwise the callback ID may vary. Never use wildcard callbacks.

See the [OpenAI authentication guide](https://developers.openai.com/plugins/build/auth)
and [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization).

## VPS server settings

Continue using Nginx on ports 80/443, proxy to the FastAPI service at
`127.0.0.1:8765`. **Never** expose port 8765 to the Internet.
Keep the existing separate agent secret, but **do not** save provider keys,
client secrets, or passwords in GitHub.

In `/etc/rigorcore-dev/server.env` (root-readable only):

```ini
RIGORCORE_MCP_AUTH_MODE=oauth
RIGORCORE_PUBLIC_HOST=mcp.rigorcore.com
RIGORCORE_OAUTH_ISSUER=https://YOUR-TENANT.REGION.auth0.com/
RIGORCORE_OAUTH_AUDIENCE=https://mcp.rigorcore.com/mcp/
RIGORCORE_OAUTH_OWNER_SUB=auth0|YOUR-EXACT-OWNER-SUBJECT
RIGORCORE_OAUTH_SCOPE=rigorcore:access
RIGORCORE_AGENT_TOKEN=YOUR-EXISTING-AGENT-SECRET
# Optional if your provider's JWKS URL is not issuer + /.well-known/jwks.json:
# RIGORCORE_OAUTH_JWKS_URL=https://YOUR-TENANT.REGION.auth0.com/.well-known/jwks.json
```

The issuer must match JWT `iss` **including the trailing slash**. The
audience must match JWT `aud` byte-for-byte. The JWKS URL must be HTTPS on
the issuer's origin. Startup deliberately fails with incomplete settings.

A **staged migration** is recommended: first configure the provider and run
OAuth-mode tests on a separate development instance, then switch production
during a maintenance window. Restart and check:

```sh
sudo systemctl restart rigorcore-dev
sudo systemctl status rigorcore-dev --no-pager
curl -fsS https://mcp.rigorcore.com/.well-known/oauth-protected-resource
curl -i https://mcp.rigorcore.com/mcp/
```

The metadata GET must report the configured issuer/resource/scope. An
unauthenticated MCP request must return HTTP **401** with a
`WWW-Authenticate: Bearer resource_metadata="..."` header. Do not expose
tokens in curl commands, shell history, logs or ChatGPT conversations.

The authorization server's `/.well-known/oauth-authorization-server` or
`/.well-known/openid-configuration` must independently advertise PKCE S256,
client registration, and the correct OAuth endpoints. The MCP server does
**not** serve this metadata or perform authorization-code exchange itself.

## ChatGPT plugin after provider setup

Once OAuth works, change the private plugin's `mcp.json` auth declaration
from `api_key` to:

```json
{
  "type": "oauth",
  "client": {"mode": "cimd"},
  "resource": "https://mcp.rigorcore.com/mcp/",
  "baseScopes": ["rigorcore:access"]
}
```

Only select `cimd` if your provider supports validating OpenAI's client
metadata document; otherwise configure `dcr` or a registered `provided`
client as documented by the provider and OpenAI. Publishing the plugin
manifest alone does not authenticate users, deploy the VPS or prove that
the ChatGPT connection works. Test discovery, account authorization,
token refresh, tools/list, read and deliberate write/command denial.

## Rollback

To return to the previous authentication method, set
`RIGORCORE_MCP_AUTH_MODE=bearer`, preserve or restore
`RIGORCORE_MCP_TOKEN`, restart the service and revert the plugin's
OAuth declaration. The agents' authentication remains unaffected.

## Security follow-ups

- Per-tool OAuth scopes (`read`, `write`, `shell`).
- Per-node agent credentials, enrollment and revocation.
- Separate approval/authorization for `write_files`.
- Sandboxed low-privilege shell execution.
- Automated end-to-end ChatGPT OAuth tests with the actual identity provider.


## ChatGPT account linking and public tool discovery (October 2026)

The plugin now uses **mixed OAuth** discovery: \`initialize\` and \`tools/list\`
can run without credentials so ChatGPT discovers the OAuth2 security requirements
and the profile tool. **Every tools/call and all private operations still require**
an RS256-signed access token for the pinned owner and \`rigorcore:access\` scope.

An unauthenticated \`tools/call\` responds with the MCP
\`_meta["mcp/www_authenticate"]\` challenge to prompt ChatGPT's OAuth login.
An unauthenticated GET /mcp/ still returns HTTP 401 with the matching
WWW-Authenticate header. Missing or invalid tokens can never invoke any device
tool. The only public information is tool names, input schemas, and descriptions.

The optional \`get_profile\` tool is read-only and returns a stable opaque ID
derived from the **validated token's** issuer and subject; it does not expose
user emails or secrets. It includes \`_meta["openai/profile"]\` and output schema.
Both per-tool \`securitySchemes\` and the backward-compatible
\`_meta.securitySchemes\` mirror are returned on \`tools/list\`.

The plugin manifest at \`mcp.json\` must use \`"type": "mixed"\` with the existing
CIMD client declaration, scope and resource, not \`"type": "none"\`. This is
*not* anonymous API access: OAuth remains mandatory for every tool call.

After deploying, use these safe, token-free checks:

\`\`\`bash
curl -fsS https://mcp.rigorcore.com/.well-known/oauth-protected-resource
curl -i https://mcp.rigorcore.com/mcp/
curl -fsS -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' -X POST \\
  https://mcp.rigorcore.com/mcp/ \\
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}'
\`\`\`

The last POST should return JSON-RPC initialization, not 401.
List tools next with method \`tools/list\` and verify the presence of
\`securitySchemes\` and \`get_profile\`; do not call write or shell tools merely
to test OAuth. The only login that can grant access is the pinned Auth0 owner.
If the ChatGPT connection UI still does not appear, inspect CIMD compatibility
and the provider's OAuth metadata with an MCP Inspector client before changing
the server's authentication again.

**Rollout:** Pull the tested commit on the VPS, reinstall \`pip install -e .\`,
restart the service, check both read-only probes and logs. On unexpected behavior
revert the server to the previous tested Git commit and restart. Retain OAuth mode
and secrets during rollback unless intentionally migrating both server and plugin.

## Legacy-protocol discovery probe (October 2026)

ChatGPT may first send `server/discover` (MCP revision `2026-07-28`). This
server deliberately uses MCP Python SDK v1 (`mcp<2`) and **does not** implement
the modern transport. The gateway therefore answers the modern probe with
HTTP **400**, JSON-RPC `-32601`, without an OAuth challenge. Compatible clients
can then fall back to the supported legacy `initialize` / `tools/list`
handshake. Returning **401** to that version probe would incorrectly treat
protocol negotiation as authentication failure.

Unknown and malformed requests still fail closed. The diagnostic flag
`RIGORCORE_MCP_TRACE_REJECTIONS=1` now records only method names or sanitized
body categories (e.g. `json-batch[2]` or `invalid-or-null-json`) without
recording JWTs, arguments, file contents or raw requests.

This is a compatibility correction, **not** native support for MCP 2026-07-28.
If the ChatGPT registration still fails, inspect the new rejection categories
before changing OAuth again. Do not return fake `server/discover` capabilities
or allow anonymous `tools/call`.
