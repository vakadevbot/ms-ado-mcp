from typing import Literal

from pydantic import BaseModel, Field, JsonValue

Kind = Literal["user_story", "task", "feature", "pi_goal"]


class Relation(BaseModel):
    rel: str
    url: str


class WorkItem(BaseModel):
    id: int | None = None
    rev: int | None = None
    fields: dict[str, JsonValue]
    relations: list[Relation] = Field(default_factory=list)
    url: str | None = None


class Violation(BaseModel):
    code: str
    message: str
    field: str | None = None
    severity: Literal["error", "warning"] = "error"


class ValidationResult(BaseModel):
    valid: bool
    scope: Literal["draft", "existing"]
    work_item_id: int | None = None
    checked_rules: list[str] = Field(default_factory=list)
    issues: list[Violation] = Field(default_factory=list)


class WorkItemPage(BaseModel):
    items: list[WorkItem]
    next_after_id: int | None = None
    as_of: str | None = None


class Hierarchy(BaseModel):
    item: WorkItem
    parents: list[WorkItem]
    children: list[WorkItem]


class CreationResult(BaseModel):
    created: bool
    validation: ValidationResult
    item: WorkItem | None = None
