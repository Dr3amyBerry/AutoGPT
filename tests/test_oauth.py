"""OAuth resource-server tests: cryptographic verification and fail-closed authorization."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from rigorcore_dev.oauth import MCPBearerAuth, OAuthSettings, OAuthVerifier

ISSUER = "https://rigorcore-test.eu.auth0.com/"
AUDIENCE = "https://mcp.rigorcore.com/mcp/"
OWNER = "auth0|approved-owner"


@pytest.fixture()
def tokens(monkeypatch):
    settings = OAuthSettings(
        issuer=ISSUER, audience=AUDIENCE, jwks_url=ISSUER + ".well-known/jwks.json",
        owner_sub=OWNER, scope="rigorcore:access", public_host="mcp.rigorcore.com",
    )
    signer = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = OAuthVerifier(settings)
    monkeypatch.setattr(verifier.jwks_client, "get_signing_key_from_jwt",
                        lambda _: SimpleNamespace(key=signer.public_key()))

    def make(**changes):
        claims = {
            "iss": ISSUER, "aud": AUDIENCE, "sub": OWNER,
            "iat": int(time.time()), "exp": int(time.time()) + 300,
            "scope": "rigorcore:access",
        }
        claims.update(changes)
        return jwt.encode(claims, signer, algorithm="RS256", headers={"kid": "test"})
    return verifier, make


def test_valid_owner_access_token(tokens):
    verifier, make = tokens
    assert verifier.validate(make())
    assert verifier.validate(make(scope="", permissions=["rigorcore:access"]))


@pytest.mark.parametrize("override", [
    {"sub": "auth0|someone-else"},
    {"aud": "https://another.example/mcp/"},
    {"iss": "https://attacker.example/"},
    {"exp": 1},
    {"scope": "profile"},
    {"scope": "", "permissions": ["read:only"]},
])
def test_bad_tokens_are_rejected(tokens, override):
    verifier, make = tokens
    assert not verifier.validate(make(**override))


def test_malformed_or_unsigned_tokens_rejected(tokens):
    verifier, _ = tokens
    assert not verifier.validate("")
    assert not verifier.validate("not-a-jwt")
    assert not verifier.validate("a" * 20000)


def test_oauth_challenge_and_success(tokens):
    verifier, make = tokens
    inner = FastAPI()

    @inner.get("/")
    async def protected():
        return {"ok": True}

    with TestClient(MCPBearerAuth(inner, oauth=verifier)) as client:
        unauth = client.get("/")
        assert unauth.status_code == 401
        assert 'resource_metadata="https://mcp.rigorcore.com/.well-known/oauth-protected-resource"' in (
            unauth.headers["www-authenticate"])
        assert client.get("/", headers={"Authorization": "Bearer bad"}).status_code == 401
        assert client.get("/", headers={"Authorization": "Bearer " + make()}).json() == {"ok": True}


def test_legacy_mode_keeps_existing_behavior():
    inner = FastAPI()
    @inner.get("/")
    async def protected():
        return {"ok": True}
    with TestClient(MCPBearerAuth(inner, legacy_token="long-legacy-token")) as client:
        assert client.get("/").status_code == 401
        assert client.get("/", headers={"Authorization": "Bearer long-legacy-token"}).status_code == 200
        assert client.get("/", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_metadata_matches_oauth_resource(tokens):
    settings = tokens[0].settings
    assert settings.protected_resource_metadata() == {
        "resource": AUDIENCE,
        "authorization_servers": [ISSUER],
        "scopes_supported": ["rigorcore:access"],
    }


def test_settings_fail_closed(monkeypatch):
    monkeypatch.setenv("RIGORCORE_PUBLIC_HOST", "mcp.rigorcore.com")
    monkeypatch.setenv("RIGORCORE_OAUTH_ISSUER", ISSUER)
    monkeypatch.setenv("RIGORCORE_OAUTH_AUDIENCE", AUDIENCE)
    monkeypatch.delenv("RIGORCORE_OAUTH_OWNER_SUB", raising=False)
    with pytest.raises(RuntimeError, match="OWNER_SUB"):
        OAuthSettings.from_env()
    monkeypatch.setenv("RIGORCORE_OAUTH_OWNER_SUB", OWNER)
    assert OAuthSettings.from_env().owner_sub == OWNER
    monkeypatch.setenv("RIGORCORE_OAUTH_JWKS_URL", "http://localhost:1234/keys")
    with pytest.raises(RuntimeError, match="JWKS_URL"):
        OAuthSettings.from_env()

def _rpc(method, request_id=1, params=None):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_public_discovery_requires_token_for_tools(tokens):
    verifier, make = tokens

    downstream = FastAPI()

    @downstream.post("/")
    async def api(request: Request):
        msg = await request.json()
        if msg["method"] == "tools/list":
            return {"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                {"name": "list_nodes", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "write_files", "inputSchema": {"type": "object", "properties": {}}},
            ]}}
        return {"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}}

    with TestClient(MCPBearerAuth(downstream, oauth=verifier)) as client:
        init = client.post("/", json=_rpc("initialize"))
        assert init.status_code == 200

        listing = client.post("/", json=_rpc("tools/list", 2))
        assert listing.status_code == 200
        assert int(listing.headers["content-length"]) == len(listing.content)
        tools = {item["name"]: item for item in listing.json()["result"]["tools"]}
        assert tools.keys() == {"list_nodes", "write_files", "get_profile"}
        assert tools["list_nodes"]["securitySchemes"] == [
            {"type": "oauth2", "scopes": ["rigorcore:access"]}
        ]
        assert tools["list_nodes"]["annotations"]["readOnlyHint"] is True
        assert tools["write_files"]["annotations"]["destructiveHint"] is True
        assert tools["get_profile"]["_meta"]["openai/profile"] is True
        assert tools["get_profile"]["outputSchema"]["required"] == ["id"]

        call = _rpc("tools/call", 3, {"name": "list_nodes", "arguments": {}})
        unauthorized = client.post("/", json=call)
        assert unauthorized.status_code == 200
        challenge = unauthorized.json()["result"]
        assert challenge["isError"] is True
        assert "mcp/www_authenticate" in challenge["_meta"]
        assert "error_description=" in challenge["_meta"]["mcp/www_authenticate"][0]
        assert client.post("/", json=_rpc("resources/list")).json()["result"] == {"resources": []}
        assert client.get("/").status_code == 401

        allowed = client.post("/", json=call,
                              headers={"Authorization": "Bearer " + make()})
        assert allowed.status_code == 200
        assert allowed.json()["result"] == {"ok": True}


def test_profile_uses_verified_subject_and_never_token_id(tokens):
    verifier, make = tokens

    inner = FastAPI()

    @inner.post("/")
    async def should_not_reach(request: Request):
        raise AssertionError("Profile is handled by the OAuth gateway")

    with TestClient(MCPBearerAuth(inner, oauth=verifier)) as client:
        req = _rpc("tools/call", 3, {"name": "get_profile", "arguments": {}})
        headers = {"Authorization": "Bearer " + make()}
        first = client.post("/", json=req, headers=headers)
        second = client.post("/", json=req, headers=headers)
        assert first.status_code == 200
        profile = first.json()["result"]["structuredContent"]
        assert profile == second.json()["result"]["structuredContent"]
        assert profile["id"].startswith("rigorcore_")
        assert OWNER not in str(first.json())
        assert first.json()["result"]["content"][0]["text"] == __import__("json").dumps(profile)
        assert client.post("/", json=req).json()["result"]["isError"] is True
        invalid = client.post("/", json=req, headers={"Authorization": "Bearer " + make(sub="someone-else")})
        assert invalid.json()["result"]["isError"] is True
        bad_args = client.post("/", json=_rpc("tools/call", 4,
            {"name": "get_profile", "arguments": {"id": "anything"}}), headers=headers)
        assert bad_args.json()["error"]["code"] == -32602


def test_oversize_discovery_not_allowed_anonymously(tokens):
    verifier, make = tokens
    downstream = FastAPI()
    @downstream.post("/")
    async def protected():
        return {"ok": True}
    with TestClient(MCPBearerAuth(downstream, oauth=verifier)) as client:
        huge = "x" * 66000
        assert client.post("/", content=huge).status_code == 401
        assert client.post("/", content=huge, headers={
            "Authorization": "Bearer " + make()}).json() == {"ok": True}


def test_invalid_batch_does_not_bypass_gateway(tokens):
    verifier, make = tokens
    downstream = FastAPI()
    @downstream.post("/")
    async def protected():
        return {"ok": True}
    with TestClient(MCPBearerAuth(downstream, oauth=verifier)) as client:
        assert client.post("/", json=[_rpc("initialize"), _rpc("tools/call")]).status_code == 401
        assert client.post("/", json=_rpc("tools/list"), headers={"Authorization": "Bearer invalid"}).status_code == 200


def test_real_fastmcp_endpoint_with_oauth_discovery(tokens):
    """Verify the actual MCP SDK transport, not only a stub FastAPI responder."""
    from contextlib import asynccontextmanager
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    verifier, make = tokens
    # A separate FastMCP instance avoids starting the session-scoped test
    # fixture's StreamableHTTPSessionManager twice (the SDK forbids reuse).
    probe = FastMCP("OAuth probe", stateless_http=True, json_response=True,
                    streamable_http_path="/",
                    transport_security=TransportSecuritySettings(
                        enable_dns_rebinding_protection=True,
                        allowed_hosts=["localhost", "localhost:*"],
                        allowed_origins=["http://localhost:*"],
                    ))

    @probe.tool()
    async def list_nodes() -> list[dict]:
        """List active nodes."""
        return []

    @probe.tool()
    async def write_files(files: list[dict]) -> dict:
        """Test-only mutating tool; never invoked in this test."""
        return {"written": []}

    @asynccontextmanager
    async def lifespan(_app):
        async with probe.session_manager.run():
            yield

    api = FastAPI(lifespan=lifespan)
    api.mount("/mcp", MCPBearerAuth(probe.streamable_http_app(), oauth=verifier))
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}

    with TestClient(api, base_url="http://localhost") as client:
        init = client.post("/mcp/", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "oauth-integration-test", "version": "1.0"}},
        })
        assert init.status_code == 200, init.text
        assert "serverInfo" in init.json()["result"]

        list_response = client.post("/mcp/", headers={**headers,
            "MCP-Protocol-Version": "2025-06-18"},
            json=_rpc("tools/list", 2))
        assert list_response.status_code == 200, list_response.text
        tools = {item["name"]: item for item in list_response.json()["result"]["tools"]}
        assert "get_profile" in tools
        assert "list_nodes" in tools
        assert "write_files" in tools
        assert all(t["securitySchemes"] ==
                   [{"type": "oauth2", "scopes": ["rigorcore:access"]}]
                   for t in tools.values())
        assert tools["get_profile"]["_meta"]["openai/profile"] is True

        denied = client.post("/mcp/", headers=headers, json=_rpc("tools/call", 3,
            {"name": "list_nodes", "arguments": {}}))
        assert denied.status_code == 200
        assert denied.json()["result"]["isError"]
        assert "mcp/www_authenticate" in denied.json()["result"]["_meta"]

        allowed = client.post("/mcp/", headers={**headers,
            "Authorization": "Bearer " + make()},
            json=_rpc("tools/call", 4, {"name": "list_nodes", "arguments": {}}))
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["result"]["isError"] is False

        profile = client.post("/mcp/", headers={**headers,
            "Authorization": "Bearer " + make()},
            json=_rpc("tools/call", 5, {"name": "get_profile", "arguments": {}}))
        assert profile.status_code == 200
        assert profile.json()["result"]["structuredContent"]["id"].startswith("rigorcore_")



def test_readonly_metadata_probes_have_only_empty_results(tokens):
    """Registration may probe capabilities beyond initialize/tools-list."""
    from fastapi import Request

    verifier, _ = tokens
    private = FastAPI()

    @private.post("/")
    async def internal(request: Request):
        raise AssertionError("Public probes must not reach data-bearing MCP handlers")

    with TestClient(MCPBearerAuth(private, oauth=verifier)) as client:
        for method, result in [
            ("ping", {}),
            ("resources/list", {"resources": []}),
            ("resources/templates/list", {"resourceTemplates": []}),
            ("prompts/list", {"prompts": []}),
        ]:
            response = client.post("/", json={
                "jsonrpc": "2.0", "id": 9, "method": method,
            })
            assert response.status_code == 200, response.text
            assert response.json()["result"] == result
            assert "no-store" in response.headers.get("cache-control", "")


def test_opt_in_auth_logging_never_records_headers_or_parameters(tokens, caplog, monkeypatch):
    verifier, _ = tokens
    private = FastAPI()
    monkeypatch.setenv("RIGORCORE_MCP_TRACE_REJECTIONS", "1")
    with TestClient(MCPBearerAuth(private, oauth=verifier)) as client:
        result = client.post("/", headers={"Authorization": "Bearer leaked-SECRET"},
                             json={"jsonrpc": "2.0", "id": 13,
                                   "method": "private/method",
                                   "params": {"secret": "leaked-PARAM"}})
        assert result.status_code == 401
    assert "private/method" in caplog.text
    assert "leaked-SECRET" not in caplog.text
    assert "leaked-PARAM" not in caplog.text
