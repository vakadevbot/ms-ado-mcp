"""YAML and classic builds through the Build API only."""

from .read_api import choice, clean, dates, number


class Builds:
    def __init__(self, api, downloads):
        self.api, self.downloads = api, downloads

    async def list_pipeline_definitions(
        self,
        project: str | None = None,
        name: str | None = None,
        folder: str | None = None,
        definition_ids: list[int] | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """List Build definitions (YAML and classic); Azure continuation tokens."""
        ids = ",".join(str(number(i)) for i in definition_ids) if definition_ids else None
        return await self.api.native_page(
            project,
            "build",
            "definitions",
            {"name": name, "path": folder, "definitionIds": ids},
            limit,
            continuation_token,
        )

    async def definition(self, project, definition_id, name):
        if definition_id is not None:
            number(definition_id)
        if name:
            result = await self.list_pipeline_definitions(project, name)
            exact = [d for d in result["items"] if d.get("name") == name]
            if len(exact) != 1 or result["has_more"]:
                raise ValueError("Pipeline name is missing/ambiguous; use definition_id.")
            if definition_id is not None and definition_id != exact[0]["id"]:
                raise ValueError("Pipeline name and ID disagree.")
            definition_id = exact[0]["id"]
        return definition_id

    async def list_pipeline_runs(
        self,
        project: str | None = None,
        definition_id: int | None = None,
        name: str | None = None,
        build_number: str | None = None,
        since: str | None = None,
        before: str | None = None,
        status: str | None = None,
        result: str | None = None,
        branch: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Filter queue time (UTC), never start/finish time. History requires since.

        status: inProgress/completed/cancelling/postponed/notStarted/all.
        result: succeeded/partiallySucceeded/failed/canceled. Keep filters across pages.
        """
        choice(status, {"inProgress", "completed", "cancelling", "postponed", "notStarted", "all"})
        choice(result, {"succeeded", "partiallySucceeded", "failed", "canceled"})
        since, before = dates(
            since, before, status not in {"inProgress", "notStarted", "postponed", "cancelling"}
        )
        definition_id = await self.definition(project, definition_id, name)
        return await self.api.native_page(
            project,
            "build",
            "builds",
            {
                "definitions": definition_id,
                "buildNumber": build_number,
                "minTime": since,
                "maxTime": before,
                "statusFilter": status,
                "resultFilter": result,
                "branchName": branch,
                "queryOrder": "queueTimeDescending",
            },
            limit,
            continuation_token,
        )

    async def get_pipeline_run(self, build_id: int, project: str | None = None) -> dict:
        """Read one build by ID; no date required."""
        data, _ = await self.api.get(project, "build", f"builds/{number(build_id)}")
        return clean(data)

    async def get_pipeline_run_steps(
        self,
        build_id: int,
        project: str | None = None,
        timeline_id: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Timeline records preserve parentId, attempt, previousAttempts and log.id.

        Follow details.id as timeline_id to inspect nested timelines. Snapshot pages can
        expire when a running timeline changes; restart in that case.
        """
        from .read_api import segment

        path = f"builds/{number(build_id)}/timeline"
        if timeline_id:
            path += "/" + segment(timeline_id)
        data, _ = await self.api.get(project, "build", path)
        records = data.get("records", [])
        binding = self.api.binding(self.api.project(project), path, records)
        return self.api.slice(
            records,
            binding,
            limit,
            continuation_token,
            timeline_id=data.get("id"),
            change_id=data.get("changeId"),
        )

    async def get_pipeline_step_log(
        self,
        build_id: int,
        log_id: int,
        project: str | None = None,
        start_line: int = 0,
        line_count: int = 200,
        max_bytes: int = 65536,
    ) -> dict:
        """Read a zero-based line window; truncated output is explicitly marked."""
        number(start_line, 1000000, 0)
        number(line_count, 1000)
        return await self.api.text(
            project,
            "build",
            f"builds/{number(build_id)}/logs/{number(log_id)}",
            {"startLine": start_line, "endLine": start_line + line_count},
            start_line,
            line_count,
            max_bytes,
        )

    async def list_pipeline_artifacts(
        self,
        build_id: int,
        project: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """List artifact metadata. Download URLs are omitted."""
        data, _ = await self.api.get(project, "build", f"builds/{number(build_id)}/artifacts")
        items = data.get("value", [])
        return self.api.slice(
            items,
            self.api.binding(self.api.project(project), build_id, items),
            limit,
            continuation_token,
        )

    async def download_pipeline_artifact(
        self, build_id: int, artifact_name: str, project: str | None = None
    ) -> dict:
        """Download a Container artifact ZIP to configured storage; other types fail explicitly."""
        path = f"builds/{number(build_id)}/artifacts"
        data, _ = await self.api.get(project, "build", path, {"artifactName": artifact_name})
        if data.get("resource", {}).get("type", "").lower() != "container":
            raise ValueError("Unsupported artifact type; only Build Container ZIP is supported.")
        return await self.downloads.fetch(
            project, "build", path, {"artifactName": artifact_name, "$format": "zip"}
        )


BUILD_TOOLS = tuple(
    name for name in Builds.__dict__ if name.startswith(("list_", "get_", "download_"))
)
