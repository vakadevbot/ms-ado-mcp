"""Read-only PR operations. API 6.0 date filtering is explicitly client-side."""

from typing import Literal

from .client import AzureDevOpsError
from .read_api import choice, clean, dates, number, page
from .service import creation_timestamp


class PullRequests:
    def __init__(self, api, git):
        self.api, self.git = api, git

    async def search_pull_requests(
        self,
        project: str | None = None,
        repository: str | None = None,
        status: str = "active",
        creator_id: str | None = None,
        reviewer_id: str | None = None,
        source_branch: str | None = None,
        target_branch: str | None = None,
        since: str | None = None,
        before: str | None = None,
        personal: Literal["created", "review"] | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Search PRs; active needs no date, completed/abandoned/all require since.
        Dates filter creationDate locally: API 6.0 lacks server date filtering. Each call
        scans one Azure page, possibly yielding zero matches and a continuation token.
        personal=review finds active PRs with your reviewer vote=0 (awaiting your review).
        Offset pagination is not a snapshot; changing PR membership can shift pages.
        """
        number(limit, 200)
        project = self.api.project(project)
        choice(status, {"active", "completed", "abandoned", "all"})
        choice(personal, {"created", "review"})
        since, before = dates(since, before, status != "active")
        if personal:
            identity = await self.api.client.current_user()
            if personal == "created":
                if creator_id and creator_id != identity.id:
                    raise ValueError("creator_id conflicts with personal mode.")
                creator_id = identity.id
            else:
                if status != "active" or (reviewer_id and reviewer_id != identity.id):
                    raise ValueError("Review mode requires active status and the PAT owner.")
                reviewer_id = identity.id
        path = (
            (await self.git.repo(repository, project)) + "/pullrequests"
            if repository
            else "pullrequests"
        )
        filters = {
            "searchCriteria.status": status,
            "searchCriteria.creatorId": creator_id,
            "searchCriteria.reviewerId": reviewer_id,
            "searchCriteria.sourceRefName": source_branch,
            "searchCriteria.targetRefName": target_branch,
        }
        binding = self.api.binding(project, path, filters, since, before, personal)
        skip = self.api.decode(continuation_token, binding) if continuation_token else 0
        data, _ = await self.api.get(
            project,
            "git",
            path,
            {**{k: v for k, v in filters.items() if v is not None}, "$top": limit, "$skip": skip},
        )
        raw = data.get("value", [])
        items = []
        for pr in raw:
            if since or before:
                created = creation_timestamp(pr["creationDate"])
                if since and created < creation_timestamp(since):
                    continue
                if before and created >= creation_timestamp(before):
                    continue
            if personal == "review" and not any(
                r.get("id") == reviewer_id and r.get("vote", 0) == 0
                for r in pr.get("reviewers", [])
            ):
                continue
            items.append(pr)
        return page(
            clean(items),
            self.api.encode(binding, skip + len(raw)) if len(raw) == limit else None,
            scanned_count=len(raw),
            date_filter="creationDate (local)",
            consistency="live offset pagination; membership changes can shift pages",
            query={**filters, "since": since, "before": before},
        )

    async def get_pull_request(
        self, repository: str, pull_request_id: int, project: str | None = None
    ) -> dict:
        """Read PR details in a verified repository; no date required."""
        repo = await self.git.repo(repository, project)
        data, _ = await self.api.get(
            project, "git", repo + f"/pullrequests/{number(pull_request_id)}"
        )
        return clean(data)

    async def get_pull_request_changes(
        self,
        repository: str,
        pull_request_id: int,
        project: str | None = None,
        iteration_id: int | None = None,
        compare_to: int = 0,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Changed files/types for a pinned PR iteration; compare_to=0 uses common ancestor.
        Omit iteration_id to select latest once, then follow the cursor to keep that iteration.
        """
        number(limit, 200)
        number(compare_to, minimum=0)
        repo = await self.git.repo(repository, project)
        path = repo + f"/pullrequests/{number(pull_request_id)}/iterations"
        iterations, _ = await self.api.get(project, "git", path)
        values = iterations.get("value", [])
        binding = self.api.binding(self.api.project(project), path, iteration_id, compare_to)
        if continuation_token:
            state = self.api.decode(continuation_token, binding)
        else:
            if not values:
                return page([], iterations=[])
            selected = iteration_id if iteration_id is not None else max(i["id"] for i in values)
            number(selected)
            state = {"iteration": selected, "skip": 0}
        data, _ = await self.api.get(
            project,
            "git",
            path + f"/{state['iteration']}/changes",
            {"$top": limit, "$skip": state["skip"], "$compareTo": compare_to},
        )
        next_skip = data.get("nextSkip", 0)
        if next_skip and next_skip <= state["skip"]:
            raise ValueError("Azure returned a non-advancing changes cursor.")
        state["skip"] = next_skip
        return page(
            clean(data.get("changeEntries", [])),
            self.api.encode(binding, state) if next_skip else None,
            iteration_id=state["iteration"],
            iterations=clean(values),
        )

    async def get_pull_request_threads(
        self,
        repository: str,
        pull_request_id: int,
        project: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Read discussions, comments and thread statuses. Bounded local pages; no writes."""
        repo = await self.git.repo(repository, project)
        path = repo + f"/pullrequests/{number(pull_request_id)}/threads"
        data, _ = await self.api.get(project, "git", path)
        items = data.get("value", [])
        return self.api.slice(
            items,
            self.api.binding(self.api.project(project), path, items),
            limit,
            continuation_token,
        )

    async def get_pull_request_statuses(
        self,
        repository: str,
        pull_request_id: int,
        project: str | None = None,
        limit: int = 100,
        continuation_token: str | None = None,
    ) -> dict:
        """Read PR status checks and policy evaluations. Unavailable APIs are reported explicitly.
        Statuses use local pages (API has no paging); policies use native offset pages.
        """
        number(limit, 200)
        metadata = await self.git.get_repository(repository, project)
        repo = await self.git.repo(repository, project)
        number(pull_request_id)
        binding = self.api.binding(self.api.project(project), repo, pull_request_id)
        state = (
            self.api.decode(continuation_token, binding)
            if continuation_token
            else {"source": "statuses", "skip": 0}
        )
        source = state["source"]
        more = False
        try:
            if source == "statuses":
                data, _ = await self.api.get(
                    project,
                    "git",
                    repo + f"/pullrequests/{pull_request_id}/statuses",
                )
                all_items = data.get("value", [])
                fingerprint = self.api.binding(all_items)
                if state.get("fingerprint", fingerprint) != fingerprint:
                    raise ValueError("Statuses changed; restart pagination.")
                state["fingerprint"] = fingerprint
                items = all_items[state["skip"] : state["skip"] + limit]
                more = state["skip"] + len(items) < len(all_items)
            else:
                data, _ = await self.api.get(
                    project,
                    "policy",
                    "evaluations",
                    {
                        "artifactId": f"vstfs:///CodeReview/CodeReviewId/{metadata['project']['id']}/{pull_request_id}",
                        "$top": limit,
                        "$skip": state["skip"],
                    },
                )
                items = data.get("value", [])
                more = len(items) == limit
            error = None
        except AzureDevOpsError as exc:
            if not any(f"HTTP {code}." in str(exc) for code in (403, 404)):
                raise
            items, error = [], str(exc)
        incomplete = state.get("incomplete", False) or bool(error)
        if more:
            state["skip"] += len(items)
        elif source == "statuses":
            state = {"source": "policies", "skip": 0, "incomplete": incomplete}
        else:
            state = None
        return page(
            clean(items),
            self.api.encode(binding, state) if state else None,
            source=source,
            unavailable=error,
            incomplete=incomplete,
        )


PR_TOOLS = tuple(name for name in PullRequests.__dict__ if name.startswith(("get_", "search_")))
