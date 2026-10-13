"""OAuth resource-server tests: cryptographic verification and fail-closed authorization."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
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
    from fastapi import Request

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
        assert client.post("/", json=_rpc("resources/list")).status_code == 401
        assert client.get("/").status_code == 401

        allowed = client.post("/", json=call,
                              headers={"Authorization": "Bearer " + make()})
        assert allowed.status_code == 200
        assert allowed.json()["result"] == {"ok": True}


def test_profile_uses_verified_subject_and_never_token_id(tokens):
    verifier, make = tokens
    from fastapi import Request

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
