"""Thin async client for the Azure DevOps REST API (read operations used in M1).

Returns trimmed dicts: only the fields an agent needs, which keeps results small and limits
personal data (identities are reduced to display names).
"""

from __future__ import annotations

import json as _json
from typing import Any
from urllib.parse import quote

import httpx

WORK_ITEM_FIELDS = {
    "System.Title": "title",
    "System.WorkItemType": "type",
    "System.State": "state",
    "System.Reason": "reason",
    "System.AreaPath": "area_path",
    "System.IterationPath": "iteration_path",
    "Microsoft.VSTS.Common.Priority": "priority",
    "Microsoft.VSTS.Common.Severity": "severity",
    "System.Tags": "tags",
    "System.CreatedDate": "created",
    "System.ChangedDate": "changed",
}
WORK_ITEM_BATCH = 200  # ADO limit for workitems?ids=


class AdoError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def _seg(value: str | int) -> str:
    """Encode one URL path segment (blocks path traversal / injection via names)."""
    return quote(str(value), safe="")


def _display(identity: Any) -> str | None:
    return identity.get("displayName") if isinstance(identity, dict) else identity


class AdoClient:
    def __init__(
        self, http: httpx.AsyncClient, org_url: str, auth_header: str, api_version: str = "7.1"
    ):
        self._http = http
        self._org = org_url.rstrip("/")
        self._headers = {"Authorization": auth_header, "Accept": "application/json"}
        self._v = api_version

    # -- transport ---------------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        json_patch: list[dict[str, Any]] | None = None,
        api_version: str | None = None,
    ) -> Any:
        query = {
            "api-version": api_version or self._v,
            **{k: v for k, v in (params or {}).items() if v is not None},
        }
        headers = dict(self._headers)
        content = None
        if json_patch is not None:  # work item create/update use JSON Patch
            headers["Content-Type"] = "application/json-patch+json"
            content = _json.dumps(json_patch).encode()
        resp = await self._http.request(
            method,
            f"{self._org}/{path}",
            params=query,
            json=json,
            content=content,
            headers=headers,
        )
        # ADO answers bad credentials with 203 + an HTML sign-in page.
        if resp.status_code == 203 or "text/html" in resp.headers.get("content-type", ""):
            raise AdoError(401, "Azure DevOps did not accept the credentials")
        if resp.status_code >= 400:
            try:
                message = str(resp.json().get("message", ""))
            except ValueError:
                message = ""
            raise AdoError(resp.status_code, message[:300] or resp.reason_phrase)
        return resp.json()

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params)

    # -- projects & repos ----------------------------------------------------------------------
    async def list_projects(self, top: int) -> list[dict[str, Any]]:
        data = await self._get("_apis/projects", **{"$top": top})
        return [
            {
                "id": p["id"],
                "name": p["name"],
                "description": p.get("description"),
                "state": p.get("state"),
                "last_updated": p.get("lastUpdateTime"),
            }
            for p in data.get("value", [])
        ]

    async def list_repositories(self, project: str) -> list[dict[str, Any]]:
        data = await self._get(f"{_seg(project)}/_apis/git/repositories")
        return [self._repo(r) for r in data.get("value", [])]

    async def get_repository(self, project: str, repository: str) -> dict[str, Any]:
        return self._repo(
            await self._get(f"{_seg(project)}/_apis/git/repositories/{_seg(repository)}")
        )

    @staticmethod
    def _repo(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": r["id"],
            "name": r["name"],
            "default_branch": r.get("defaultBranch"),
            "size_bytes": r.get("size"),
            "web_url": r.get("webUrl"),
            "is_disabled": r.get("isDisabled", False),
        }

    async def list_branches(self, project: str, repository: str, top: int) -> list[dict[str, Any]]:
        data = await self._get(
            f"{_seg(project)}/_apis/git/repositories/{_seg(repository)}/refs",
            filter="heads/",
            **{"$top": top},
        )
        return [
            {
                "name": b["name"].removeprefix("refs/heads/"),
                "commit": b.get("objectId"),
                "creator": _display(b.get("creator")),
            }
            for b in data.get("value", [])
        ]

    async def list_commits(
        self, project: str, repository: str, branch: str | None, top: int
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"searchCriteria.$top": top}
        if branch:
            params["searchCriteria.itemVersion.version"] = branch
            params["searchCriteria.itemVersion.versionType"] = "branch"
        data = await self._get(
            f"{_seg(project)}/_apis/git/repositories/{_seg(repository)}/commits", **params
        )
        return [
            {
                "commit": c["commitId"],
                "author": (c.get("author") or {}).get("name"),
                "date": (c.get("author") or {}).get("date"),
                "message": c.get("comment"),
            }
            for c in data.get("value", [])
        ]

    # -- pull requests --------------------------------------------------------------------------
    async def list_pull_requests(
        self, project: str, repository: str | None, status: str, top: int
    ) -> list[dict[str, Any]]:
        params = {"searchCriteria.status": status, "$top": top}
        path = (
            f"{_seg(project)}/_apis/git/repositories/{_seg(repository)}/pullrequests"
            if repository
            else f"{_seg(project)}/_apis/git/pullrequests"
        )
        data = await self._get(path, **params)
        return [self._pr(p) for p in data.get("value", [])]

    async def get_pull_request(self, project: str, pull_request_id: int) -> dict[str, Any]:
        pr = await self._get(f"{_seg(project)}/_apis/git/pullrequests/{_seg(pull_request_id)}")
        out = self._pr(pr)
        out["description"] = pr.get("description")
        out["reviewers"] = [
            {
                "name": r.get("displayName"),
                "vote": r.get("vote"),
                "required": r.get("isRequired", False),
            }
            for r in pr.get("reviewers", [])
        ]
        return out

    @staticmethod
    def _pr(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": p["pullRequestId"],
            "title": p.get("title"),
            "status": p.get("status"),
            "is_draft": p.get("isDraft", False),
            "created_by": _display(p.get("createdBy")),
            "created": p.get("creationDate"),
            "repository": (p.get("repository") or {}).get("name"),
            "source_branch": (p.get("sourceRefName") or "").removeprefix("refs/heads/"),
            "target_branch": (p.get("targetRefName") or "").removeprefix("refs/heads/"),
            "merge_status": p.get("mergeStatus"),
        }

    # -- work items -----------------------------------------------------------------------------
    async def get_work_items(
        self, project: str, ids: list[int], include_description: bool = False
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for start in range(0, len(ids), WORK_ITEM_BATCH):
            chunk = ids[start : start + WORK_ITEM_BATCH]
            data = await self._get(
                f"{_seg(project)}/_apis/wit/workitems",
                ids=",".join(str(i) for i in chunk),
                errorPolicy="omit",
            )
            items += [self._work_item(w, include_description) for w in data.get("value", []) if w]
        return items

    @staticmethod
    def _work_item(w: dict[str, Any], include_description: bool) -> dict[str, Any]:
        f = w.get("fields", {})
        out: dict[str, Any] = {"id": w["id"]}
        out.update({short: f[name] for name, short in WORK_ITEM_FIELDS.items() if name in f})
        out["assigned_to"] = _display(f.get("System.AssignedTo"))
        if include_description:
            out["description"] = f.get("System.Description")
            out["acceptance_criteria"] = f.get("Microsoft.VSTS.Common.AcceptanceCriteria")
        return out

    async def query_work_item_ids(self, project: str, wiql: str, top: int) -> list[int]:
        data = await self._request(
            "POST", f"{_seg(project)}/_apis/wit/wiql", params={"$top": top}, json={"query": wiql}
        )
        return [w["id"] for w in data.get("workItems", [])]

    # -- pipelines & builds ---------------------------------------------------------------------
    async def list_pipelines(self, project: str, top: int) -> list[dict[str, Any]]:
        data = await self._get(f"{_seg(project)}/_apis/pipelines", **{"$top": top})
        return [
            {"id": p["id"], "name": p["name"], "folder": p.get("folder")}
            for p in data.get("value", [])
        ]

    async def list_pipeline_runs(self, project: str, pipeline_id: int) -> list[dict[str, Any]]:
        data = await self._get(f"{_seg(project)}/_apis/pipelines/{_seg(pipeline_id)}/runs")
        return [self._run(r) for r in data.get("value", [])]

    async def get_pipeline_run(self, project: str, pipeline_id: int, run_id: int) -> dict[str, Any]:
        return self._run(
            await self._get(
                f"{_seg(project)}/_apis/pipelines/{_seg(pipeline_id)}/runs/{_seg(run_id)}"
            )
        )

    @staticmethod
    def _run(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": r["id"],
            "name": r.get("name"),
            "state": r.get("state"),
            "result": r.get("result"),
            "created": r.get("createdDate"),
            "finished": r.get("finishedDate"),
            "web_url": ((r.get("_links") or {}).get("web") or {}).get("href"),
        }

    async def list_builds(
        self,
        project: str,
        top: int,
        definition_id: int | None,
        status: str | None,
        branch: str | None,
    ) -> list[dict[str, Any]]:
        data = await self._get(
            f"{_seg(project)}/_apis/build/builds",
            **{
                "$top": top,
                "definitions": definition_id,
                "statusFilter": status,
                "branchName": f"refs/heads/{branch}" if branch else None,
            },
        )
        return [
            {
                "id": b["id"],
                "number": b.get("buildNumber"),
                "status": b.get("status"),
                "result": b.get("result"),
                "pipeline": (b.get("definition") or {}).get("name"),
                "branch": (b.get("sourceBranch") or "").removeprefix("refs/heads/"),
                "requested_for": _display(b.get("requestedFor")),
                "started": b.get("startTime"),
                "finished": b.get("finishTime"),
            }
            for b in data.get("value", [])
        ]

    # -- write operations (need the Ado.Write role; see write_tools.py) --------------------------
    def _work_item_url(self, work_item_id: int) -> str:
        return f"{self._org}/_apis/wit/workItems/{int(work_item_id)}"

    async def create_work_item(
        self, project: str, work_item_type: str, fields: dict[str, Any], parent_id: int | None
    ) -> dict[str, Any]:
        patch: list[dict[str, Any]] = [
            {"op": "add", "path": f"/fields/{name}", "value": value}
            for name, value in fields.items()
        ]
        if parent_id is not None:
            patch.append(
                {
                    "op": "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "System.LinkTypes.Hierarchy-Reverse",
                        "url": self._work_item_url(parent_id),
                    },
                }
            )
        w = await self._request(
            "POST", f"{_seg(project)}/_apis/wit/workitems/${_seg(work_item_type)}", json_patch=patch
        )
        return self._work_item(w, include_description=False) | {
            "revision": w.get("rev"),
            "url": _web_url(w),
        }

    async def update_work_item(
        self,
        project: str,
        work_item_id: int,
        fields: dict[str, Any],
        expected_revision: int | None,
    ) -> dict[str, Any]:
        patch: list[dict[str, Any]] = []
        if expected_revision is not None:  # optimistic concurrency: fail if changed meanwhile
            patch.append({"op": "test", "path": "/rev", "value": expected_revision})
        patch += [{"op": "add", "path": f"/fields/{n}", "value": v} for n, v in fields.items()]
        w = await self._request(
            "PATCH", f"{_seg(project)}/_apis/wit/workitems/{_seg(work_item_id)}", json_patch=patch
        )
        return self._work_item(w, include_description=False) | {
            "revision": w.get("rev"),
            "url": _web_url(w),
        }

    async def add_work_item_comment(
        self, project: str, work_item_id: int, text: str
    ) -> dict[str, Any]:
        c = await self._request(
            "POST",
            f"{_seg(project)}/_apis/wit/workItems/{_seg(work_item_id)}/comments",
            json={"text": text},
            api_version="7.1-preview.4",
        )
        return {
            "comment_id": c.get("id"),
            "work_item_id": work_item_id,
            "created": c.get("createdDate"),
        }

    async def create_pull_request(
        self,
        project: str,
        repository: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str | None,
        is_draft: bool,
    ) -> dict[str, Any]:
        pr = await self._request(
            "POST",
            f"{_seg(project)}/_apis/git/repositories/{_seg(repository)}/pullrequests",
            json={
                "sourceRefName": f"refs/heads/{source_branch}",
                "targetRefName": f"refs/heads/{target_branch}",
                "title": title,
                "description": description or "",
                "isDraft": is_draft,
            },
        )
        return self._pr(pr)

    async def run_pipeline(
        self,
        project: str,
        pipeline_id: int,
        branch: str | None,
        parameters: dict[str, str] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if branch:
            body["resources"] = {"repositories": {"self": {"refName": f"refs/heads/{branch}"}}}
        if parameters:
            body["templateParameters"] = parameters
        run = await self._request(
            "POST", f"{_seg(project)}/_apis/pipelines/{_seg(pipeline_id)}/runs", json=body
        )
        return self._run(run)


def _web_url(w: dict[str, Any]) -> str | None:
    return ((w.get("_links") or {}).get("html") or {}).get("href")
