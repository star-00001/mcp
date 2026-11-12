"""Run the Azure DevOps MCP server.

python -m azure_devops_mcp                     # Streamable HTTP, Entra auth + OBO (default)
python -m azure_devops_mcp --transport stdio   # local dev with ADO_MCP_PAT, no auth
"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from providers import OboClientProvider, PatClientProvider
from server import build_server
from settings import AdoSettings
from mcp_server_common.app import run_http
from mcp_server_common.telemetry import setup_telemetry


def main() -> None:
    load_dotenv()  # Load .env for local stdio development mode (ADO_MCP_PAT)
    parser = argparse.ArgumentParser(prog="azure_devops_mcp")
    parser.add_argument("--transport", choices=["http", "stdio"], default="http")
    args = parser.parse_args()
    settings = AdoSettings()  # type: ignore[call-arg]  # values come from env
    setup_telemetry("azure-devops-mcp")

    if args.transport == "stdio":
        build_server(settings, PatClientProvider(settings), require_auth=False).run("stdio")
    else:
        run_http(build_server(settings, OboClientProvider(settings)), settings)


if __name__ == "__main__":
    main()
