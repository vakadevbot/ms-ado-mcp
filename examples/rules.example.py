"""FICTITIOUS examples only. Copy OUTSIDE the public repository and replace the rules."""

import math
from html import unescape
from html.parser import HTMLParser

from ms_ado_mcp.models import Violation
from ms_ado_mcp.policy import ProjectPolicy, Rule


class TextContent(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def has_text(value):
    if not isinstance(value, str):
        return False
    parser = TextContent()
    parser.feed(value)
    return bool(unescape("".join(parser.parts)).strip())


def description_required(item, context):
    if not has_text(item.fields.get("System.Description")):
        return [
            Violation(
                code="example.description",
                field="System.Description",
                message="Добавьте содержательное описание.",
            )
        ]
    return []


def acceptance_required(item, context):
    field = "Microsoft.VSTS.Common.AcceptanceCriteria"
    if not has_text(item.fields.get(field)):
        return [
            Violation(
                code="example.acceptance",
                field=field,
                message="Укажите критерии приёмки User Story.",
            )
        ]
    return []


def task_parent_and_estimate(item, context):
    issues = []
    if len(context.parents) != 1 or (
        context.parents[0].fields.get("System.WorkItemType") != "User Story"
    ):
        issues.append(
            Violation(
                code="example.task_parent",
                message="Task должна иметь родителя User Story.",
            )
        )
    field = "Microsoft.VSTS.Scheduling.OriginalEstimate"
    value = item.fields.get(field)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        issues.append(
            Violation(
                code="example.estimate",
                field=field,
                message="Укажите положительную оценку в часах.",
            )
        )
    return issues


PROJECTS = [
    ProjectPolicy(
        name="Your Project",
        # Change real type names here, including custom PI Goal.
        work_item_types={
            "user_story": "User Story",
            "task": "Task",
            "feature": "Feature",
            "pi_goal": "PI Goal",
        },
        active_categories=("InProgress",),
        # Override category discovery for a particular kind if necessary:
        # active_states={"task": ("В работе", "В анализе")},
        # defaults={"task": {"System.AreaPath": "Your Project\\Team"}},
        guidance="Это демонстрационные правила. Замените их корпоративными перед использованием.",
        rules=(
            Rule(
                code="example.description",
                instructions="Для всех типов нужно непустое текстовое описание (HTML допустим).",
                check=description_required,
            ),
            Rule(
                code="example.acceptance",
                instructions=(
                    "Для User Story заполните Microsoft.VSTS.Common.AcceptanceCriteria: "
                    "критерии приёмки должны содержать текст."
                ),
                check=acceptance_required,
                kinds=("user_story",),
            ),
            Rule(
                code="example.task",
                instructions=(
                    "Task должна иметь родителя User Story и положительную числовую оценку "
                    "в часах в Microsoft.VSTS.Scheduling.OriginalEstimate."
                ),
                check=task_parent_and_estimate,
                kinds=("task",),
            ),
        ),
    ),
]
