"""Public extension contract. Corporate implementations live outside this package."""

import importlib.util
import inspect
import json
import re
import sys
from collections.abc import Awaitable, Callable
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from .models import Kind, ValidationResult, Violation, WorkItem

DEFAULT_TYPES: dict[Kind, str] = {
    "user_story": "User Story",
    "task": "Task",
    "feature": "Feature",
    "pi_goal": "PI Goal",
}
FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+\Z")
RESERVED_FIELDS = {
    "system.id",
    "system.rev",
    "system.teamproject",
    "system.workitemtype",
    "system.parent",
    "system.title",
    "system.description",
    "system.createdby",
    "system.createddate",
    "system.changedby",
    "system.changeddate",
}


def check_field_names(fields: dict[str, JsonValue]) -> None:
    for name in fields:
        if not FIELD_NAME.fullmatch(name) or name.lower() in RESERVED_FIELDS:
            raise ValueError(f"Field {name!r} is reserved or is not a field reference name.")


@dataclass(frozen=True)
class ValidationContext:
    project: str
    kind: Kind
    scope: Literal["draft", "existing"]
    parents: tuple[WorkItem, ...] = ()
    children: tuple[WorkItem, ...] = ()


Check = Callable[[WorkItem, ValidationContext], list[Violation] | Awaitable[list[Violation]]]


@dataclass(frozen=True)
class Rule:
    code: str
    instructions: str
    check: Check
    kinds: tuple[Kind, ...] = tuple(DEFAULT_TYPES)

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.instructions.strip() or not callable(self.check):
            raise ValueError("Each rule needs a code, instructions, and a callable check.")
        if not self.kinds or set(self.kinds) - DEFAULT_TYPES.keys():
            raise ValueError("Rule kinds must contain supported logical work item kinds.")


@dataclass(frozen=True)
class ProjectPolicy:
    name: str
    rules: tuple[Rule, ...] = ()
    work_item_types: dict[Kind, str] = field(default_factory=lambda: DEFAULT_TYPES.copy())
    active_categories: tuple[str, ...] = ("InProgress",)
    # Per-kind explicit states override category discovery. Empty tuple disables that kind.
    active_states: dict[Kind, tuple[str, ...]] = field(default_factory=dict)
    defaults: dict[Kind, dict[str, JsonValue]] = field(default_factory=dict)
    guidance: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip() or self.name in {".", ".."}:
            raise ValueError("Project name must be nonempty.")
        if not self.work_item_types or set(self.work_item_types) - DEFAULT_TYPES.keys():
            raise ValueError("Unsupported work item kind in project policy.")
        if any(not name.strip() for name in self.work_item_types.values()):
            raise ValueError("Work item type names must be nonempty.")
        if len(set(self.work_item_types.values())) != len(self.work_item_types):
            raise ValueError("Work item type names must be unique within a project.")
        if len({r.code for r in self.rules}) != len(self.rules):
            raise ValueError("Rule codes must be unique within a project.")
        if (self.defaults.keys() | self.active_states.keys()) - self.work_item_types.keys():
            raise ValueError("Defaults/states reference a kind absent from this project.")
        for fields in self.defaults.values():
            check_field_names(fields)
            json.dumps(fields, allow_nan=False)

    def instructions(self, kind: Kind | None = None) -> str:
        types = self.work_item_types if kind is None else {kind: self.work_item_types[kind]}
        rules = [r for r in self.rules if kind is None or kind in r.kinds]
        lines = [
            f"Project: {self.name}",
            f"Work item kinds: {json.dumps(types, ensure_ascii=False)}",
            f"Field defaults: {json.dumps(self.defaults, ensure_ascii=False)}",
            "Use field reference names in fields. Title must be nonblank and <=255 characters.",
            "Parent links must stay within the project. Do not invent business details.",
            "Call validate_work_item on the draft before creating; fix errors first.",
            self.guidance,
        ]
        lines.extend(f"[{rule.code}] {rule.instructions}" for rule in rules)
        if not rules:
            lines.append("No corporate checks configured: validation and creation are blocked.")
        return "\n".join(line for line in lines if line)


def load_policies(path: Path | None, default_project: str | None) -> dict[str, ProjectPolicy]:
    if path is None:
        if not default_project:
            raise ValueError("Set ADO_RULES_FILE or AZURE_DEVOPS_PROJECT.")
        return {default_project: ProjectPolicy(name=default_project)}
    try:
        spec = importlib.util.spec_from_file_location("_ado_private_policy", path)
        if spec is None or spec.loader is None:
            raise ValueError
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        # Accidental print() in private code must never corrupt the stdio protocol.
        with redirect_stdout(sys.stderr):
            spec.loader.exec_module(module)
        projects = module.PROJECTS
        if not isinstance(projects, (list, tuple)) or not projects:
            raise ValueError
        if not all(isinstance(p, ProjectPolicy) for p in projects):
            raise ValueError
        result = {p.name: p for p in projects}
        if len(result) != len(projects):
            raise ValueError
        if default_project and default_project not in result:
            raise ValueError
        return result
    except Exception:
        raise ValueError(
            "Cannot load ADO_RULES_FILE: check trusted Python code, PROJECTS, "
            "unique project names, and AZURE_DEVOPS_PROJECT. No fallback is allowed."
        ) from None


async def validate(
    item: WorkItem, context: ValidationContext, policy: ProjectPolicy
) -> ValidationResult:
    issues: list[Violation] = []
    title = item.fields.get("System.Title")
    if not isinstance(title, str) or not title.strip() or len(title) > 255:
        issues.append(
            Violation(
                code="base.title",
                message="Title must contain 1–255 characters.",
                field="System.Title",
            )
        )
    if len(context.parents) > 1:
        issues.append(Violation(code="base.parent", message="At most one parent is allowed."))
    rules = [r for r in policy.rules if context.kind in r.kinds]
    if not rules:
        issues.append(
            Violation(
                code="policy.missing",
                message="No corporate checks configured for this work item kind.",
            )
        )
    checked = ["base.title", "base.parent"]
    for rule in rules:
        checked.append(rule.code)
        try:
            # Each rule sees an isolated snapshot; a checker cannot rewrite the outgoing draft.
            isolated_context = ValidationContext(
                project=context.project,
                kind=context.kind,
                scope=context.scope,
                parents=tuple(p.model_copy(deep=True) for p in context.parents),
                children=tuple(c.model_copy(deep=True) for c in context.children),
            )
            outcome = rule.check(item.model_copy(deep=True), isolated_context)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            if not isinstance(outcome, list) or not all(isinstance(v, Violation) for v in outcome):
                raise TypeError("Rule must return list[Violation].")
            issues.extend(outcome)
        except Exception:
            issues.append(
                Violation(
                    code=f"{rule.code}.failed",
                    message="Corporate checker failed. Fix the private rule before continuing.",
                )
            )
    return ValidationResult(
        valid=not any(v.severity == "error" for v in issues),
        scope=context.scope,
        work_item_id=item.id,
        checked_rules=checked,
        issues=issues,
    )
