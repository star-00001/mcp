"""How each request gets an authenticated Azure DevOps client.

* ``OboClientProvider`` (production, HTTP): checks the caller's Entra app role, then exchanges
  the caller's token (OBO) for an Azure DevOps token, so ADO permissions apply to the user.
* ``PatClientProvider`` (local stdio development only): a personal access token, no per-user
  identity and no role checks.
"""

from __future__ import annotations

import base64
import logging
from typing import Protocol

import httpx

from client import AdoClient
from settings import ADO_SCOPE, AdoSettings
from mcp_server_common.auth import current_principal
from mcp_server_common.obo import OboTokenProvider

log = logging.getLogger(__name__)

ROLE_READ = "Ado.Read"
ROLE_WRITE = "Ado.Write"


class AdoClientProvider(Protocol):
    def authorize(self, required_role: str) -> None:
        """Raise ToolError if the caller lacks the role (cheap; no Azure DevOps call)."""
        ...

    async def client(self, required_role: str) -> AdoClient: ...


def _http(settings: AdoSettings) -> httpx.AsyncClient:
    # One pooled client per process; never follow redirects off the ADO origin.
    return httpx.AsyncClient(timeout=settings.http_timeout_s, follow_redirects=False)


class OboClientProvider:
    def __init__(self, settings: AdoSettings, obo: OboTokenProvider | None = None):
        self._settings = settings
        self._obo = obo or OboTokenProvider(settings)
        self._http = _http(settings)

    def authorize(self, required_role: str) -> None:
        current_principal().require_role(required_role)

    async def client(self, required_role: str) -> AdoClient:
        principal = current_principal()
        principal.require_role(required_role)
        token = await self._obo.token_for(principal.raw_token, ADO_SCOPE)
        return AdoClient(
            self._http, self._settings.org_url, f"Bearer {token}", self._settings.api_version
        )


class PatClientProvider:
    def __init__(self, settings: AdoSettings):
        if settings.pat is None:
            raise ValueError("Set ADO_MCP_PAT for stdio development mode.")
        raw = f":{settings.pat.get_secret_value()}".encode()
        self._auth = "Basic " + base64.b64encode(raw).decode()
        self._settings = settings
        self._http = _http(settings)
        log.warning("PAT mode: no per-user identity or role checks. Development only.")

    def authorize(self, required_role: str) -> None:
        return None  # PAT mode has no per-user identity

    async def client(self, required_role: str) -> AdoClient:
        return AdoClient(self._http, self._settings.org_url, self._auth, self._settings.api_version)
