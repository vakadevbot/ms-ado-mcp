import ssl
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .config import Settings
from .models import WorkItem

PARENT = "System.LinkTypes.Hierarchy-Reverse"
CHILD = "System.LinkTypes.Hierarchy-Forward"


class AzureDevOpsError(RuntimeError):
    """Safe error text suitable for an MCP tool result."""


class AzureDevOpsClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.base_url = settings.url.rstrip("/")
        verify = (
            ssl.create_default_context(cafile=str(settings.ca_bundle))
            if settings.ca_bundle
            else True
        )
        self.http = httpx.AsyncClient(
            auth=httpx.BasicAuth("", settings.pat),
            timeout=settings.timeout,
            verify=verify,
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def close(self) -> None:
        await self.http.aclose()

    def api_url(self, project: str, path: str) -> str:
        return f"{self.base_url}/{quote(project, safe='')}/_apis/wit/{path}"

    def item_url(self, project: str, item_id: int) -> str:
        return self.api_url(project, f"workitems/{item_id}")

    async def request(
        self,
        method: str,
        project: str,
        path: str,
        endpoint: str,
        *,
        params: dict | None = None,
        body: Any = None,
        patch: bool = False,
    ) -> dict:
        is_create = patch
        uncertain = (
            " Creation may have succeeded; search for the item before retrying."
            if is_create
            else ""
        )
        try:
            response = await self.http.request(
                method,
                self.api_url(project, path),
                params={**(params or {}), "api-version": self.settings.version(endpoint)},
                json=body,
                headers={"Content-Type": "application/json-patch+json"} if patch else None,
            )
        except httpx.RequestError:
            raise AzureDevOpsError(
                "Azure DevOps connection failed or timed out." + uncertain
            ) from None
        if not response.is_success:
            hints = {
                400: "Check field names, values, process rules, and the configured API version.",
                401: "Check the PAT and its expiration.",
                403: "Check PAT Work Items permissions and project access.",
                404: "Project, work item, type, or API endpoint was not found.",
                429: "Rate limit reached; try later.",
            }
            # Raw response bodies/URLs can echo private fields or credentials.
            raise AzureDevOpsError(
                f"Azure DevOps HTTP {response.status_code}. "
                + hints.get(response.status_code, "Request failed.")
                + (uncertain if response.status_code >= 500 else "")
            )
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except ValueError:
            raise AzureDevOpsError("Azure DevOps returned invalid JSON." + uncertain) from None

    @staticmethod
    def checked_item(data: dict, project: str) -> WorkItem:
        item = WorkItem.model_validate(data)
        if item.fields.get("System.TeamProject") != project:
            raise AzureDevOpsError(
                "The work item belongs to another project. Cross-project links are not supported."
            )
        return item

    async def get_item(self, project: str, item_id: int) -> WorkItem:
        data = await self.request(
            "GET",
            project,
            f"workitems/{item_id}",
            "workitems",
            params={"$expand": "Relations"},
        )
        return self.checked_item(data, project)

    async def get_items(
        self, project: str, ids: list[int], *, as_of: str | None = None
    ) -> list[WorkItem]:
        result: dict[int, WorkItem] = {}
        for offset in range(0, len(ids), 200):
            batch = ids[offset : offset + 200]
            body: dict = {"ids": batch, "errorPolicy": "Fail"}
            if as_of:
                body["asOf"] = as_of
            data = await self.request(
                "POST",
                project,
                "workitemsbatch",
                "workitemsbatch",
                body=body,
            )
            for raw in data["value"]:
                item = self.checked_item(raw, project)
                result[item.id] = item
        if set(ids) != result.keys():
            raise AzureDevOpsError("Azure DevOps returned an incomplete work item batch.")
        return [result[item_id] for item_id in ids]

    async def query(self, project: str, wiql: str, limit: int) -> dict:
        return await self.request(
            "POST",
            project,
            "wiql",
            "wiql",
            params={"$top": limit},
            body={"query": wiql},
        )

    async def states(self, project: str, work_item_type: str) -> list[dict]:
        data = await self.request(
            "GET",
            project,
            f"workitemtypes/{quote(work_item_type, safe='')}/states",
            "states",
        )
        return data["value"]

    def relation_id(self, url: str) -> int:
        # Extract an ID, never follow a URL supplied in a work item's relation.
        base, linked = urlsplit(self.base_url), urlsplit(url)
        prefix = base.path.rstrip("/") + "/"
        path = linked.path
        if (
            (linked.scheme, linked.netloc) != (base.scheme, base.netloc)
            or not path.startswith(prefix)
            or linked.query
            or linked.fragment
        ):
            raise AzureDevOpsError("Hierarchy link points outside the configured collection.")
        pieces = path.rstrip("/").rsplit("/", 3)
        if len(pieces) != 4 or pieces[-3:-1] != ["wit", "workItems"]:
            # Azure DevOps also uses lowercase workitems.
            if len(pieces) != 4 or [p.lower() for p in pieces[-3:-1]] != ["wit", "workitems"]:
                raise AzureDevOpsError("Invalid work item hierarchy link.")
        try:
            item_id = int(pieces[-1])
            if item_id <= 0:
                raise ValueError
            return item_id
        except ValueError:
            raise AzureDevOpsError("Invalid work item hierarchy ID.") from None

    async def create(self, project: str, kind: str, item: WorkItem) -> WorkItem:
        patch = [
            {"op": "add", "path": f"/fields/{name}", "value": value}
            for name, value in item.fields.items()
            if name not in {"System.WorkItemType", "System.TeamProject"}
        ]
        patch.extend(
            {"op": "add", "path": "/relations/-", "value": r.model_dump()} for r in item.relations
        )
        raw = await self.request(
            "POST",
            project,
            f"workitems/${quote(kind, safe='')}",
            "workitems",
            body=patch,
            patch=True,
        )
        return self.checked_item(raw, project)
