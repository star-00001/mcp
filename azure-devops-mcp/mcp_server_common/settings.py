"""Settings shared by every K Agent custom MCP server (read from environment variables)."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    """Base settings. Each server subclasses this and sets its own ``env_prefix``.

    Example for the ADO server (prefix ``ADO_MCP_``)::

        ADO_MCP_TENANT_ID=...           # Entra tenant
        ADO_MCP_CLIENT_ID=...           # this server's app registration (token audience)
        ADO_MCP_CLIENT_SECRET=...       # DEV ONLY; production uses managed identity (M3)
        ADO_MCP_PUBLIC_URL=https://ado-mcp.internal.example.com/mcp
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tenant_id: str = Field(description="Entra ID tenant that issues tokens.")
    client_id: str = Field(description="This server's app registration ID (token audience).")
    client_secret: SecretStr | None = Field(
        default=None, description="Dev-only secret for OBO. Production: managed identity."
    )
    public_url: str = Field(
        default="http://localhost:8000/mcp",
        description="Externally visible MCP endpoint URL (RFC 9728 resource identifier).",
    )
    required_scope: str = "access_as_user"

    host: str = "0.0.0.0"  # noqa: S104 - container listens on all interfaces behind ingress
    port: int = 8000
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost:*", "127.0.0.1:*"],
        description="Host headers accepted (DNS-rebinding protection). Add the ingress host.",
    )

    max_items: int = Field(default=100, ge=1, le=1000, description="Cap on list results.")
    http_timeout_s: float = Field(default=30.0, gt=0)

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def issuer(self) -> str:
        return f"{self.authority}/v2.0"
