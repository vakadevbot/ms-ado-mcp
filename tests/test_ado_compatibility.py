import json
from dataclasses import replace

import httpx
import pytest

from ms_ado_mcp.client import AzureDevOpsClient
from ms_ado_mcp.policy import BoardScope, Rule
from ms_ado_mcp.service import WorkItemService

from .conftest import authenticated_user, item


async def test_create_my_item_uses_preview_connection_api_with_stable_wit(settings, policy):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/connectionData"):
            # ConnectionData is preview-only; stable WIT setting must not change that.
            if request.url.params["api-version"] != "6.1-preview.1":
                return httpx.Response(400)
            return httpx.Response(200, json=authenticated_user())
        assert request.url.params["api-version"] == "6.1"
        values = {op["path"]: op["value"] for op in json.loads(request.content)}
        assert values["/fields/System.AssignedTo"] == "me@example.org"
        assert values["/fields/System.IterationPath"] == "Demo Project\\Sprint"
        return httpx.Response(200, json=item())

    client = AzureDevOpsClient(replace(settings, api_version="6.1"), httpx.MockTransport(handler))
    service = WorkItemService(client, {policy.name: policy}, policy.name)
    try:
        result = await service.create_my_work_item("task", "My task")
    finally:
        await client.close()
    assert result.created
    assert len(calls) == 2


async def test_creation_date_query_uses_time_precision_and_utc_z(make_service):
    def handler(request):
        assert request.url.params["timePrecision"] == "true"
        query = json.loads(request.content)["query"]
        assert "[System.CreatedDate] >= '2026-07-31T21:00:00Z'" in query
        assert "[System.CreatedDate] < '2026-09-01T00:00:00Z'" in query
        assert "+00:00" not in query
        return httpx.Response(200, json={"workItems": []})

    await make_service(handler).search(
        text="auth",
        created_since="2026-08-01T00:00:00+03:00",
        created_before="2026-09-01",
    )


async def test_onprem_self_assignment_qualifies_account_with_domain(settings):
    def handler(request):
        response = authenticated_user()
        response["authenticatedUser"].pop("uniqueName")
        response["authenticatedUser"]["properties"] = {
            "Account": {"$value": "my-login"},
            "Domain": {"$value": "CORP"},
        }
        return httpx.Response(200, json=response)

    client = AzureDevOpsClient(settings, httpx.MockTransport(handler))
    try:
        user = await client.current_user()
    finally:
        await client.close()
    assert user.unique_name == "CORP\\my-login"


@pytest.mark.parametrize("configured", [None, "Demo Project"])
async def test_creation_without_concrete_iteration_is_blocked_before_http(
    make_service, policy, configured
):
    policy = replace(policy, write_scope=BoardScope("Demo Project\\Team", configured))
    service = make_service(lambda r: pytest.fail("No HTTP allowed"), policy)
    for operation in (service.create("task", "Task"), service.create_my_work_item("task", "Task")):
        with pytest.raises(ValueError, match="iteration|Iteration|sprint"):
            await operation


async def test_trusted_default_iteration_is_preserved_and_checked_before_creation(
    make_service, policy
):
    checked = []

    def checker(draft, context):
        assert draft.fields["System.IterationPath"] == "Demo Project\\PI 5\\Sprint 4"
        checked.append(True)
        return []

    policy = replace(
        policy,
        write_scope=BoardScope("Demo Project\\Team"),
        defaults={"task": {"system.iterationpath": "Demo Project\\PI 5\\Sprint 4"}},
        rules=(Rule("sprint", "Sprint required", checker),),
    )

    def handler(request):
        assert checked == [True]
        values = {op["path"]: op["value"] for op in json.loads(request.content)}
        assert values["/fields/System.IterationPath"] == "Demo Project\\PI 5\\Sprint 4"
        assert "/fields/system.iterationpath" not in values
        return httpx.Response(200, json=item())

    assert (await make_service(handler, policy).create("task", "Task")).created


@pytest.mark.parametrize("default", ["Other\\Sprint", "Demo Project", "Demo Project\\.."])
async def test_invalid_default_iteration_cannot_escape_project(make_service, policy, default):
    policy = replace(
        policy,
        write_scope=BoardScope("Demo Project\\Team"),
        defaults={"task": {"System.IterationPath": default}},
    )
    with pytest.raises(ValueError):
        await make_service(lambda r: pytest.fail("No HTTP allowed"), policy).create("task", "No")


async def test_owned_root_item_can_move_only_to_server_configured_iteration(make_service, policy):
    checked = []

    def checker(proposed, context):
        assert proposed.fields["System.IterationPath"] == policy.write_scope.iteration_path
        checked.append(True)
        return []

    policy = replace(policy, rules=(Rule("target", "Check target iteration", checker),))

    def handler(request):
        if request.url.path.endswith("/connectionData"):
            return httpx.Response(200, json=authenticated_user())
        if request.method == "GET":
            return httpx.Response(200, json=item(**{"System.IterationPath": "Demo Project"}))
        assert checked == [True]
        assert json.loads(request.content) == [
            {"op": "test", "path": "/rev", "value": 1},
            {"op": "add", "path": "/fields/System.IterationPath", "value": "Demo Project\\Sprint"},
        ]
        return httpx.Response(200, json=item())

    assert (await make_service(handler, policy).update(1, use_configured_iteration=True)).updated


@pytest.mark.parametrize(
    "changes",
    [
        {"System.AreaPath": "Demo Project\\Other"},
        {"System.CreatedBy": {"id": "00000000-0000-0000-0000-000000000002"}},
        {"System.WorkItemType": "Feature"},
    ],
)
async def test_iteration_move_never_bypasses_owner_area_or_type(make_service, changes):
    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/connectionData"):
            return httpx.Response(200, json=authenticated_user())
        return httpx.Response(200, json=item(**changes))

    with pytest.raises(ValueError):
        await make_service(handler).update(1, use_configured_iteration=True)


async def test_iteration_move_does_not_accept_a_caller_path(make_service):
    with pytest.raises(ValueError):
        await make_service(lambda r: pytest.fail("No HTTP allowed")).update(
            1,
            use_configured_iteration=True,
            fields={"System.IterationPath": "Demo Project\\Other"},
        )
