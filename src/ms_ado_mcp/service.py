from copy import deepcopy
from typing import Literal

from pydantic import JsonValue

from .client import CHILD, PARENT, AzureDevOpsClient
from .models import (
    CreationResult,
    Hierarchy,
    Kind,
    Relation,
    ValidationResult,
    WorkItem,
    WorkItemPage,
)
from .policy import ProjectPolicy, ValidationContext, check_field_names, validate


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
        area_path: str | None = None,
        iteration_path: str | None = None,
        limit: int = 50,
        after_id: int = 0,
        as_of: str | None = None,
    ) -> WorkItemPage:
        policy = self.policy(project)
        if not 1 <= limit <= 200 or after_id < 0:
            raise ValueError("limit must be 1–200; after_id must be >=0.")
        selected = kinds if kinds is not None else list(policy.work_item_types)
        if not selected:
            raise ValueError("Select at least one work item kind.")
        filters = ["[System.TeamProject] = @project", f"[System.Id] > {after_id}"]
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
            ("System.AreaPath", area_path, "UNDER"),
            ("System.IterationPath", iteration_path, "UNDER"),
        ]:
            if value is not None:
                rhs = "@Me" if field == "System.AssignedTo" and value == "@Me" else literal(value)
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
        check_field_names(values)
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
    ) -> ValidationResult:
        hierarchy = await self.hierarchy_for(policy.name, item)
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
        policy = self.policy(project)
        item = self.draft(policy, kind, title, description, fields, parent_id)
        report = await self.check(policy, item, kind, "draft")
        if not report.valid:
            return CreationResult(created=False, validation=report)
        created = await self.client.create(policy.name, self.type_name(policy, kind), item)
        return CreationResult(created=True, validation=report, item=created)
