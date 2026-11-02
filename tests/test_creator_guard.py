import json

import httpx
import pytest

from ms_ado_mcp import server as server_module
from ms_ado_mcp.client import AzureDevOpsClient, AzureDevOpsError

from .conftest import AUTHOR_ID, authenticated_user, item

OTHER_ID = "00000000-0000-0000-0000-000000000002"


@pytest.mark.parametrize(
    "creator",
    [
        {"id": OTHER_ID, "displayName": "Me", "uniqueName": "me@example.org"},
        None,
        "me@example.org",
        {"uniqueName": "me@example.org"},
        {"id": "me@example.org"},
        {"id": ""},
        {"id": None},
        {"id": 123},
        {"id": "00000000-0000-0000-0000-000000000000"},
    ],
)
async def test_assigned_to_me_does_not_grant_edit_permission(make_service, creator):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        if request.url.path.endswith("/connectionData"):
            return httpx.Response(200, json=authenticated_user())
        return httpx.Response(
            200,
            json=item(
                **{
                    "System.CreatedBy": creator,
                    "System.AssignedTo": {"id": AUTHOR_ID, "uniqueName": "me@example.org"},
                }
            ),
        )

    with pytest.raises(ValueError, match="creator|created by"):
        await make_service(handler).update(1, title="Must not write")
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_actor_lookup_failure_blocks_edit(make_service, status):
    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/connectionData"):
            return httpx.Response(status, text="private-secret")
        return httpx.Response(200, json=item())

    with pytest.raises(AzureDevOpsError):
        await make_service(handler).update(1, title="No")


async def test_own_created_item_can_be_edited_even_if_assigned_elsewhere(make_service):
    actor_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/connectionData"):
            return httpx.Response(
                200,
                json={
                    "authenticatedUser": {
                        "id": actor_id,
                        "providerDisplayName": "Renamed User",
                        "uniqueName": "new@example.org",
                    }
                },
            )
        current = item(
            **{
                "System.CreatedBy": {"id": actor_id.upper(), "uniqueName": "old@example.org"},
                "System.AssignedTo": {"id": OTHER_ID},
            }
        )
        if request.method == "GET":
            return httpx.Response(200, json=current)
        operations = json.loads(request.content)
        assert operations == [
            {"op": "test", "path": "/rev", "value": 1},
            {"op": "add", "path": "/fields/System.Title", "value": "Changed"},
        ]
        return httpx.Response(200, json={**current, "rev": 2})

    result = await make_service(handler).update(1, title="Changed")
    assert result.updated
    assert len(calls) == 3


@pytest.mark.parametrize("field", ["System.CreatedBy", "system.createdby", "System.ChangedBy"])
async def test_creator_cannot_be_forged_through_fields(make_service, field):
    service = make_service(lambda r: pytest.fail("No HTTP allowed"))
    for operation in (
        service.update(1, fields={field: {"id": AUTHOR_ID}}),
        service.create("task", "Task", fields={field: {"id": AUTHOR_ID}}),
        service.create_my_work_item("task", "Task", fields={field: {"id": AUTHOR_ID}}),
    ):
        with pytest.raises(ValueError, match="reserved"):
            await operation


async def test_creating_child_of_someone_elses_item_is_blocked(make_service):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/connectionData"):
            return httpx.Response(200, json=authenticated_user())
        assert request.url.path.endswith("/workitemsbatch")
        return httpx.Response(
            200, json={"value": [item(9, **{"System.CreatedBy": {"id": OTHER_ID}})]}
        )

    with pytest.raises(ValueError, match="created by"):
        await make_service(handler).create("task", "Task", parent_id=9)
    assert len(calls) == 2


async def test_personal_search_defaults_to_created_by_me(settings, policy, monkeypatch):
    queries = []

    def handler(request):
        query = json.loads(request.content)["query"]
        queries.append(query)
        assert "[System.CreatedBy] = @Me" in query
        assert "[System.AssignedTo]" not in query
        return httpx.Response(200, json={"workItems": []})

    client = AzureDevOpsClient(settings, httpx.MockTransport(handler))
    monkeypatch.setattr(server_module, "AzureDevOpsClient", lambda settings: client)
    server = server_module.build_server(settings, {policy.name: policy})
    try:
        await server.call_tool(
            "search_my_work_items",
            {
                "text": "auth",
                "created_since": "2026-08-01",
            },
        )
    finally:
        await client.close()
    assert len(queries) == 1


async def test_get_work_item_by_id_is_still_available(settings, policy, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url.path.endswith("/workitems/123")
        return httpx.Response(200, json=item(123))

    client = AzureDevOpsClient(settings, httpx.MockTransport(handler))
    monkeypatch.setattr(server_module, "AzureDevOpsClient", lambda settings: client)
    server = server_module.build_server(settings, {policy.name: policy})
    try:
        await server.call_tool("get_work_item", {"item_id": 123})
    finally:
        await client.close()
    assert len(calls) == 1
