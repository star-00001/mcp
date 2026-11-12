"""Azure DevOps write tools.

Safety layers (all must pass):
1. Entra app role ``Ado.Write`` on the caller's token (checked before any ADO call).
2. The user's own Azure DevOps permissions (calls run on behalf of the user).
3. A confirmation question shown to the user, describing exactly what will change
   (MCP elicitation). It is on by default (``ADO_MCP_CONFIRM_WRITES=true``). Once the gateway's
   argument-bound approvals (M3/M5) are live, it can be turned off to avoid asking twice.
4. Inputs are restricted: only known work item fields, plus custom fields under the
   ``Custom.`` or ``Microsoft.VSTS.`` namespaces.
"""

import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from client import AdoClient

T = TypeVar("T")
RunWrite = Callable[[Callable[[AdoClient], Awaitable[T]]], Awaitable[T]]

CREATES = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
CHANGES = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)

# Friendly argument name -> ADO field reference name.
STANDARD_FIELDS = {
    "title": "System.Title",
    "description": "System.Description",
    "assigned_to": "System.AssignedTo",
    "state": "System.State",
    "reason": "System.Reason",
    "priority": "Microsoft.VSTS.Common.Priority",
    "severity": "Microsoft.VSTS.Common.Severity",
    "tags": "System.Tags",
    "area_path": "System.AreaPath",
    "iteration_path": "System.IterationPath",
    "acceptance_criteria": "Microsoft.VSTS.Common.AcceptanceCriteria",
    "repro_steps": "Microsoft.VSTS.TCM.ReproSteps",
    "story_points": "Microsoft.VSTS.Scheduling.StoryPoints",
    "remaining_work": "Microsoft.VSTS.Scheduling.RemainingWork",
}
_ALLOWED_EXTRA = re.compile(r"^(Custom|Microsoft\.VSTS)\.[A-Za-z0-9_.]{1,128}$")
_BRANCH = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/\-]{1,250}(?<!/)$")
MAX_TEXT = 32_000

Project = Annotated[str, Field(min_length=1, max_length=256, description="Project name or ID.")]
Branch = Annotated[str, Field(min_length=1, max_length=250, description="Branch name, e.g. main")]


class Confirm(BaseModel):
    confirm: bool = Field(description="Proceed with this change?")


class WorkItemFields(BaseModel):
    """Fields to set. Omit anything that should not change."""

    title: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=MAX_TEXT, description="HTML allowed.")
    assigned_to: str | None = Field(default=None, max_length=256, description="Email or name.")
    state: str | None = Field(default=None, max_length=64)
    reason: str | None = Field(default=None, max_length=128)
    priority: int | None = Field(default=None, ge=1, le=4)
    severity: str | None = Field(default=None, max_length=64, description="e.g. '2 - High'")
    tags: str | None = Field(default=None, max_length=1024, description="Semicolon-separated.")
    area_path: str | None = Field(default=None, max_length=512)
    iteration_path: str | None = Field(default=None, max_length=512)
    acceptance_criteria: str | None = Field(default=None, max_length=MAX_TEXT)
    repro_steps: str | None = Field(default=None, max_length=MAX_TEXT)
    story_points: float | None = Field(default=None, ge=0, le=1000)
    remaining_work: float | None = Field(default=None, ge=0, le=10_000)
    additional_fields: dict[str, str | int | float | bool] | None = Field(
        default=None,
        description="Extra fields by reference name; only 'Custom.*' or 'Microsoft.VSTS.*'.",
    )

    def to_ado(self) -> dict[str, Any]:
        out = {
            STANDARD_FIELDS[k]: v
            for k, v in self.model_dump(exclude_none=True).items()
            if k in STANDARD_FIELDS
        }
        for name, value in (self.additional_fields or {}).items():
            if not _ALLOWED_EXTRA.match(name):
                raise ToolError(
                    f"Field '{name}' is not allowed. Use the named arguments, or "
                    "a 'Custom.*' / 'Microsoft.VSTS.*' field."
                )
            if name in out.values() or name in out:
                raise ToolError(f"Field '{name}' is given twice.")
            out[name] = value
        return out


def _describe(fields: dict[str, Any]) -> str:
    def short(v: Any) -> str:
        text = str(v)
        return text if len(text) <= 80 else text[:77] + "..."

    return "; ".join(f"{k.split('.')[-1]} = {short(v)}" for k, v in fields.items())


def _check_branch(name: str) -> str:
    """Validate against git's ref-name rules (git check-ref-format), minus exotic cases."""
    name = name.removeprefix("refs/heads/")
    bad = (
        not _BRANCH.match(name)
        or "//" in name
        or "@{" in name
        or name.startswith("-")
        or name.endswith((".", ".lock"))
        or any(part.startswith(".") for part in name.split("/"))
    )
    if bad:
        raise ToolError(f"'{name}' is not a valid branch name.")
    return name


def _create_fields(fields: WorkItemFields) -> dict[str, Any]:
    ado = fields.to_ado()
    if "System.Title" not in ado:
        raise ToolError("A title is required to create a work item.")
    return ado


def _update_fields(fields: WorkItemFields) -> dict[str, Any]:
    ado = fields.to_ado()
    if not ado:
        raise ToolError("Nothing to update: give at least one field.")
    return ado


def register_write_tools(
    mcp: MCPServer,
    run_write: RunWrite[Any],
    *,
    authorize: Callable[[], None],
    confirm_writes: bool,
) -> None:
    def ask(message: str) -> Elicit[Confirm] | Confirm:
        authorize()  # check Ado.Write before asking the user anything
        return Elicit(message, Confirm) if confirm_writes else Confirm(confirm=True)

    def require(answer: Confirm) -> None:
        if not answer.confirm:
            raise ToolError("Cancelled: the user did not confirm the change.")

    # Resolvers receive tool arguments by name. They validate first (so the user is never asked
    # about a request that would be rejected anyway), then build the confirmation question.
    def confirm_create(
        project: str, work_item_type: str, fields: WorkItemFields, parent_id: int | None = None
    ) -> Elicit[Confirm] | Confirm:
        parent = f" under #{parent_id}" if parent_id else ""
        return ask(
            f"Create a {work_item_type} in project '{project}'{parent}: "
            f"{_describe(_create_fields(fields))}?"
        )

    def confirm_update(
        project: str, work_item_id: int, fields: WorkItemFields
    ) -> Elicit[Confirm] | Confirm:
        return ask(
            f"Update work item #{work_item_id} in '{project}': {_describe(_update_fields(fields))}?"
        )

    def confirm_comment(project: str, work_item_id: int, text: str) -> Elicit[Confirm] | Confirm:
        return ask(
            f"Add a comment to work item #{work_item_id} in '{project}': "
            f'"{_describe({"text": text})[7:]}"?'
        )

    def confirm_pr(
        project: str, repository: str, source_branch: str, target_branch: str, title: str
    ) -> Elicit[Confirm] | Confirm:
        src, dst = _check_branch(source_branch), _check_branch(target_branch)
        if src == dst:
            raise ToolError("Source and target branch must differ.")
        return ask(
            f"Create a pull request in '{project}/{repository}' from '{src}' "
            f"into '{dst}' titled \"{title}\"?"
        )

    def confirm_run(
        project: str, pipeline_id: int, branch: str | None = None
    ) -> Elicit[Confirm] | Confirm:
        on = f" on branch '{_check_branch(branch)}'" if branch else " on its default branch"
        return ask(f"Run pipeline {pipeline_id} in '{project}'{on}?")

    @mcp.tool(annotations=CREATES)
    async def create_work_item(
        project: Project,
        work_item_type: Annotated[
            str, Field(min_length=1, max_length=64, description="e.g. Bug, Task, User Story, Epic")
        ],
        fields: WorkItemFields,
        approval: Annotated[Confirm, Resolve(confirm_create)],
        parent_id: Annotated[int | None, Field(ge=1, description="Parent work item ID.")] = None,
    ) -> dict[str, Any]:
        """Create a work item. The user is asked to confirm first."""
        require(approval)
        ado_fields = _create_fields(fields)
        return await run_write(
            lambda c: c.create_work_item(project, work_item_type, ado_fields, parent_id)
        )

    @mcp.tool(annotations=CHANGES)
    async def update_work_item(
        project: Project,
        work_item_id: Annotated[int, Field(ge=1)],
        fields: WorkItemFields,
        approval: Annotated[Confirm, Resolve(confirm_update)],
        expected_revision: Annotated[
            int | None,
            Field(ge=1, description="Fail if the work item changed since this revision."),
        ] = None,
    ) -> dict[str, Any]:
        """Change fields on a work item (state, assignee, priority, ...). Asks to confirm first."""
        require(approval)
        ado_fields = _update_fields(fields)
        return await run_write(
            lambda c: c.update_work_item(project, work_item_id, ado_fields, expected_revision)
        )

    @mcp.tool(annotations=CREATES)
    async def add_work_item_comment(
        project: Project,
        work_item_id: Annotated[int, Field(ge=1)],
        text: Annotated[str, Field(min_length=1, max_length=MAX_TEXT, description="HTML allowed.")],
        approval: Annotated[Confirm, Resolve(confirm_comment)],
    ) -> dict[str, Any]:
        """Add a comment to a work item's discussion. Asks to confirm first."""
        require(approval)
        return await run_write(lambda c: c.add_work_item_comment(project, work_item_id, text))

    @mcp.tool(annotations=CREATES)
    async def create_pull_request(
        project: Project,
        repository: Annotated[str, Field(min_length=1, max_length=256)],
        source_branch: Branch,
        target_branch: Branch,
        title: Annotated[str, Field(min_length=1, max_length=400)],
        approval: Annotated[Confirm, Resolve(confirm_pr)],
        description: Annotated[str | None, Field(max_length=4000)] = None,
        is_draft: bool = False,
    ) -> dict[str, Any]:
        """Open a pull request from one branch into another. Asks to confirm first."""
        require(approval)
        src, dst = _check_branch(source_branch), _check_branch(target_branch)
        if src == dst:
            raise ToolError("Source and target branch must differ.")
        return await run_write(
            lambda c: c.create_pull_request(
                project, repository, src, dst, title, description, is_draft
            )
        )

    @mcp.tool(annotations=CREATES)
    async def run_pipeline(
        project: Project,
        pipeline_id: Annotated[int, Field(ge=1)],
        approval: Annotated[Confirm, Resolve(confirm_run)],
        branch: Annotated[str | None, Field(max_length=250)] = None,
        parameters: Annotated[
            dict[str, str] | None,
            Field(max_length=50, description="Template parameters (name -> value)."),
        ] = None,
    ) -> dict[str, Any]:
        """Queue a pipeline run, optionally on a branch. Asks to confirm first."""
        require(approval)
        ref = _check_branch(branch) if branch else None
        return await run_write(lambda c: c.run_pipeline(project, pipeline_id, ref, parameters))
