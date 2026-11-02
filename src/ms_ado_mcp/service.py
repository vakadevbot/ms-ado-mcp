import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from .client import CHILD, PARENT, AzureDevOpsClient, AzureDevOpsError
from .diagnostics import logger
from .models import (
    BatchValidationEntry,
    BatchValidationResult,
    CreationResult,
    CurrentUser,
    Hierarchy,
    Kind,
    Relation,
    UpdateResult,
    ValidationResult,
    WorkItem,
    WorkItemPage,
)
from .policy import SCOPE_FIELDS, ProjectPolicy, ValidationContext, check_field_names, validate


def literal(value: str) -> str:
    """WIQL strings support either delimiter; reject ambiguous/control characters."""
    if any(ord(c) < 32 for c in value):
        raise ValueError("WIQL filter values cannot contain control characters.")
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    # WIQL has no documented, portable escape for mixed quote delimiters.
    raise ValueError("A search/filter value cannot contain both single and double quotes.")


def positive_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("Work item IDs must be positive integers.")
    return value


def creation_timestamp(value: str) -> datetime:
    """Accept ISO dates (UTC midnight) or timestamps with an explicit timezone."""
    try:
        if len(value) == 10:
            parsed = datetime.combine(date.fromisoformat(value), datetime.min.time(), tzinfo=UTC)
        else:
            parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except ValueError:
        raise ValueError(
            "Creation date filters must be YYYY-MM-DD (UTC) or ISO timestamps with timezone."
        ) from None


def require_bounded_history(
    project: str,
    *,
    active_only: bool,
    iteration_path: str | None,
    created_since: str | None,
    exact_id: bool = False,
) -> None:
    if iteration_path is not None:
        parts = iteration_path.split("\\")
        if (
            len(parts) < 2
            or parts[0].casefold() != project.casefold()
            or any(not p.strip() or p != p.strip() or p in {".", ".."} for p in parts)
        ):
            raise ValueError("iteration_path must identify a sprint/PI below the project root.")
        literal(iteration_path)
    if not active_only and not exact_id and iteration_path is None and created_since is None:
        raise ValueError(
            "Provide iteration_path (sprint/PI) or created_since; "
            "unbounded history queries are not allowed. Active items and exact ID lookups "
            "do not require a time filter."
        )


class WorkItemService:
    def __init__(
        self,
        client: AzureDevOpsClient,
        policies: dict[str, ProjectPolicy],
        default_project: str | None,
    ) -> None:
        self.client = client
        self.policies = policies
        self.default_project = default_project
        writable = [p for p in policies.values() if p.write_scope is not None]
        if len(writable) > 1:
            raise ValueError("Only one project may have a BoardScope for writes.")
        self.writable_policy = writable[0] if writable else None

    def write_policy(self, project: str | None = None) -> ProjectPolicy:
        policy = self.writable_policy
        if policy is None:
            raise ValueError(
                "Writes disabled: configure one ProjectPolicy.write_scope=BoardScope(...)."
            )
        if project is not None and project != policy.name:
            raise ValueError("Writes outside the configured team scope are forbidden.")
        return policy

    @staticmethod
    def check_write_fields(fields: dict[str, JsonValue]) -> None:
        check_field_names(fields)
        if any(name.lower() in SCOPE_FIELDS for name in fields):
            raise ValueError("Area, iteration and board fields are managed by the server policy.")

    @staticmethod
    def require_in_scope(policy: ProjectPolicy, item: WorkItem) -> None:
        if policy.write_scope is None or not policy.write_scope.contains(item):
            raise ValueError("Work item is outside the configured team scope; write forbidden.")

    @staticmethod
    def require_created_by(item: WorkItem, user: CurrentUser) -> None:
        creator = item.fields.get("System.CreatedBy")
        try:
            if not isinstance(creator, dict):
                raise ValueError
            actor_id = UUID(user.id)
            creator_id = UUID(creator.get("id", ""))
            if actor_id.int == 0 or creator_id.int == 0:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError(
                "Cannot verify creator identity; editing/linking is forbidden. "
                "System.CreatedBy must include an immutable Azure identity ID."
            ) from None
        if creator_id != actor_id:
            raise ValueError(
                "Only work items created by the configured PAT owner may be edited/linked."
            )

    def policy(self, project: str | None) -> ProjectPolicy:
        name = project or self.default_project
        if name is None and len(self.policies) == 1:
            name = next(iter(self.policies))
        if name not in self.policies:
            raise ValueError("Select a configured project using get_project_rules.")
        return self.policies[name]

    @staticmethod
    def type_name(policy: ProjectPolicy, kind: Kind) -> str:
        if kind not in policy.work_item_types:
            raise ValueError(f"Work item kind {kind!r} is not configured for this project.")
        return policy.work_item_types[kind]

    async def search(
        self,
        *,
        project: str | None = None,
        text: str | None = None,
        kinds: list[Kind] | None = None,
        active_only: bool = False,
        assigned_to: str | None = None,
        created_by: str | None = None,
        created_since: str | None = None,
        created_before: str | None = None,
        area_path: str | None = None,
        iteration_path: str | None = None,
        limit: int = 50,
        after_id: int = 0,
        as_of: str | None = None,
    ) -> WorkItemPage:
        policy = self.policy(project)
        if not 1 <= limit <= 200 or after_id < 0:
            raise ValueError("limit must be 1–200; after_id must be >=0.")
        exact_id = text is not None and text.strip().isascii() and text.strip().isdecimal()
        require_bounded_history(
            policy.name,
            active_only=active_only,
            iteration_path=iteration_path,
            created_since=created_since,
            exact_id=exact_id,
        )
        selected = kinds if kinds is not None else list(policy.work_item_types)
        if not selected:
            raise ValueError("Select at least one work item kind.")
        filters = ["[System.TeamProject] = @project", f"[System.Id] > {after_id}"]
        lower = creation_timestamp(created_since) if created_since is not None else None
        upper = creation_timestamp(created_before) if created_before is not None else None
        if lower is not None and upper is not None and lower >= upper:
            raise ValueError("created_since must be earlier than created_before.")
        for operator, timestamp in ((">=", lower), ("<", upper)):
            if timestamp is not None:
                filters.append(f"[System.CreatedDate] {operator} {literal(timestamp.isoformat())}")
        type_filters = []
        for kind in dict.fromkeys(selected):
            type_name = self.type_name(policy, kind)
            clause = f"[System.WorkItemType] = {literal(type_name)}"
            if active_only:
                if kind in policy.active_states:
                    names = policy.active_states[kind]
                else:
                    states = await self.client.states(policy.name, type_name)
                    names = tuple(
                        s["name"] for s in states if s.get("category") in policy.active_categories
                    )
                if not names:
                    continue
                clause += f" AND [System.State] IN ({', '.join(literal(s) for s in names)})"
            type_filters.append(f"({clause})")
        if not type_filters:
            return WorkItemPage(items=[], as_of=as_of)
        filters.append(f"({' OR '.join(type_filters)})")
        if text is not None:
            text = text.strip()
            if not text or len(text) > 1000:
                raise ValueError("Search text must contain 1–1000 characters.")
            if text.isascii() and text.isdecimal():
                filters.append(f"[System.Id] = {positive_id(int(text))}")
            else:
                filters.append(f"[System.Title] CONTAINS {literal(text)}")
        for field, value, operator in [
            ("System.AssignedTo", assigned_to, "="),
            ("System.CreatedBy", created_by, "="),
            ("System.AreaPath", area_path, "UNDER"),
            ("System.IterationPath", iteration_path, "UNDER"),
        ]:
            if value is not None:
                rhs = (
                    "@Me"
                    if field in {"System.AssignedTo", "System.CreatedBy"} and value == "@Me"
                    else literal(value)
                )
                filters.append(f"[{field}] {operator} {rhs}")
        wiql = "SELECT [System.Id] FROM WorkItems WHERE " + " AND ".join(filters)
        wiql += " ORDER BY [System.Id] ASC"
        if as_of:
            # Validate the timestamp before using it in WIQL.
            from datetime import datetime

            if datetime.fromisoformat(as_of).tzinfo is None:
                raise ValueError("as_of must be an ISO timestamp with a timezone.")
            wiql += f" ASOF {literal(as_of)}"
        data = await self.client.query(policy.name, wiql, limit + 1)
        ids = [positive_id(row["id"]) for row in data["workItems"]]
        snapshot = data.get("asOf")
        items = await self.client.get_items(policy.name, ids[:limit], as_of=snapshot)
        return WorkItemPage(
            items=items,
            next_after_id=ids[limit - 1] if len(ids) > limit else None,
            as_of=snapshot,
        )

    async def my_work_items(
        self,
        *,
        relationship: Literal["assigned", "created"] = "assigned",
        project: str | None = None,
        text: str | None = None,
        kinds: list[Kind] | None = None,
        active_only: bool = False,
        area_path: str | None = None,
        iteration_path: str | None = None,
        created_since: str | None = None,
        created_before: str | None = None,
        limit: int = 50,
        after_id: int = 0,
        as_of: str | None = None,
    ) -> WorkItemPage:
        if relationship not in {"assigned", "created"}:
            raise ValueError("relationship must be assigned or created.")
        return await self.search(
            project=project,
            text=text,
            kinds=kinds,
            active_only=active_only,
            assigned_to="@Me" if relationship == "assigned" else None,
            created_by="@Me" if relationship == "created" else None,
            created_since=created_since,
            created_before=created_before,
            area_path=area_path,
            iteration_path=iteration_path,
            limit=limit,
            after_id=after_id,
            as_of=as_of,
        )

    async def validate_my_items(
        self,
        *,
        active_only: bool,
        project: str | None = None,
        kinds: list[Kind] | None = None,
        iteration_path: str | None = None,
        created_since: str | None = None,
        created_before: str | None = None,
        max_items: int = 200,
        after_id: int = 0,
        as_of: str | None = None,
    ) -> BatchValidationResult:
        policy = self.policy(project)
        if not 1 <= max_items <= 1000:
            raise ValueError("max_items must be 1–1000.")
        require_bounded_history(
            policy.name,
            active_only=active_only,
            iteration_path=iteration_path,
            created_since=created_since,
        )
        results: list[BatchValidationEntry] = []
        cursor = after_id
        snapshot = as_of
        next_cursor = None
        semaphore = asyncio.Semaphore(4)

        async def validate_one(item: WorkItem) -> BatchValidationEntry:
            item_id = positive_id(item.id)
            title = item.fields.get("System.Title")
            title = title if isinstance(title, str) else None
            async with semaphore:
                try:
                    # Use exactly the same existing-item validation as validate_work_item.
                    report = await self.validate_item(project=policy.name, item_id=item_id)
                    return BatchValidationEntry(
                        work_item_id=item_id, title=title, validation=report
                    )
                except Exception as exc:
                    # Never echo arbitrary exception text/private rule source or work item data.
                    message = (
                        str(exc)
                        if isinstance(exc, AzureDevOpsError)
                        else (
                            "Validation could not complete; "
                            "check work item type, scope and project rules."
                        )
                    )
                    logger.error(
                        "Batch validation failed: item_id=%s error=%s", item_id, type(exc).__name__
                    )
                    return BatchValidationEntry(work_item_id=item_id, title=title, error=message)

        while len(results) < max_items:
            page = await self.search(
                project=policy.name,
                kinds=kinds,
                active_only=active_only,
                assigned_to="@Me",
                iteration_path=iteration_path,
                created_since=created_since,
                created_before=created_before,
                limit=min(200, max_items - len(results)),
                after_id=cursor,
                as_of=snapshot,
            )
            snapshot = page.as_of or snapshot
            next_cursor = page.next_after_id
            results.extend(await asyncio.gather(*(validate_one(item) for item in page.items)))
            if next_cursor is None:
                break
            if next_cursor <= cursor or not page.items:
                raise ValueError("Azure returned an invalid pagination cursor; batch stopped.")
            cursor = next_cursor
        return BatchValidationResult(
            selected_count=len(results),
            valid_count=sum(r.validation is not None and r.validation.valid for r in results),
            invalid_count=sum(r.validation is not None and not r.validation.valid for r in results),
            error_count=sum(r.error is not None for r in results),
            complete=next_cursor is None,
            next_after_id=next_cursor,
            as_of=snapshot,
            results=results,
        )

    async def create_my_work_item(
        self,
        kind: Literal["user_story", "task"],
        title: str,
        description: str = "",
        *,
        project: str | None = None,
        fields: dict[str, JsonValue] | None = None,
        parent_id: int | None = None,
    ) -> CreationResult:
        # Validate arguments before contacting Azure. The identity field is then included
        # in the same draft that corporate rules validate and the create request sends.
        policy = self.write_policy(project)
        if kind not in {"user_story", "task"}:
            raise ValueError("Only user stories and tasks can be created.")
        self.draft(policy, kind, title, description, fields, parent_id)
        if any(name.lower() == "system.assignedto" for name in fields or {}):
            raise ValueError("create_my_work_item assigns to you; omit System.AssignedTo.")
        user = await self.client.current_user()
        if not user.unique_name:
            raise ValueError(
                "Azure DevOps did not return a login for the PAT owner; cannot assign to self. "
                "Use create_task/create_user_story with an explicit System.AssignedTo instead."
            )
        return await self.create(
            kind,
            title,
            description,
            project=project,
            fields={**(fields or {}), "System.AssignedTo": user.unique_name},
            parent_id=parent_id,
        )

    async def hierarchy_for(self, project: str, item: WorkItem) -> Hierarchy:
        parent_ids = [self.client.relation_id(r.url) for r in item.relations if r.rel == PARENT]
        child_ids = [self.client.relation_id(r.url) for r in item.relations if r.rel == CHILD]
        return Hierarchy(
            item=item,
            parents=await self.client.get_items(project, parent_ids),
            children=await self.client.get_items(project, child_ids),
        )

    async def hierarchy(self, item_id: int, project: str | None = None) -> Hierarchy:
        policy = self.policy(project)
        item = await self.client.get_item(policy.name, positive_id(item_id))
        return await self.hierarchy_for(policy.name, item)

    def draft(
        self,
        policy: ProjectPolicy,
        kind: Kind,
        title: str,
        description: str,
        fields: dict[str, JsonValue] | None,
        parent_id: int | None,
    ) -> WorkItem:
        type_name = self.type_name(policy, kind)
        values = {**deepcopy(policy.defaults.get(kind, {})), **(fields or {})}
        self.check_write_fields(fields or {})
        check_field_names(values)
        if policy.write_scope is not None:
            # Remove case aliases from defaults before setting server-owned routing fields.
            values = {k: v for k, v in values.items() if k.lower() not in SCOPE_FIELDS}
            values.update(policy.write_scope.fields())
        values.update(
            {
                "System.Title": title,
                "System.Description": description,
                "System.WorkItemType": type_name,
                "System.TeamProject": policy.name,
            }
        )
        relations = []
        if parent_id is not None:
            relations.append(
                Relation(
                    rel=PARENT,
                    url=self.client.item_url(policy.name, positive_id(parent_id)),
                )
            )
        return WorkItem(fields=values, relations=relations)

    async def check(
        self,
        policy: ProjectPolicy,
        item: WorkItem,
        kind: Kind,
        scope: Literal["draft", "existing"],
        *,
        require_owned_parents: bool = False,
    ) -> ValidationResult:
        hierarchy = await self.hierarchy_for(policy.name, item)
        if policy.write_scope is not None:
            for parent in hierarchy.parents:
                self.require_in_scope(policy, parent)
        if require_owned_parents and hierarchy.parents:
            user = await self.client.current_user()
            for parent in hierarchy.parents:
                self.require_created_by(parent, user)
        context = ValidationContext(
            project=policy.name,
            kind=kind,
            scope=scope,
            parents=tuple(hierarchy.parents),
            children=tuple(hierarchy.children),
        )
        return await validate(item, context, policy)

    async def validate_item(
        self,
        *,
        project: str | None = None,
        item_id: int | None = None,
        kind: Kind | None = None,
        title: str | None = None,
        description: str | None = None,
        fields: dict[str, JsonValue] | None = None,
        parent_id: int | None = None,
    ) -> ValidationResult:
        policy = self.policy(project)
        if item_id is not None:
            if any(v is not None for v in (kind, title, description, fields, parent_id)):
                raise ValueError("Use item_id OR draft arguments, never both.")
            item = await self.client.get_item(policy.name, positive_id(item_id))
            kind = next(
                (
                    k
                    for k, name in policy.work_item_types.items()
                    if name == item.fields.get("System.WorkItemType")
                ),
                None,
            )
            if kind is None:
                raise ValueError("The item's type has no configured policy.")
            return await self.check(policy, item, kind, "existing")
        if kind is None or title is None:
            raise ValueError("Draft validation requires kind and title.")
        policy = self.write_policy(project)
        item = self.draft(policy, kind, title, description or "", fields, parent_id)
        return await self.check(policy, item, kind, "draft")

    async def create(
        self,
        kind: Literal["user_story", "task"],
        title: str,
        description: str = "",
        *,
        project: str | None = None,
        fields: dict[str, JsonValue] | None = None,
        parent_id: int | None = None,
    ) -> CreationResult:
        if kind not in {"user_story", "task"}:
            raise ValueError("Only user stories and tasks can be created.")
        policy = self.write_policy(project)
        item = self.draft(policy, kind, title, description, fields, parent_id)
        report = await self.check(policy, item, kind, "draft", require_owned_parents=True)
        if not report.valid:
            return CreationResult(created=False, validation=report)
        created = await self.client.create(policy.name, self.type_name(policy, kind), item)
        return CreationResult(created=True, validation=report, item=created)

    async def update(
        self,
        item_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        state: str | None = None,
        fields: dict[str, JsonValue] | None = None,
    ) -> UpdateResult:
        policy = self.write_policy()
        changes = dict(fields or {})
        self.check_write_fields(changes)
        if state is not None:
            if any(k.lower() == "system.state" for k in changes):
                raise ValueError("Use state OR fields.System.State, not both.")
            changes["System.State"] = state
        if title is not None:
            changes["System.Title"] = title
        if description is not None:
            changes["System.Description"] = description
        if not changes:
            raise ValueError("Provide at least one change.")
        current = await self.client.get_item(policy.name, positive_id(item_id))
        self.require_in_scope(policy, current)
        kind = next(
            (
                k
                for k in ("task", "user_story")
                if policy.work_item_types.get(k) == current.fields.get("System.WorkItemType")
            ),
            None,
        )
        if kind is None:
            raise ValueError("Only Task and User Story can be edited; Features are read-only.")
        if isinstance(current.rev, bool) or current.rev is None or current.rev <= 0:
            raise ValueError("Azure did not return a valid revision; update forbidden.")
        user = await self.client.current_user()
        self.require_created_by(current, user)
        proposed = current.model_copy(deep=True)
        proposed.fields.update(changes)
        self.require_in_scope(policy, proposed)
        report = await self.check(policy, proposed, kind, "existing")
        if not report.valid:
            return UpdateResult(updated=False, validation=report)
        updated = await self.client.update(policy.name, current, changes)
        return UpdateResult(updated=True, validation=report, item=updated)
