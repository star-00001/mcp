"""Azure DevOps MCP server: read tools (``Ado.Read``) and write tools (``Ado.Write``)."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from client import AdoClient, AdoError
from providers import ROLE_READ, ROLE_WRITE, AdoClientProvider
from settings import AdoSettings
from write_tools import register_write_tools
from mcp_server_common.app import create_server
from mcp_server_common.auth import EntraTokenVerifier
from mcp_server_common.limits import capped, clamp

INSTRUCTIONS = (
    "Tools for Azure DevOps (projects, repositories, pull requests, work items, pipelines). "
    "All calls run with the signed-in user's own Azure DevOps permissions. "
    "Start with list_projects if the project name is unknown. "
    "For work item searches use query_work_items with a WIQL SELECT statement. "
    "Tools that create or change data ask the user to confirm before anything is changed."
)
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
MAX_WIQL_LENGTH = 32_000
_WIQL_SELECT = re.compile(r"^\s*SELECT\s", re.IGNORECASE)

Project = Annotated[str, Field(min_length=1, max_length=256, description="Project name or ID.")]
Repository = Annotated[
    str, Field(min_length=1, max_length=256, description="Repository name or ID.")
]
Top = Annotated[int | None, Field(ge=1, le=1000, description="Maximum results (server caps this).")]

T = TypeVar("T")


def validate_wiql(wiql: str) -> str:
    """WIQL is query-only, but keep inputs to a single SELECT of bounded size."""
    if len(wiql) > MAX_WIQL_LENGTH:
        raise ToolError(f"WIQL is too long (max {MAX_WIQL_LENGTH} characters).")
    if not _WIQL_SELECT.match(wiql):
        raise ToolError(
            "WIQL must be a single SELECT statement, e.g. "
            "SELECT [System.Id] FROM WorkItems WHERE [System.State] = 'Active'"
        )
    if ";" in wiql:
        raise ToolError("WIQL must be a single statement (no ';').")
    return wiql


def build_server(
    settings: AdoSettings,
    provider: AdoClientProvider,
    *,
    require_auth: bool = True,
    token_verifier: EntraTokenVerifier | None = None,
) -> MCPServer:
    mcp = create_server(
        "azure-devops",
        settings,
        instructions=INSTRUCTIONS,
        require_auth=require_auth,
        token_verifier=token_verifier,
    )
    limit = settings.max_items

    async def run(fn: Callable[[AdoClient], Awaitable[T]], role: str = ROLE_READ) -> T:
        client = await provider.client(role)
        try:
            return await fn(client)
        except AdoError as e:
            raise ToolError(_friendly(e)) from None

    async def run_write(fn: Callable[[AdoClient], Awaitable[T]]) -> T:
        return await run(fn, ROLE_WRITE)

    if settings.enable_write_tools:
        register_write_tools(
            mcp,
            run_write,
            authorize=lambda: provider.authorize(ROLE_WRITE),
            confirm_writes=settings.confirm_writes,
        )

    # -- projects & repositories --------------------------------------------------------------
    @mcp.tool(annotations=READ_ONLY)
    async def list_projects(top: Top = None) -> dict[str, Any]:
        """List Azure DevOps projects the user can see."""
        n = clamp(top, limit, limit)
        return capped(await run(lambda c: c.list_projects(n + 1)), n)

    @mcp.tool(annotations=READ_ONLY)
    async def list_repositories(project: Project) -> dict[str, Any]:
        """List Git repositories in a project."""
        return capped(await run(lambda c: c.list_repositories(project)), limit)

    @mcp.tool(annotations=READ_ONLY)
    async def get_repository(project: Project, repository: Repository) -> dict[str, Any]:
        """Get details of one Git repository (default branch, size, URL)."""
        return await run(lambda c: c.get_repository(project, repository))

    @mcp.tool(annotations=READ_ONLY)
    async def list_branches(
        project: Project, repository: Repository, top: Top = None
    ) -> dict[str, Any]:
        """List branches of a repository."""
        n = clamp(top, limit, limit)
        return capped(await run(lambda c: c.list_branches(project, repository, n + 1)), n)

    @mcp.tool(annotations=READ_ONLY)
    async def list_commits(
        project: Project,
        repository: Repository,
        branch: Annotated[
            str | None, Field(max_length=256, description="Branch name; default branch if omitted.")
        ] = None,
        top: Top = None,
    ) -> dict[str, Any]:
        """List recent commits in a repository, newest first."""
        n = clamp(top, 20, limit)
        return capped(await run(lambda c: c.list_commits(project, repository, branch, n + 1)), n)

    # -- pull requests --------------------------------------------------------------------------
    @mcp.tool(annotations=READ_ONLY)
    async def list_pull_requests(
        project: Project,
        repository: Annotated[
            str | None, Field(max_length=256, description="Limit to one repository.")
        ] = None,
        status: Literal["active", "completed", "abandoned", "all"] = "active",
        top: Top = None,
    ) -> dict[str, Any]:
        """List pull requests in a project, optionally for one repository."""
        n = clamp(top, 25, limit)
        return capped(
            await run(lambda c: c.list_pull_requests(project, repository, status, n + 1)), n
        )

    @mcp.tool(annotations=READ_ONLY)
    async def get_pull_request(
        project: Project, pull_request_id: Annotated[int, Field(ge=1)]
    ) -> dict[str, Any]:
        """Get one pull request with its description and reviewer votes."""
        return await run(lambda c: c.get_pull_request(project, pull_request_id))

    # -- work items -----------------------------------------------------------------------------
    @mcp.tool(annotations=READ_ONLY)
    async def get_work_items(
        project: Project,
        ids: Annotated[list[Annotated[int, Field(ge=1)]], Field(min_length=1, max_length=200)],
        include_description: bool = False,
    ) -> dict[str, Any]:
        """Get work items by ID (title, type, state, assignee, priority, paths, dates)."""
        unique = list(dict.fromkeys(ids))
        return capped(
            await run(lambda c: c.get_work_items(project, unique, include_description)), 200
        )

    @mcp.tool(annotations=READ_ONLY)
    async def query_work_items(
        project: Project,
        wiql: Annotated[
            str,
            Field(
                description=(
                    "WIQL SELECT statement. Example: SELECT [System.Id] FROM WorkItems WHERE "
                    "[System.TeamProject] = @project AND [System.State] = 'Active' "
                    "ORDER BY [System.ChangedDate] DESC"
                )
            ),
        ],
        top: Top = None,
    ) -> dict[str, Any]:
        """Search work items with a WIQL query; returns the matching work items' details."""
        query = validate_wiql(wiql)
        n = clamp(top, 50, min(limit, 200))

        async def go(c: AdoClient) -> dict[str, Any]:
            ids = await c.query_work_item_ids(project, query, n + 1)
            result = capped(await c.get_work_items(project, ids[:n]), n)
            result["truncated"] = len(ids) > n
            return result

        return await run(go)

    # -- pipelines & builds ---------------------------------------------------------------------
    @mcp.tool(annotations=READ_ONLY)
    async def list_pipelines(project: Project, top: Top = None) -> dict[str, Any]:
        """List pipelines in a project."""
        n = clamp(top, limit, limit)
        return capped(await run(lambda c: c.list_pipelines(project, n + 1)), n)

    @mcp.tool(annotations=READ_ONLY)
    async def list_pipeline_runs(
        project: Project, pipeline_id: Annotated[int, Field(ge=1)], top: Top = None
    ) -> dict[str, Any]:
        """List recent runs of a pipeline, newest first."""
        n = clamp(top, 20, limit)
        return capped(await run(lambda c: c.list_pipeline_runs(project, pipeline_id)), n)

    @mcp.tool(annotations=READ_ONLY)
    async def get_pipeline_run(
        project: Project,
        pipeline_id: Annotated[int, Field(ge=1)],
        run_id: Annotated[int, Field(ge=1)],
    ) -> dict[str, Any]:
        """Get the state and result of one pipeline run."""
        return await run(lambda c: c.get_pipeline_run(project, pipeline_id, run_id))

    @mcp.tool(annotations=READ_ONLY)
    async def list_builds(
        project: Project,
        pipeline_id: Annotated[int | None, Field(ge=1, description="Build definition ID.")] = None,
        status: Literal["inProgress", "completed", "notStarted", "all"] | None = None,
        branch: Annotated[str | None, Field(max_length=256)] = None,
        top: Top = None,
    ) -> dict[str, Any]:
        """List recent builds, optionally filtered by pipeline, status or branch."""
        n = clamp(top, 20, limit)
        return capped(
            await run(lambda c: c.list_builds(project, n + 1, pipeline_id, status, branch)), n
        )

    # -- resources --------------------------------------------------------------------------------
    @mcp.resource(
        "ado://{project}/workitems/{work_item_id}",
        name="work_item",
        description="One Azure DevOps work item as JSON.",
        mime_type="application/json",
    )
    async def work_item_resource(project: str, work_item_id: str) -> str:
        if not work_item_id.isdigit():
            raise ToolError("work_item_id must be a number")
        items = await run(lambda c: c.get_work_items(project, [int(work_item_id)], True))
        if not items:
            raise ToolError(f"Work item {work_item_id} not found")
        return json.dumps(items[0])

    return mcp


def _friendly(e: AdoError) -> str:
    if e.status == 401:
        return "Azure DevOps rejected the credentials. Sign in again or check the org connection."
    if e.status == 403:
        return "You don't have permission for this in Azure DevOps."
    if e.status == 404:
        return f"Not found in Azure DevOps: {e.message}"
    if e.status == 400:
        return f"Azure DevOps rejected the request: {e.message}"
    if e.status in (409, 412):
        return "The item changed in Azure DevOps since it was read. Fetch it again and retry."
    if e.status == 429:
        return "Azure DevOps is rate limiting requests. Try again shortly."
    return f"Azure DevOps error (HTTP {e.status})."
