from typing import Literal

from pydantic import BaseModel, Field, JsonValue

Kind = Literal["user_story", "task", "feature", "pi_goal"]


class CurrentUser(BaseModel):
    id: str = Field(min_length=1)
    display_name: str
    unique_name: str | None = None


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


class UpdateResult(BaseModel):
    updated: bool
    validation: ValidationResult
    item: WorkItem | None = None


class BatchValidationEntry(BaseModel):
    work_item_id: int
    title: str | None = None
    validation: ValidationResult | None = None
    error: str | None = None


class BatchValidationResult(BaseModel):
    selected_count: int
    valid_count: int
    invalid_count: int
    error_count: int
    complete: bool
    next_after_id: int | None = None
    as_of: str | None = None
    results: list[BatchValidationEntry]
