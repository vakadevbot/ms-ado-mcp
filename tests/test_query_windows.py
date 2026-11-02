import json
from dataclasses import replace

import httpx
import pytest

from ms_ado_mcp import server as server_module
from ms_ado_mcp.client import AzureDevOpsClient


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"text": "auth"},
        {"assigned_to": "@Me"},
        {"area_path": "Demo Project\\Team"},
        {"created_before": "2026-09-01"},
        {"iteration_path": ""},
        {"iteration_path": "Demo Project"},
        {"iteration_path": "Other\\PI 5"},
    ],
)
async def test_nonactive_queries_require_a_real_window_before_http(make_service, arguments):
    service = make_service(lambda r: pytest.fail("No HTTP allowed"))
    with pytest.raises(ValueError):
        await service.search(**arguments)


@pytest.mark.parametrize("relationship", ["assigned", "created"])
async def test_my_history_also_requires_window(make_service, relationship):
    service = make_service(lambda r: pytest.fail("No HTTP allowed"))
    with pytest.raises(ValueError, match="created_since"):
        await service.my_work_items(relationship=relationship)


@pytest.mark.parametrize(
    "tool",
    [
        "list_my_work_items",
        "search_work_items",
        "list_active_work_items",
        "list_my_active_work_items",
    ],
)
@pytest.mark.parametrize("window", ["date", "iteration"])
async def test_list_search_tools_forward_windows_to_wiql(
    settings, policy, monkeypatch, tool, window
):
    policy = replace(policy, active_states={kind: ("Doing",) for kind in policy.work_item_types})
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.path.endswith("/wiql")
        query = json.loads(request.content)["query"]
        if window == "date":
            assert "[System.CreatedDate] >= '2026-08-01T00:00:00+00:00'" in query
            assert "[System.CreatedDate] < '2026-09-01T00:00:00+00:00'" in query
        else:
            assert "[System.IterationPath] UNDER 'Demo Project\\PI 5'" in query
        for type_name in policy.work_item_types.values():
            assert f"[System.WorkItemType] = '{type_name}'" in query
        if "active" in tool:
            assert "[System.State] IN ('Doing')" in query
        return httpx.Response(200, json={"workItems": []})

    client = AzureDevOpsClient(settings, httpx.MockTransport(handler))
    monkeypatch.setattr(server_module, "AzureDevOpsClient", lambda settings: client)
    server = server_module.build_server(settings, {policy.name: policy})
    args = (
        {"created_since": "2026-08-01", "created_before": "2026-09-01"}
        if window == "date"
        else {"iteration_path": "Demo Project\\PI 5"}
    )
    if tool == "search_work_items":
        args["text"] = "auth"
    try:
        await server.call_tool(tool, args)
    finally:
        await client.close()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "tool",
    [
        "list_my_work_items",
        "search_work_items",
        "list_active_work_items",
        "list_my_active_work_items",
    ],
)
async def test_active_tools_need_no_window_and_include_all_kinds(
    settings, policy, monkeypatch, tool
):
    policy = replace(policy, active_states={kind: ("Doing",) for kind in policy.work_item_types})
    calls = []

    def handler(request):
        calls.append(request)
        query = json.loads(request.content)["query"]
        assert "[System.CreatedDate]" not in query
        assert "[System.IterationPath]" not in query
        for type_name in policy.work_item_types.values():
            assert f"[System.WorkItemType] = '{type_name}'" in query
        assert "[System.State] IN ('Doing')" in query
        return httpx.Response(200, json={"workItems": []})

    client = AzureDevOpsClient(settings, httpx.MockTransport(handler))
    monkeypatch.setattr(server_module, "AzureDevOpsClient", lambda settings: client)
    server = server_module.build_server(settings, {policy.name: policy})
    args = {} if "active" in tool else {"active_only": True}
    if tool == "search_work_items":
        args["text"] = "auth"
    try:
        await server.call_tool(tool, args)
    finally:
        await client.close()
    assert len(calls) == 1


async def test_exact_id_lookup_stays_available_without_dates(make_service):
    queries = []

    def handler(request):
        query = json.loads(request.content)["query"]
        queries.append(query)
        assert "[System.Id] = 123" in query
        assert "[System.CreatedDate]" not in query
        return httpx.Response(200, json={"workItems": []})

    service = make_service(handler)
    await service.search(text="123")
    await service.my_work_items(text="123")
    assert len(queries) == 2
