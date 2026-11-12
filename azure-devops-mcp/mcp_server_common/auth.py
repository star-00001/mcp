"""Entra ID access-token validation and caller identity for custom MCP servers.

Every request is validated (stateless): signature (tenant JWKS), issuer, audience, expiry,
and the delegated scope. App roles from the token are exposed for tool-level authorization.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import anyio
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server_common.settings import ServerSettings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """The signed-in user a request runs on behalf of."""

    object_id: str
    username: str | None
    roles: frozenset[str] = field(default_factory=frozenset)
    raw_token: str = field(default="", repr=False)  # never log or return this

    def require_role(self, role: str) -> None:
        if role not in self.roles:
            raise ToolError(
                f"Permission denied: this tool needs the '{role}' app role. "
                "Ask an administrator to assign it in Entra ID."
            )


class EntraTokenVerifier(TokenVerifier):
    """Validates Entra ID v2 access tokens issued for this server."""

    def __init__(self, settings: ServerSettings, jwk_client: jwt.PyJWKClient | None = None):
        self._settings = settings
        self._audiences = [settings.client_id, f"api://{settings.client_id}"]
        self._jwks = jwk_client or jwt.PyJWKClient(
            f"{settings.authority}/discovery/v2.0/keys", cache_keys=True, lifespan=3600
        )

    def decode(self, token: str) -> dict[str, Any]:
        """Synchronous validation; raises ``jwt.PyJWTError`` on any failure."""
        key = self._jwks.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=self._audiences,
            issuer=self._settings.issuer,
            options={"require": ["exp", "iat", "aud", "iss", "oid"]},
            leeway=60,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # JWKS lookup may do network I/O; keep it off the event loop.
            claims = await anyio.to_thread.run_sync(self.decode, token)
        except jwt.PyJWTError as exc:
            log.info("token rejected: %s", type(exc).__name__)  # no token content in logs
            return None
        scopes = str(claims.get("scp", "")).split()
        if self._settings.required_scope not in scopes:
            log.info("token rejected: missing delegated scope")
            return None
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("appid") or ""),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            subject=str(claims["oid"]),
            claims={
                "iss": claims["iss"],
                "roles": list(claims.get("roles", [])),
                "username": claims.get("preferred_username") or claims.get("upn"),
                "groups": list(claims.get("groups", [])),
                "tid": claims.get("tid"),
            },
        )


def current_principal() -> Principal:
    """Return the caller of the current MCP request (HTTP transport with auth only)."""
    access = get_access_token()
    if access is None or access.subject is None:
        raise ToolError("Not authenticated.")
    claims = access.claims or {}
    return Principal(
        object_id=access.subject,
        username=claims.get("username"),
        roles=frozenset(claims.get("roles", [])),
        raw_token=access.token,
    )
