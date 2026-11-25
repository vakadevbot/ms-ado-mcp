"""Classic Releases; never YAML stages."""

from .read_api import choice, clean, dates, number


class Releases:
    def __init__(self, api, downloads):
        self.api, self.downloads = api, downloads

    async def list_release_definitions(
        self,
        project: str | None = None,
        name: str | None = None,
        folder: str | None = None,
        definition_ids: list[int] | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Search classic Release definitions; keep filters with the Azure continuation token."""
        ids = ",".join(str(number(i)) for i in definition_ids) if definition_ids else None
        return await self.api.native_page(
            project,
            "release",
            "definitions",
            {"searchText": name, "path": folder, "definitionIdFilter": ids},
            limit,
            continuation_token,
        )

    async def list_releases(
        self,
        since: str,
        project: str | None = None,
        before: str | None = None,
        definition_id: int | None = None,
        name: str | None = None,
        search_text: str | None = None,
        status: str | None = None,
        environment_status: int | None = None,
        definition_environment_id: int | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Classic release creation time. status=active/draft/abandoned, distinct from
        environment_status (Azure EnvironmentStatus bitmask) requires definition_environment_id.
        since is required.
        """
        since, before = dates(since, before)
        choice(status, {"active", "draft", "abandoned"})
        if definition_id is not None:
            number(definition_id)
        if environment_status is not None:
            number(environment_status, 255)
            if definition_environment_id is None:
                raise ValueError("environment_status requires definition_environment_id.")
        if definition_environment_id is not None:
            number(definition_environment_id)
        if name:
            definitions = await self.list_release_definitions(project, name)
            exact = [d for d in definitions["items"] if d.get("name") == name]
            if len(exact) != 1 or definitions["has_more"]:
                raise ValueError("Release definition name is missing/ambiguous; use definition_id.")
            if definition_id is not None and definition_id != exact[0]["id"]:
                raise ValueError("Release definition name and ID disagree.")
            definition_id = exact[0]["id"]
        return await self.api.native_page(
            project,
            "release",
            "releases",
            {
                "definitionId": definition_id,
                "searchText": search_text,
                "statusFilter": status,
                "environmentStatusFilter": environment_status,
                "definitionEnvironmentId": definition_environment_id,
                "minCreatedTime": since,
                "maxCreatedTime": before,
                "queryOrder": "descending",
            },
            limit,
            continuation_token,
        )

    async def get_release(self, release_id: int, project: str | None = None) -> dict:
        """Read classic release details; no date required. Secret variables are redacted."""
        data, _ = await self.api.get(
            project, "release", f"releases/{number(release_id)}", {"$expand": "all"}
        )
        return clean(data)

    async def get_release_steps(
        self,
        release_id: int,
        project: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Environments, deployment attempts, phases, jobs and tasks as related records.
        releaseDeployPhaseId identifies the attempt used by get_release_task_log.
        """
        data = await self.get_release(release_id, project)
        records = []
        for env in data.get("environments", []):
            records.append(
                {
                    "kind": "environment",
                    "environment_id": env["id"],
                    "name": env.get("name"),
                    "status": env.get("status"),
                }
            )
            for attempt in env.get("deploySteps", []):
                context = {
                    "environment_id": env["id"],
                    "deployment_id": attempt.get("id"),
                    "attempt": attempt.get("attempt"),
                }
                records.append(
                    {
                        **context,
                        "kind": "deployment",
                        **{k: v for k, v in attempt.items() if k != "releaseDeployPhases"},
                    }
                )
                for phase in attempt.get("releaseDeployPhases", []):
                    context2 = {**context, "release_deploy_phase_id": phase["id"]}
                    records.append(
                        {
                            **context2,
                            "kind": "phase",
                            **{k: v for k, v in phase.items() if k != "deploymentJobs"},
                        }
                    )
                    for index, job in enumerate(phase.get("deploymentJobs", [])):
                        context3 = {**context2, "job_index": index}
                        records.append({**context3, "kind": "job", "job": job.get("job")})
                        for task in job.get("tasks", []):
                            records.append({**context3, "kind": "task", "task": task})
        return self.api.slice(
            records,
            self.api.binding(self.api.project(project), release_id, records),
            limit,
            continuation_token,
        )

    async def get_release_task_log(
        self,
        release_id: int,
        environment_id: int,
        release_deploy_phase_id: int,
        task_id: int,
        project: str | None = None,
        start_line: int = 0,
        line_count: int = 200,
        max_bytes: int = 65536,
    ) -> dict:
        """Read a classic release task log from one deployment phase/attempt."""
        path = (
            f"releases/{number(release_id)}/environments/{number(environment_id)}/"
            f"deployPhases/{number(release_deploy_phase_id)}/tasks/{number(task_id)}/logs"
        )
        number(start_line, 1000000, 0)
        number(line_count, 1000)
        return await self.api.text(
            project,
            "release",
            path,
            {"startLine": start_line, "endLine": start_line + line_count},
            start_line,
            line_count,
            max_bytes,
        )

    async def download_release_logs(self, release_id: int, project: str | None = None) -> dict:
        """Download a classic release logs ZIP without extraction."""
        return await self.downloads.fetch(
            project, "release", f"releases/{number(release_id)}/logs", {}
        )

    async def list_release_artifacts(
        self,
        release_id: int,
        project: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Artifact sources with definitionReference.version.id (build ID for Build sources)."""
        data = await self.get_release(release_id, project)
        items = data.get("artifacts", [])
        return self.api.slice(
            items,
            self.api.binding(self.api.project(project), release_id, items),
            limit,
            continuation_token,
        )


RELEASE_TOOLS = tuple(
    name for name in Releases.__dict__ if name.startswith(("list_", "get_", "download_"))
)
