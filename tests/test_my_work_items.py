import json
from dataclasses import replace

import httpx
import pytest

from ms_ado_mcp.client import AzureDevOpsClient, AzureDevOpsError
from ms_ado_mcp.models import Violation
from ms_ado_mcp.policy import Rule

from .conftest import item

USER = {
    "id": "00000000-0000-0000-0000-000000000001",
    "providerDisplayName": "Current User",
    "properties": {"Account": {"$value": "me@example.org"}},
}


@pytest.mark.parametrize("api_version", ["6.1", "6.1-preview"])
@pytest.mark.parametrize("unique_name", [True, False])
async def test_current_user_uses_collection_and_pat_identity(settings, api_version, unique_name):
    identity = {**USER}
    if unique_name:
        identity["uniqueName"] = "me@example.org"
        identity["properties"] = {}

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/collection/_apis/connectionData"
        assert request.url.params["api-version"] == "6.1-preview.1"
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(
            200,
            json={
                "authenticatedUser": identity,
                "authorizedUser": {**USER, "id": "another-user"},
                "secret-metadata": "test-pat",
            },
        )

    client = AzureDevOpsClient(
        replace(settings, api_version=api_version), httpx.MockTransport(handler)
    )
    try:
        user = await client.current_user()
    finally:
        await client.close()
    assert user.id == USER["id"]
    assert user.display_name == "Current User"
    assert user.unique_name == "me@example.org"
    assert "test-pat" not in user.model_dump_json()
    assert "secret-metadata" not in user.model_dump_json()


@pytest.mark.parametrize("identity", [None, {}, {**USER, "isActive": False}])
async def test_missing_or_disabled_identity_is_rejected(settings, identity):
    client = AzureDevOpsClient(
        settings,
        httpx.MockTransport(lambda r: httpx.Response(200, json={"authenticatedUser": identity})),
    )
    try:
        with pytest.raises(AzureDevOpsError, match="active authenticated PAT user"):
            await client.current_user()
    finally:
        await client.close()


@pytest.mark.parametrize(
    "relationship,field", [("assigned", "AssignedTo"), ("created", "CreatedBy")]
)
async def test_my_items_use_me_macro_and_pagination(make_service, relationship, field):
    calls = []
    snapshot = "2026-01-01T00:00:00Z"

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/wiql"):
            query = body["query"]
            assert f"[System.{field}] = @Me" in query
            assert "'@Me'" not in query
            assert "[System.State]" not in query
            assert "[System.Title] CONTAINS 'example'" in query
            assert "[System.Id] > 2" in query
            assert "[System.AreaPath] UNDER 'Demo Project\\Team'" in query
            assert "[System.IterationPath] UNDER 'Demo Project\\Sprint'" in query
            assert request.url.params["$top"] == "2"
            assert query.endswith(f"ASOF '{snapshot}'")
            return httpx.Response(200, json={"workItems": [{"id": 3}, {"id": 4}], "asOf": snapshot})
        assert body["ids"] == [3]
        return httpx.Response(200, json={"value": [item(3)]})

    result = await make_service(handler).my_work_items(
        relationship=relationship,
        text="example",
        kinds=["task"],
        area_path="Demo Project\\Team",
        iteration_path="Demo Project\\Sprint",
        after_id=2,
        limit=1,
        as_of=snapshot,
    )
    assert result.items[0].id == 3
    assert result.next_after_id == 3
    assert result.as_of == snapshot
    assert len(calls) == 2  # No identity lookup needed for @Me.


async def test_my_active_tasks_respect_custom_states(make_service, policy):
    policy = replace(policy, active_states={"task": ("Doing",)})

    def handler(request):
        query = json.loads(request.content)["query"]
        assert "[System.AssignedTo] = @Me" in query
        assert "[System.State] IN ('Doing')" in query
        return httpx.Response(200, json={"workItems": []})

    await make_service(handler, policy).my_work_items(kinds=["task"], active_only=True)


@pytest.mark.parametrize("kind,azure_type", [("task", "Task"), ("user_story", "User Story")])
async def test_create_my_item_validates_resolved_assignee_and_preserves_auth(
    make_service, policy, kind, azure_type
):
    checked = []
    calls = []

    def checker(draft, context):
        assert draft.fields["System.AssignedTo"] == "me@example.org"
        assert draft.fields["Custom.Team"] == "Backend"
        checked.append(True)
        return []

    policy = replace(
        policy,
        rules=(Rule("assignee", "Must be assigned to me", checker),),
        defaults={kind: {"System.AssignedTo": "other@example.org", "Custom.Team": "Backend"}},
    )

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/connectionData"):
            return httpx.Response(200, json={"authenticatedUser": USER})
        assert checked == [True]
        assert request.method == "POST"
        values = {op["path"]: op["value"] for op in json.loads(request.content)}
        assert values["/fields/System.AssignedTo"] == "me@example.org"
        assert "/fields/System.CreatedBy" not in values
        assert "bypassRules" not in request.url.params
        return httpx.Response(200, json=item(42, azure_type))

    result = await make_service(handler, policy).create_my_work_item(kind, "My work item")
    assert result.created and result.item.id == 42
    assert len(calls) == 2
    assert calls[0].headers["authorization"] == calls[1].headers["authorization"]


@pytest.mark.parametrize("with_rules", [False, True])
async def test_my_creation_never_bypasses_rules(make_service, policy, with_rules):
    policy = replace(
        policy,
        rules=(Rule("deny", "Denied", lambda i, c: [Violation(code="deny", message="No")]),)
        if with_rules
        else (),
    )
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path.endswith("/connectionData")
        return httpx.Response(200, json={"authenticatedUser": USER})

    result = await make_service(handler, policy).create_my_work_item("task", "Task")
    assert not result.created
    assert not result.validation.valid
    assert len(calls) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"System.AssignedTo": "someone"},
        {"system.assignedto": "someone"},
        {"System.CreatedBy": "someone"},
    ],
)
async def test_my_creation_rejects_identity_overrides_before_requests(make_service, fields):
    service = make_service(lambda request: pytest.fail("No request expected"))
    with pytest.raises(ValueError):
        await service.create_my_work_item("task", "Task", fields=fields)


async def test_no_account_never_assigns_ambiguous_display_name(make_service):
    def handler(request):
        assert request.url.path.endswith("/connectionData")
        return httpx.Response(200, json={"authenticatedUser": {**USER, "properties": {}}})

    with pytest.raises(ValueError, match="did not return a login"):
        await make_service(handler).create_my_work_item("task", "Task")


@pytest.mark.parametrize("explicit_states", [True, False])
async def test_my_active_mcp_tool_always_filters_owner_and_active_states(
    settings, policy, monkeypatch, explicit_states
):
    from ms_ado_mcp import server as server_module

    policy = replace(policy, active_states={"task": ("Doing",)} if explicit_states else {})
    calls = []
    snapshot = "2026-01-01T00:00:00Z"

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/states"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"name": "Doing", "category": "InProgress"},
                        {"name": "Closed", "category": "Completed"},
                    ]
                },
            )
        body = json.loads(request.content)
        if request.url.path.endswith("/wiql"):
            query = body["query"]
            assert "[System.AssignedTo] = @Me" in query
            assert "[System.CreatedBy]" not in query
            assert "[System.State] IN ('Doing')" in query
            assert "Closed" not in query
            assert "[System.Title] CONTAINS 'auth'" in query
            assert "[System.WorkItemType] = 'Task'" in query
            assert "[System.AreaPath] UNDER 'Demo Project\\Team'" in query
            assert "[System.IterationPath] UNDER 'Demo Project\\Sprint'" in query
            assert "[System.Id] > 2" in query
            assert query.endswith(f"ASOF '{snapshot}'")
            assert request.url.params["$top"] == "2"
            return httpx.Response(
                200,
                json={
                    "workItems": [{"id": 3}, {"id": 4}],
                    "asOf": snapshot,
                },
            )
        assert request.url.path.endswith("/workitemsbatch")
        assert body["ids"] == [3]
        assert body["asOf"] == snapshot
        return httpx.Response(200, json={"value": [item(3)]})

    client = AzureDevOpsClient(settings, httpx.MockTransport(handler))
    monkeypatch.setattr(server_module, "AzureDevOpsClient", lambda settings: client)
    server = server_module.build_server(settings, {policy.name: policy})
    try:
        await server.call_tool(
            "list_my_active_work_items",
            {
                "project": policy.name,
                "text": "auth",
                "kinds": ["task"],
                "area_path": "Demo Project\\Team",
                "iteration_path": "Demo Project\\Sprint",
                "limit": 1,
                "after_id": 2,
                "as_of": snapshot,
            },
        )
    finally:
        await client.close()
    assert len(calls) == (2 if explicit_states else 3)
