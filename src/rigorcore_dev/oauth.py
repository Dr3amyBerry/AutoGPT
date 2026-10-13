"""OAuth 2.1 resource-server validation for the private MCP control plane.

Use an established OIDC/OAuth identity provider (for example Auth0).
This module NEVER issues tokens or accepts unsigned/self-signed access tokens.
"""
from __future__ import annotations

import asyncio
import hmac
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

import jwt
from jwt import PyJWKClient
from starlette.responses import JSONResponse


def _https_url(value: str, name: str) -> str:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an absolute HTTPS URL without credentials/query/fragment") from exc
    return value


@dataclass(frozen=True)
class OAuthSettings:
    issuer: str
    audience: str
    jwks_url: str
    owner_sub: str
    scope: str
    public_host: str

    @classmethod
    def from_env(cls):
        issuer = _https_url(os.getenv("RIGORCORE_OAUTH_ISSUER", "").strip(), "RIGORCORE_OAUTH_ISSUER")
        audience = _https_url(os.getenv("RIGORCORE_OAUTH_AUDIENCE", "").strip(), "RIGORCORE_OAUTH_AUDIENCE")
        owner_sub = os.getenv("RIGORCORE_OAUTH_OWNER_SUB", "").strip()
        if not owner_sub:
            raise RuntimeError("RIGORCORE_OAUTH_OWNER_SUB must contain the exact authorized user subject")
        public_host = os.getenv("RIGORCORE_PUBLIC_HOST", "").strip()
        if not public_host or urlsplit(audience).hostname != public_host:
            raise RuntimeError("RIGORCORE_PUBLIC_HOST must match the OAuth audience hostname")
        jwks_url = _https_url(
            os.getenv("RIGORCORE_OAUTH_JWKS_URL", "").strip() or
            issuer.rstrip("/") + "/.well-known/jwks.json",
            "RIGORCORE_OAUTH_JWKS_URL",
        )
        if urlsplit(jwks_url).netloc != urlsplit(issuer).netloc:
            raise RuntimeError("JWKS must be hosted on the configured issuer origin")
        scope = os.getenv("RIGORCORE_OAUTH_SCOPE", "rigorcore:access").strip()
        if not scope or any(ch.isspace() for ch in scope):
            raise RuntimeError("RIGORCORE_OAUTH_SCOPE must be one nonempty scope")
        return cls(issuer, audience, jwks_url, owner_sub, scope, public_host)

    @property
    def resource_metadata_url(self) -> str:
        return f"https://{self.public_host}/.well-known/oauth-protected-resource"

    def protected_resource_metadata(self) -> dict:
        return {
            "resource": self.audience,
            "authorization_servers": [self.issuer],
            "scopes_supported": [self.scope],
        }


class OAuthVerifier:
    """Validate signed RS256 access tokens; fail closed on JWKS or claim errors.

    Owner-subject pinning is mandatory because a successful IdP login alone does
    not authorize arbitrary tenants/users to run shell commands on this VPS.
    All MCP tools share one high-privilege scope in this initial private release.
    """

    def __init__(self, settings: OAuthSettings):
        self.settings = settings
        self.jwks_client = PyJWKClient(settings.jwks_url, cache_jwk_set=True, lifespan=300, timeout=5)

    def validate(self, token: str) -> bool:
        if not token or len(token) > 16384:
            return False
        try:
            key = self.jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                leeway=30,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
            if not hmac.compare_digest(str(claims["sub"]), self.settings.owner_sub):
                return False
            raw_scopes = claims.get("scope", "")
            scopes = set(raw_scopes.split()) if isinstance(raw_scopes, str) else set()
            permissions = claims.get("permissions", [])
            if isinstance(permissions, list):
                scopes.update(item for item in permissions if isinstance(item, str))
            return self.settings.scope in scopes
        except (jwt.PyJWTError, ValueError, TypeError, OSError):
            return False


class MCPBearerAuth:
    """ASGI gate for the MCP endpoint only; the agent WebSocket uses its own key."""

    def __init__(self, app, *, legacy_token: str | None = None,
                 oauth: OAuthVerifier | None = None):
        if (legacy_token is None) == (oauth is None):
            raise ValueError("Exactly one of legacy_token or oauth is required")
        self.app = app
        self.legacy_token = legacy_token
        self.oauth = oauth

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            authorization = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
            ok = False
            if authorization.startswith("Bearer "):
                token = authorization[7:]
                if self.oauth:
                    ok = await asyncio.to_thread(self.oauth.validate, token)
                else:
                    ok = hmac.compare_digest(token, self.legacy_token)
            if not ok:
                response_headers = {}
                if self.oauth:
                    settings = self.oauth.settings
                    response_headers["WWW-Authenticate"] = (
                        f'Bearer resource_metadata="{settings.resource_metadata_url}", '
                        f'scope="{settings.scope}", error="invalid_token"'
                    )
                response = JSONResponse({"detail": "unauthorized"}, status_code=401, headers=response_headers)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
