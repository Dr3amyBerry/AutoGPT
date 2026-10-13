"""ASGI OAuth gateway for MCP, including public tool discovery with protected calls."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import json
import os
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit

import jwt
from jwt import PyJWKClient
from starlette.responses import JSONResponse

_oauth_logger = logging.getLogger('rigorcore_dev.oauth')


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
    """Only accept correctly signed Auth0 JWTs for one owner and scope."""

    def __init__(self, settings: OAuthSettings):
        self.settings = settings
        self.jwks_client = PyJWKClient(settings.jwks_url, cache_jwk_set=True, lifespan=300, timeout=5)

    def validate_claims(self, token: str) -> dict | None:
        if not token or len(token) > 16384:
            return None
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
                return None
            raw_scopes = claims.get("scope", "")
            scopes = set(raw_scopes.split()) if isinstance(raw_scopes, str) else set()
            permissions = claims.get("permissions", [])
            if isinstance(permissions, list):
                scopes.update(item for item in permissions if isinstance(item, str))
            return claims if self.settings.scope in scopes else None
        except (jwt.PyJWTError, ValueError, TypeError, OSError):
            return None

    def validate(self, token: str) -> bool:
        return self.validate_claims(token) is not None


def _tool_schemes(scope: str) -> list[dict]:
    return [{"type": "oauth2", "scopes": [scope]}]


PROFILE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1, "pattern": r"\S"},
        "nickname": {"type": "string"},
    },
    "required": ["id"],
    "additionalProperties": False,
}

# Only empty protocol metadata can be exposed without a user token.
# Never forward these requests to FastMCP: future registered resources or
# prompts might contain workspace-specific information.
PUBLIC_EMPTY_RESPONSES = {
    "ping": {},
    "resources/list": {"resources": []},
    "resources/templates/list": {"resourceTemplates": []},
    "prompts/list": {"prompts": []},
}


def _trace_denied_method(method) -> None:
    """Opt-in metadata-only debug signal; no bodies, headers or credentials."""
    if os.getenv("RIGORCORE_MCP_TRACE_REJECTIONS", "0") != "1":
        return
    value = method if isinstance(method, str) else "(missing-or-batch)"
    value = value[:64]
    safe = re.sub(r"[^a-zA-Z0-9_.:/-]", "_", value)
    _oauth_logger.warning("MCP unauthenticated method rejected: %s", safe)


READ_ONLY_TOOLS = {
    "list_nodes", "list_workspaces", "list_files", "read_file", "search_text",
    "get_task", "get_tasks", "list_tasks", "get_task_logs", "wait_for_task",
}


def _decorate_tool_listing(body: dict, scope: str) -> dict:
    tools = body.get("result", {}).get("tools")
    if not isinstance(tools, list):
        return body
    scheme = _tool_schemes(scope)
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool["securitySchemes"] = scheme
        meta = tool.get("_meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        meta["securitySchemes"] = scheme
        tool["_meta"] = meta
        name = tool.get("name")
        read_only = name in READ_ONLY_TOOLS
        annotation = tool.get("annotations") or {}
        if not isinstance(annotation, dict):
            annotation = {}
        tool["annotations"] = annotation
        annotation.update({
            "readOnlyHint": read_only,
            "destructiveHint": not read_only,
            "openWorldHint": False,
        })
    tools.append({
        "name": "get_profile",
        "description": "Get the stable opaque identity of the account authenticated for this MCP request.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "outputSchema": PROFILE_SCHEMA,
        "securitySchemes": scheme,
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        "_meta": {"openai/profile": True, "securitySchemes": scheme},
    })
    return body


def _challenge(settings: OAuthSettings) -> str:
    return (
        f'Bearer resource_metadata="{settings.resource_metadata_url}", '
        'error="invalid_token", error_description="Authenticate with Auth0 to access RigorCore Dev"'
    )


def _profile_for_claims(claims: dict) -> dict:
    # Auth0's immutable "sub" is the identity. Hashing it with the issuer
    # avoids exposing the raw IdP user ID to the tool consumer.
    source = (str(claims["iss"]) + "\0" + str(claims["sub"])).encode("utf-8")
    return {"id": "rigorcore_" + hashlib.sha256(source).hexdigest()[:32],
            "nickname": "RigorCore Dev owner"}


class MCPBearerAuth:
    """Fail-closed MCP ASGI gateway. Agent WebSocket uses separate authentication.

    In OAuth mode only initialize, tools/list and initialized notifications are
    anonymously discoverable. Tool calls and all other methods require a verified
    JWT. An unauthenticated tools/call returns the OpenAI MCP OAuth challenge.
    This discovery layer is needed for ChatGPT's mixed OAuth linking flow.
    """

    MAX_PROBE_BYTES = 65536

    def __init__(self, app, *, legacy_token: str | None = None,
                 oauth: OAuthVerifier | None = None):
        if (legacy_token is None) == (oauth is None):
            raise ValueError("Exactly one of legacy_token or oauth is required")
        self.app = app
        self.legacy_token = legacy_token
        self.oauth = oauth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if self.oauth is None:
            if not hmac.compare_digest(token, self.legacy_token):
                await JSONResponse({"detail": "unauthorized"}, status_code=401)(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        claims = await asyncio.to_thread(self.oauth.validate_claims, token) if token else None
        verified = claims is not None

        # GET /mcp/ stays protected, including WebSocket/streaming handshakes.
        if scope.get("method") != "POST":
            if not verified:
                await self._unauthorized(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        # Large authorized tool writes are forwarded without buffering.
        raw_length = headers.get(b"content-length", b"")
        try:
            length = int(raw_length) if raw_length else 0
        except ValueError:
            length = self.MAX_PROBE_BYTES + 1
        if length > self.MAX_PROBE_BYTES:
            if not verified:
                await self._unauthorized(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        pending = deque()
        total = 0
        while True:
            message = await receive()
            pending.append(message)
            if message["type"] != "http.request":
                break
            total += len(message.get("body", b""))
            if total > self.MAX_PROBE_BYTES or not message.get("more_body", False):
                break

        async def replay():
            if pending:
                return pending.popleft()
            return await receive()

        if total > self.MAX_PROBE_BYTES:
            if not verified:
                await self._unauthorized(scope, replay, send)
                return
            await self.app(scope, replay, send)
            return

        try:
            request = json.loads(b"".join(m.get("body", b"") for m in pending))
        except (ValueError, TypeError):
            request = None
        method = request.get("method") if isinstance(request, dict) else None
        request_id = request.get("id") if isinstance(request, dict) else None
        public_discovery = method in {"initialize", "tools/list", "notifications/initialized"}
        public_discovery = public_discovery or method in PUBLIC_EMPTY_RESPONSES

        if not verified and not public_discovery:
            _trace_denied_method(method)
            if method == "tools/call" and request_id is not None:
                result = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [{"type": "text", "text": "Sign in to RigorCore Dev before using tools."}],
                        "isError": True,
                        "_meta": {"mcp/www_authenticate": [_challenge(self.oauth.settings)]},
                    },
                }
                await JSONResponse(result, headers={"Cache-Control": "no-store"})(scope, replay, send)
            else:
                await self._unauthorized(scope, replay, send)
            return

        # Public *empty* metadata probes cannot enumerate private data, even
        # if an MCP implementation later registers prompts/resources.
        if method in PUBLIC_EMPTY_RESPONSES:
            if request_id is not None:
                await JSONResponse({
                    "jsonrpc": "2.0", "id": request_id,
                    "result": PUBLIC_EMPTY_RESPONSES[method],
                }, headers={"Cache-Control": "no-store"})(scope, replay, send)
            else:
                await JSONResponse({"detail": "bad JSON-RPC request"}, status_code=400)(
                    scope, replay, send)
            return

        if method == "tools/call" and request.get("params", {}).get("name") == "get_profile":
            arguments = request.get("params", {}).get("arguments", {})
            if arguments not in ({}, None):
                await JSONResponse({"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": "get_profile requires no arguments"}})(scope, replay, send)
                return
            profile = _profile_for_claims(claims)
            await JSONResponse({
                "jsonrpc": "2.0", "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(profile)}],
                    "structuredContent": profile,
                    "isError": False,
                },
            }, headers={"Cache-Control": "no-store"})(scope, replay, send)
            return

        if method != "tools/list":
            await self.app(scope, replay, send)
            return

        # FastMCP (json_response=True) emits tools/list as one JSON response.
        # Rebuild that response to attach auth metadata to each tool. No private
        # workspace data is returned by discovery.
        messages = []

        async def stamp(message):
            messages.append(message)
            if message["type"] != "http.response.body" or message.get("more_body", False):
                return
            started = next((m for m in messages if m["type"] == "http.response.start"), None)
            original = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
            if started is not None and started.get("status") == 200:
                try:
                    document = json.loads(original)
                    if isinstance(document, dict):
                        document = _decorate_tool_listing(document, self.oauth.settings.scope)
                        original = json.dumps(document, separators=(",", ":")).encode("utf-8")
                        started["headers"] = [
                            (k, v) for k, v in started.get("headers", [])
                            if k.lower() != b"content-length"
                        ] + [(b"content-length", str(len(original)).encode())]
                except (ValueError, TypeError):
                    pass
            if started is not None:
                await send(started)
            await send({"type": "http.response.body", "body": original, "more_body": False})

        await self.app(scope, replay, stamp)

    async def _unauthorized(self, scope, receive, send):
        await JSONResponse(
            {"detail": "unauthorized"}, status_code=401,
            headers={"WWW-Authenticate": _challenge(self.oauth.settings), "Cache-Control": "no-store"}
        )(scope, receive, send)
