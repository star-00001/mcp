from __future__ import annotations

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import SettingsConfigDict

from mcp_server_common.settings import ServerSettings

ADO_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"  # Azure DevOps resource

load_dotenv()  # Load .env for local stdio development mode (ADO_MCP_PAT)
class AdoSettings(ServerSettings):
    model_config = SettingsConfigDict(env_prefix="ADO_MCP_", env_file=".env", extra="ignore")

    org_url: str = Field(description="Azure DevOps org, e.g. https://dev.azure.com/contoso")
    pat: SecretStr | None = Field(
        default=None, description="DEV/stdio only: personal access token instead of Entra OBO."
    )
    api_version: str = "7.1"
    enable_write_tools: bool = Field(default=True, description="Register write tools.")
    confirm_writes: bool = Field(
        default=True,
        description="Ask the user to confirm each write (MCP elicitation). Turn off only when "
        "the gateway enforces its own argument-bound approvals.",
    )

    @field_validator("org_url")
    @classmethod
    def _https_org(cls, v: str) -> str:
        v = v.rstrip("/")
        if not v.startswith("https://dev.azure.com/") and not v.endswith(".visualstudio.com"):
            raise ValueError(
                "org_url must be an Azure DevOps Services URL (https://dev.azure.com/<org>)"
            )
        return v
