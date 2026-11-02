import socket
import ssl
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .config import Settings
from .diagnostics import logger
from .models import CurrentUser, WorkItem

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
        collection_scoped: bool = False,
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
                f"{self.base_url}/_apis/{path}"
                if collection_scoped
                else self.api_url(project, path),
                params={**(params or {}), "api-version": self.settings.version(endpoint)},
                json=body,
                headers={"Content-Type": "application/json-patch+json"} if patch else None,
            )
        except httpx.RequestError as exc:
            hint = "Check server availability, network, VPN and proxy settings."
            if isinstance(exc, httpx.TimeoutException):
                hint = "Request timed out; check connectivity and ADO_HTTP_TIMEOUT."
            elif isinstance(exc, httpx.ProxyError):
                hint = "Proxy connection failed; check HTTPS_PROXY and proxy authentication."
            cause: BaseException | None = exc
            seen: set[int] = set()
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                if isinstance(cause, ssl.SSLError):
                    hint = "TLS failed; check the certificate chain and ADO_CA_BUNDLE."
                    break
                if isinstance(cause, socket.gaierror):
                    hint = "DNS lookup failed; check the server hostname, network and VPN."
                    break
                cause = cause.__cause__ or cause.__context__
            # Do not log exception strings, request bodies, URLs, headers or PATs.
            message = "Azure DevOps connection failed or timed out. " + hint + uncertain
            logger.error(
                "ADO request failed: method=%s host=%s endpoint=%s error=%s. %s",
                method,
                urlsplit(self.base_url).hostname,
                endpoint,
                type(exc).__name__,
                message,
            )
            raise AzureDevOpsError(message) from None
        if not response.is_success:
            hints = {
                400: "Check field names, values, process rules, and the configured API version.",
                401: "Check the PAT and its expiration.",
                403: "Check PAT Work Items permissions and project access.",
                404: "Project, work item, type, or API endpoint was not found.",
                407: "Proxy authentication required; check proxy credentials and HTTPS_PROXY.",
                429: "Rate limit reached; try later.",
            }
            # Raw response bodies/URLs can echo private fields or credentials.
            message = (
                f"Azure DevOps HTTP {response.status_code}. "
                + hints.get(response.status_code, "Request failed.")
                + (uncertain if response.status_code >= 500 else "")
            )
            logger.error(
                "ADO HTTP failure: method=%s host=%s endpoint=%s status=%s. %s",
                method,
                urlsplit(self.base_url).hostname,
                endpoint,
                response.status_code,
                message,
            )
            raise AzureDevOpsError(message)
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except ValueError:
            logger.error(
                "ADO invalid JSON response: method=%s host=%s endpoint=%s",
                method,
                urlsplit(self.base_url).hostname,
                endpoint,
            )
            raise AzureDevOpsError("Azure DevOps returned invalid JSON." + uncertain) from None

    async def current_user(self) -> CurrentUser:
        data = await self.request(
            "GET", "", "connectionData", "connectiondata", collection_scoped=True
        )
        # Return only identity fields, never the raw connection/authentication metadata.
        identity = data.get("authenticatedUser")
        if (
            not isinstance(identity, dict)
            or not identity.get("id")
            or identity.get("isActive") is False
        ):
            raise AzureDevOpsError("Azure DevOps did not return an active authenticated PAT user.")
        unique_name = identity.get("uniqueName")
        if not unique_name:
            properties = identity.get("properties") or {}
            account = properties.get("Account") if isinstance(properties, dict) else None
            unique_name = account.get("$value") if isinstance(account, dict) else None
        return CurrentUser(
            id=identity["id"],
            display_name=identity.get("customDisplayName")
            or identity.get("providerDisplayName")
            or "",
            unique_name=unique_name,
        )

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
