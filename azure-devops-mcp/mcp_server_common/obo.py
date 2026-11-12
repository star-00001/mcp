"""Entra On-Behalf-Of: exchange the caller's token for a downstream API token."""

from __future__ import annotations

import threading

import anyio
import msal
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server_common.settings import ServerSettings


class OboTokenProvider:
    """Acquires downstream tokens on behalf of the user. MSAL caches them in memory
    per incoming assertion, so repeated calls in a token's lifetime don't hit Entra."""

    def __init__(self, settings: ServerSettings):
        if settings.client_secret is None:
            # Managed identity + federated credential replaces the secret in M3.
            raise ValueError("OBO needs a client credential (set *_CLIENT_SECRET for dev).")
        self._settings = settings
        self._app: msal.ConfidentialClientApplication | None = None
        self._lock = threading.Lock()

    def _client(self) -> msal.ConfidentialClientApplication:
        # Built lazily: MSAL contacts Entra on construction, and server startup (scale-to-zero
        # cold start, health probes) must not depend on Entra being reachable.
        with self._lock:
            if self._app is None:
                secret = self._settings.client_secret
                assert secret is not None
                self._app = msal.ConfidentialClientApplication(
                    self._settings.client_id,
                    client_credential=secret.get_secret_value(),
                    authority=self._settings.authority,
                )
            return self._app

    async def token_for(self, user_assertion: str, scope: str) -> str:
        try:
            result = await anyio.to_thread.run_sync(
                lambda: self._client().acquire_token_on_behalf_of(user_assertion, scopes=[scope])
            )
        except ValueError:  # MSAL raises this when the authority can't be resolved
            raise ToolError("Could not reach Entra ID to obtain a downstream token.") from None
        if "access_token" not in result:
            # Error codes are safe to surface; descriptions can contain IDs, so keep them out.
            code = result.get("error", "unknown_error")
            if code in {"interaction_required", "consent_required"}:
                raise ToolError("Consent is missing for the downstream API. Contact an admin.")
            raise ToolError(f"Could not obtain a downstream token ({code}).")
        return str(result["access_token"])
