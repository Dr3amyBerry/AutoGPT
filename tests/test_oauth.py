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
