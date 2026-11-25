"""Bounded GET-only transport and query helpers. No URL is accepted from MCP."""

import base64
import hashlib
import hmac
import json
import secrets
from urllib.parse import quote

import httpx

from .client import AzureDevOpsError
from .diagnostics import logger
from .service import creation_timestamp

API_VERSIONS = {"build": "6.0", "release": "6.0", "git": "6.0", "policy": "6.0-preview.1"}
MAX_JSON = 8 * 1024 * 1024


def api_version(area, path):
    if area == "git" and (path.endswith("/statuses") or "/trees/" in path):
        return "6.0-preview.1"
    if area == "release" and path.endswith("/logs"):
        return "6.0-preview.2"
    return API_VERSIONS[area]


def segment(value: str | int) -> str:
    value = str(value)
    if not value or value in {".", ".."} or any(c in value for c in "/\\\x00"):
        raise ValueError("Invalid resource identifier.")
    return quote(value, safe="")


def number(value: int, maximum: int = 2147483647, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"Expected an integer between {minimum} and {maximum}.")
    return value


def dates(since: str | None, before: str | None, required: bool = True) -> tuple:
    if required and not since:
        raise ValueError("A lower time bound (since) is required for historical queries.")
    start = creation_timestamp(since) if since else None
    end = creation_timestamp(before) if before else None
    if start and end and start >= end:
        raise ValueError("since must precede before.")
    return tuple(d.isoformat().replace("+00:00", "Z") if d else None for d in (start, end))


def choice(value: str | None, allowed: set[str]) -> str | None:
    if value is not None and value not in allowed:
        raise ValueError("Unsupported filter value; consult the tool description.")
    return value


def page(items: list, cursor=None, *, incomplete=False, **metadata) -> dict:
    return {
        "items": items,
        "continuation_token": cursor,
        "has_more": cursor is not None,
        "incomplete": incomplete,
        "untrusted_data": True,
        **metadata,
    }


def clean(value):
    """Remove credential-bearing URLs and secret variables from structured responses."""
    if isinstance(value, dict):
        if value.get("isSecret") is True:
            return {k: clean(v) for k, v in value.items() if k != "value"}
        return {
            k: clean(v)
            for k, v in value.items()
            if k.lower() not in {"authorization", "accesstoken", "downloadurl"}
        }
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return value.split("?", 1)[0].split("#", 1)[0]
    return value


class ReadAPI:
    def __init__(self, work_items):
        self.work_items = work_items
        self.client = work_items.client
        self.settings = self.client.settings
        self._key = secrets.token_bytes(32)

    def project(self, project):
        return self.work_items.policy(project).name

    def url(self, project, area, path):
        project = self.project(project)
        base = self.settings.release_base_url if area == "release" else self.client.base_url
        return f"{base}/{segment(project)}/_apis/{area}/{path}"

    def fail(self, area, operation, status=None, reason="Request failed"):
        message = f"{area} {operation}: HTTP {status or 'unavailable'}. {reason}."
        logger.error("%s", message)
        return AzureDevOpsError(message)

    def check(self, response, area, operation):
        if response.status_code == 206:
            raise self.fail(
                area, operation, 206, "Unexpected partial response; completeness unknown"
            )
        if response.is_success:
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise self.fail(
                    area,
                    operation,
                    response.status_code,
                    "Unexpected HTTP compression; bounded identity transfer required",
                )
            return
        hint = {
            401: "Check PAT expiry",
            403: "Check read scope and project access",
            404: "Resource or API version unavailable",
            429: "Rate limited; retry later",
        }
        raise self.fail(
            area,
            operation,
            response.status_code,
            hint.get(response.status_code, "Request rejected"),
        )

    async def read_bytes(
        self,
        project,
        area,
        path,
        params=None,
        *,
        limit=MAX_JSON,
        partial=False,
        accept="application/json",
    ):
        url = self.url(project, area, path)
        try:
            async with self.client.http.stream(
                "GET",
                url,
                params={**(params or {}), "api-version": api_version(area, path)},
                headers={"Accept": accept, "Accept-Encoding": "identity"},
            ) as response:
                self.check(response, area, "read")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    room = limit + 1 - len(data)
                    data.extend(chunk[:room])
                    if len(data) > limit:
                        if not partial:
                            raise self.fail(
                                area,
                                "read",
                                response.status_code,
                                "Response exceeds safety limit; narrow the query",
                            )
                        return bytes(data[:limit]), dict(response.headers), True
                return bytes(data), dict(response.headers), False
        except (httpx.RequestError, httpx.InvalidURL):
            raise self.fail(area, "read", reason="Network/TLS/timeout failure") from None

    async def get(self, project, area, path, params=None):
        raw, headers, _ = await self.read_bytes(project, area, path, params)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            raise self.fail(area, "read", reason="Invalid JSON response") from None
        self.check_project(data, self.project(project))
        return data, headers

    def check_project(self, data, project):
        if isinstance(data, dict):
            owner = data.get("project") or data.get("projectReference")
            if isinstance(owner, dict) and owner.get("name"):
                if owner["name"].casefold() != project.casefold():
                    raise self.fail("scope", "read", reason="Cross-project resource rejected")
            for key, value in data.items():
                if key not in {"artifacts", "definitionReference"}:
                    self.check_project(value, project)
        elif isinstance(data, list):
            for value in data:
                self.check_project(value, project)

    async def native_page(self, project, area, path, params, limit, continuation_token):
        number(limit, 200)
        binding = self.binding(
            self.settings.organization, self.project(project), area, path, params
        )
        azure_token = continuation_token
        if self.settings.organization is not None and continuation_token:
            azure_token = self.decode(continuation_token, binding)
        data, headers = await self.get(
            project,
            area,
            path,
            {
                **{k: v for k, v in params.items() if v is not None},
                "$top": limit,
                **({"continuationToken": azure_token} if azure_token else {}),
            },
        )
        cursor = headers.get("x-ms-continuationtoken") or None
        if cursor is not None and cursor == azure_token:
            raise self.fail(area, "list", reason="Non-advancing continuation token")
        if cursor is not None and self.settings.organization is not None:
            cursor = self.encode(binding, cursor)
        return page(
            clean(data.get("value", [])),
            cursor,
            query=clean(params),
            api_version=api_version(area, path),
        )

    def encode(self, binding, state):
        raw = json.dumps([binding, state], sort_keys=True, separators=(",", ":")).encode()
        if len(raw) > 140000:
            raise ValueError("Cursor state too large; narrow the directory/query.")
        return base64.urlsafe_b64encode(hmac.digest(self._key, raw, "sha256") + raw).decode()

    def decode(self, token, binding):
        try:
            if len(token) > 200000:
                raise ValueError
            raw = base64.urlsafe_b64decode(token)
            if not hmac.compare_digest(raw[:32], hmac.digest(self._key, raw[32:], "sha256")):
                raise ValueError
            saved, state = json.loads(raw[32:])
            if saved != binding:
                raise ValueError
            return state
        except (ValueError, TypeError):
            raise ValueError(
                "Invalid/expired cursor or changed query; restart the query."
            ) from None

    def binding(self, *parts):
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()

    def slice(self, items, binding, limit=100, continuation_token=None, **metadata):
        number(limit, 200)
        offset = self.decode(continuation_token, binding) if continuation_token else 0
        end = offset + limit
        return page(
            clean(items[offset:end]),
            self.encode(binding, end) if end < len(items) else None,
            **metadata,
        )

    async def text(
        self, project, area, path, params=None, start_line=0, line_count=200, max_bytes=65536
    ):
        number(start_line, 1000000, 0)
        number(line_count, 1000)
        number(max_bytes, 262144)
        raw, _, truncated = await self.read_bytes(
            project, area, path, params, limit=max_bytes, partial=True, accept="text/plain"
        )
        try:
            content = raw.decode("utf-8")
        except UnicodeError:
            content = raw.decode("utf-8", errors="replace")
        lines = content.splitlines(keepends=True)
        selected = lines[:line_count]
        more = truncated or len(lines) > line_count
        return {
            "text": "".join(selected),
            "start_line": start_line,
            "next_start_line": start_line + len(selected) if more and not truncated else None,
            "truncated": more,
            "byte_limit_reached": truncated,
            "untrusted_data": True,
        }
