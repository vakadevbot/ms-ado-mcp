import json
from dataclasses import replace

import httpx
import pytest

from ms_ado_mcp.client import AzureDevOpsError
from ms_ado_mcp.models import Violation
from ms_ado_mcp.policy import BoardScope, ProjectPolicy, Rule, load_policies

from .conftest import authenticated_user, item


async def test_unconfigured_scope_blocks_every_write_without_requests(make_service, policy):
    service = make_service(
        lambda r: pytest.fail("No HTTP allowed"), replace(policy, write_scope=None)
    )
    for operation in (
        service.create("task", "Task"),
        service.create("user_story", "Story"),
        service.create_my_work_item("task", "Mine"),
        service.update(1, title="Edit"),
    ):
        with pytest.raises(ValueError, match="Writes disabled"):
            await operation


async def test_create_injects_fixed_scope_over_defaults(make_service, policy):
    scope = BoardScope("Demo Project\\Team", "Demo Project\\Sprint")
    policy = replace(
        policy,
        write_scope=scope,
        defaults={
            "task": {
                "System.AreaPath": "Demo Project\\Other",
                "system.iterationpath": "other",
            }
        },
    )

    def handler(request):
        assert request.method == "POST"
        values = {op["path"]: op["value"] for op in json.loads(request.content)}
        assert values["/fields/System.AreaPath"] == scope.area_path
        assert values["/fields/System.IterationPath"] == scope.iteration_path
        assert "/fields/system.iterationpath" not in values
        return httpx.Response(200, json=item())

    result = await make_service(handler, policy).create("task", "Task")
    assert result.created


@pytest.mark.parametrize(
    "field",
    [
        "System.AreaPath",
        "system.areapath",
        "System.IterationPath",
        "SYSTEM.ITERATIONPATH",
        "System.BoardColumn",
        "System.BoardColumnDone",
        "System.BoardLane",
        "System.TeamProject",
        "System.WorkItemType",
        "System.Rev",
        "System.AreaPath/../Title",
    ],
)
async def test_routing_and_system_overrides_rejected_before_http(make_service, field):
    service = make_service(lambda r: pytest.fail("No HTTP allowed"))
    for operation in (
        service.create("task", "Task", fields={field: "Other"}),
        service.create_my_work_item("task", "Task", fields={field: "Other"}),
        service.update(1, fields={field: "Other"}),
    ):
        with pytest.raises(ValueError):
            await operation


@pytest.mark.parametrize("kind", ["feature", "pi_goal"])
async def test_other_types_cannot_be_created(make_service, kind):
    service = make_service(lambda r: pytest.fail("No HTTP allowed"))
    with pytest.raises(ValueError, match="Only user stories and tasks"):
        await service.create(kind, "No")


async def test_other_project_cannot_be_selected_for_creation(make_service):
    service = make_service(lambda r: pytest.fail("No HTTP allowed"))
    with pytest.raises(ValueError, match="outside"):
        await service.create("task", "No", project="Other")


async def test_parent_outside_scope_blocks_creation(make_service):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path.endswith("/workitemsbatch")
        return httpx.Response(
            200, json={"value": [item(9, **{"System.AreaPath": "Demo Project\\Other"})]}
        )

    with pytest.raises(ValueError, match="outside"):
        await make_service(handler).create("task", "Task", parent_id=9)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "area,iteration",
    [
        ("Demo Project\\Other", "Demo Project\\Sprint"),
        ("Demo Project\\Team\\Child", "Demo Project\\Sprint"),
        ("Demo Project\\Team", "Demo Project\\OtherSprint"),
        (None, "Demo Project\\Sprint"),
    ],
)
async def test_update_outside_exact_scope_never_patches(make_service, policy, area, iteration):
    policy = replace(policy, write_scope=BoardScope("Demo Project\\Team", "Demo Project\\Sprint"))
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(
            200, json=item(**{"System.AreaPath": area, "System.IterationPath": iteration})
        )

    with pytest.raises(ValueError, match="outside"):
        await make_service(handler, policy).update(1, title="No")
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["Feature", "PI Goal", "Epic"])
async def test_non_task_or_story_cannot_be_edited(make_service, kind):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=item(kind=kind))

    with pytest.raises(ValueError, match="Only Task and User Story"):
        await make_service(handler).update(1, title="No")


@pytest.mark.parametrize("kind", ["Task", "User Story"])
async def test_update_validates_full_proposed_item_and_tests_revision(make_service, policy, kind):
    checked = []
    calls = []

    def checker(proposed, context):
        assert context.scope == "existing"
        assert proposed.fields["System.Title"] == "New title"
        assert proposed.fields["System.Description"] == "Description"
        assert proposed.fields["System.State"] == "Doing"
        assert proposed.fields["System.AreaPath"] == "Demo Project\\Team"
        assert proposed.fields["Custom.Owner"] == "Team"
        assert proposed.rev == 7
        checked.append(True)
        return []

    policy = replace(policy, rules=(Rule("check", "Inspect proposed changes", checker),))

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/connectionData"):
            return httpx.Response(200, json=authenticated_user())
        if request.method == "GET":
            return httpx.Response(200, json={**item(kind=kind), "rev": 7})
        assert checked == [True]
        assert request.method == "PATCH"
        assert request.url.path.endswith("/workitems/1")
        assert request.url.params["api-version"] == "6.1-preview.3"
        assert "bypassRules" not in request.url.params
        assert request.headers["content-type"] == "application/json-patch+json"
        operations = json.loads(request.content)
        assert operations[0] == {"op": "test", "path": "/rev", "value": 7}
        values = {op["path"]: op["value"] for op in operations[1:]}
        assert values == {
            "/fields/System.Title": "New title",
            "/fields/System.Description": "Description",
            "/fields/System.State": "Doing",
            "/fields/Custom.Owner": "Team",
        }
        return httpx.Response(
            200, json={**item(kind=kind, **{"System.Title": "New title"}), "rev": 8}
        )

    result = await make_service(handler, policy).update(
        1,
        title="New title",
        description="Description",
        state="Doing",
        fields={"Custom.Owner": "Team"},
    )
    assert result.updated and result.item.rev == 8
    assert len(calls) == 3


@pytest.mark.parametrize(
    "rules",
    [(), (Rule("deny", "Do not allow", lambda i, c: [Violation(code="deny", message="No")]),)],
)
async def test_update_requires_successful_rules(make_service, policy, rules):
    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/connectionData"):
            return httpx.Response(200, json=authenticated_user())
        return httpx.Response(200, json=item())

    result = await make_service(handler, replace(policy, rules=rules)).update(1, title="Changed")
    assert not result.updated and not result.validation.valid


@pytest.mark.parametrize("rev", [None, 0, -1])
async def test_update_missing_revision_is_blocked(make_service, rev):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={**item(), "rev": rev})

    with pytest.raises(ValueError, match="revision"):
        await make_service(handler).update(1, title="No")


@pytest.mark.parametrize("failure", ["conflict", "timeout"])
async def test_update_conflict_or_timeout_never_retried(make_service, failure):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/connectionData"):
            return httpx.Response(200, json=authenticated_user())
        if request.method == "GET":
            return httpx.Response(200, json=item())
        assert json.loads(request.content)[0] == {"op": "test", "path": "/rev", "value": 1}
        if failure == "conflict":
            return httpx.Response(412)
        raise httpx.ReadTimeout("private-secret", request=request)

    with pytest.raises(AzureDevOpsError) as error:
        await make_service(handler).update(1, title="Changed")
    assert "read" in str(error.value).lower()
    assert "Creation may have succeeded" not in str(error.value)
    assert "private-secret" not in str(error.value)
    assert len(calls) == 3


def test_scope_configuration_is_exact_and_single(tmp_path):
    with pytest.raises(ValueError):
        ProjectPolicy(name="Project", write_scope=BoardScope("Other\\Team"))
    for path in ("", " Project\\Team", "Project\\..\\Team", "Project\\Team\\"):
        with pytest.raises(ValueError):
            BoardScope(path)
    path = tmp_path / "rules.py"
    path.write_text(
        "from ms_ado_mcp.policy import BoardScope, ProjectPolicy\n"
        "PROJECTS = [ProjectPolicy(name=p, write_scope=BoardScope(p+'\\\\Team')) "
        "for p in ['A','B']]\n"
    )
    with pytest.raises(ValueError):
        load_policies(path, None)
