"""Build and run an Entra-protected, stateless Streamable HTTP MCP server."""

from __future__ import annotations

import logging

import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_server_common.auth import EntraTokenVerifier
from mcp_server_common.settings import ServerSettings


def create_server(
    name: str,
    settings: ServerSettings,
    *,
    instructions: str | None = None,
    version: str = "0.1.0",
    require_auth: bool = True,
    token_verifier: EntraTokenVerifier | None = None,
) -> MCPServer:
    """Create an MCPServer that acts as an OAuth 2.1 resource server for Entra ID.

    ``require_auth=False`` is only for local stdio development.
    """
    if not require_auth:
        return MCPServer(name, instructions=instructions, version=version)

    server = MCPServer(
        name,
        instructions=instructions,
        version=version,
        token_verifier=token_verifier or EntraTokenVerifier(settings),
        auth=AuthSettings(
            issuer_url=settings.issuer,
            resource_server_url=settings.public_url,
            required_scopes=[settings.required_scope],
            validate_token_resource=False,  # EntraTokenVerifier checks the audience itself
        ),
    )

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:  # unauthenticated liveness probe
        return JSONResponse({"status": "ok"})

    return server


def http_app(server: MCPServer, settings: ServerSettings) -> Starlette:
    """ASGI app: stateless (no MCP sessions) so replicas can scale to zero and back."""
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
        ),
    )


def run_http(server: MCPServer, settings: ServerSettings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(
        http_app(server, settings), host=settings.host, port=settings.port, proxy_headers=True
    )
