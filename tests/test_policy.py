from dataclasses import replace
from pathlib import Path

import pytest

from ms_ado_mcp.models import Violation, WorkItem
from ms_ado_mcp.policy import (
    ProjectPolicy,
    Rule,
    ValidationContext,
    load_policies,
    validate,
)


async def test_async_checks_exceptions_and_isolated_snapshots(policy):
    def mutate(item, context):
        item.fields["System.Title"] = "mutated"
        context.parents[0].fields["System.Title"] = "mutated"
        return []

    async def inspect(item, context):
        assert item.fields["System.Title"] == "Original"
        assert context.parents[0].fields["System.Title"] == "Parent"
        return [Violation(code="notice", message="Optional advice", severity="warning")]

    def broken(item, context):
        raise RuntimeError("secret-marker")

    policy = replace(
        policy,
        rules=(
            Rule("mutate", "Mutation is isolated", mutate),
            Rule("inspect", "Read isolated inputs", inspect),
            Rule("broken", "Exception must fail closed", broken),
        ),
    )
    item = WorkItem(fields={"System.Title": "Original"})
    context = ValidationContext(
        project=policy.name,
        kind="task",
        scope="draft",
        parents=(WorkItem(fields={"System.Title": "Parent"}),),
    )
    result = await validate(item, context, policy)
    assert not result.valid
    assert result.checked_rules == ["base.title", "base.parent", "mutate", "inspect", "broken"]
    assert [x.code for x in result.issues] == ["notice", "broken.failed"]
    assert "secret-marker" not in result.model_dump_json()
    assert item.fields["System.Title"] == "Original"
    assert context.parents[0].fields["System.Title"] == "Parent"


@pytest.mark.parametrize("title", ["", "  ", "a" * 256, None, 10])
async def test_invalid_titles(policy, title):
    result = await validate(
        WorkItem(fields={"System.Title": title}),
        ValidationContext(project=policy.name, kind="task", scope="draft"),
        policy,
    )
    assert not result.valid
    assert result.issues[0].code == "base.title"


def test_policy_load_errors_never_fall_back(tmp_path):
    file = tmp_path / "rules.py"
    for source in [
        "PROJECTS = []",
        "raise RuntimeError('private-data')",
        "PROJECTS = [42]",
        "broken Python syntax",
    ]:
        file.write_text(source)
        with pytest.raises(ValueError, match="No fallback") as error:
            load_policies(file, "Demo Project")
        assert "private-data" not in str(error.value)
    with pytest.raises(ValueError):
        load_policies(tmp_path / "missing.py", "Demo Project")


def test_load_example_rules_and_generated_instructions():
    path = Path(__file__).parents[1] / "examples" / "rules.example.py"
    projects = load_policies(path, "Your Project")
    policy = projects["Your Project"]
    for rule in policy.rules:
        assert rule.instructions in policy.instructions()
    assert "example.acceptance" not in policy.instructions("task")
    with pytest.raises(ValueError):
        load_policies(path, "Wrong default")


def test_policy_import_stdout_goes_to_stderr(tmp_path, capsys):
    path = tmp_path / "rules.py"
    path.write_text(
        "from ms_ado_mcp.policy import ProjectPolicy\n"
        "print('plugin-message')\n"
        "PROJECTS = [ProjectPolicy(name='Demo')]\n",
    )
    assert "Demo" in load_policies(path, None)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "plugin-message" in captured.err


def test_duplicate_projects_and_invalid_rule_configs(tmp_path):
    path = tmp_path / "rules.py"
    path.write_text(
        "from ms_ado_mcp.policy import ProjectPolicy\n"
        "PROJECTS = [ProjectPolicy(name='Demo'), ProjectPolicy(name='Demo')]\n",
    )
    with pytest.raises(ValueError):
        load_policies(path, None)
    with pytest.raises(ValueError):
        Rule("test", "", lambda i, c: [])
    with pytest.raises(ValueError):
        ProjectPolicy(name="Demo", defaults={"task": {"System.Title": "No override"}})


async def test_example_story_acceptance_and_html_only_description():
    path = Path(__file__).parents[1] / "examples" / "rules.example.py"
    policy = load_policies(path, None)["Your Project"]
    context = ValidationContext(project=policy.name, kind="user_story", scope="draft")
    item = WorkItem(fields={"System.Title": "Story", "System.Description": "<p>&nbsp;</p>"})
    report = await validate(item, context, policy)
    assert {v.code for v in report.issues} == {"example.description", "example.acceptance"}
    item.fields.update(
        {
            "System.Description": "<p>As a user I need a feature.</p>",
            "Microsoft.VSTS.Common.AcceptanceCriteria": "<p>Observable outcome.</p>",
        }
    )
    assert (await validate(item, context, policy)).valid
