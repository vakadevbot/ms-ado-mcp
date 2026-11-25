import asyncio
import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from ms_ado_mcp import organization_server
from ms_ado_mcp import server as server_module
from ms_ado_mcp.app_config import AppSettings
from ms_ado_mcp.client import AzureDevOpsClient
from ms_ado_mcp.config import ConfigurationError, Settings
from ms_ado_mcp.models import UpdateResult, ValidationResult
from ms_ado_mcp.organization_server import build_organization_server
from ms_ado_mcp.policy import BoardScope, ProjectPolicy
from ms_ado_mcp.service import WorkItemService


def payload(result):
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


@pytest.fixture
async def multi(monkeypatch, tmp_path):
    clients = []
    settings = AppSettings(
        "alpha",
        {
            name: Settings(
                organization=name,
                url=f"https://dev.azure.com/{name}",
                pat="fake-pat",
                project="Shared",
                projects=("Shared", name + "-only"),
                allowed_repositories=(f"Shared/{repo}",),
                download_dir=tmp_path,
            )
            for name, repo in [("alpha", "api"), ("beta", "web")]
        },
    )

    def factory(handler, config=None):
        def client(org):
            async def guarded(request):
                assert request.method == "GET"
                await asyncio.sleep(0)
                return handler(request)

            result = AzureDevOpsClient(org, httpx.MockTransport(guarded))
            clients.append(result)
            return result

        monkeypatch.setattr(server_module, "AzureDevOpsClient", client)
        return build_organization_server(config or settings)

    yield factory, settings, clients
    for client in clients:
        await client.close()


async def test_concurrent_reads_use_isolated_organization_clients(multi):
    factory, _, clients = multi

    def handler(request):
        org = request.url.path.split("/")[1]
        assert org in {"alpha", "beta"}
        if request.url.path.endswith("connectionData"):
            return httpx.Response(
                200, json={"authenticatedUser": {"id": org, "providerDisplayName": org}}
            )
        if request.url.path.endswith("/workitems/7"):
            assert f"/{org}/Shared/_apis/wit/" in request.url.path
            return httpx.Response(
                200, json={"id": 7, "fields": {"System.TeamProject": "Shared", "System.Title": org}}
            )
        expected = "api" if org == "alpha" else "web"
        assert request.url.path.endswith("/repositories/" + expected)
        return httpx.Response(
            200, json={"id": "same-id", "name": expected, "project": {"name": "Shared"}}
        )

    app = factory(handler)
    async with app.settings.lifespan(app):
        results = await asyncio.gather(
            *(
                app.call_tool("get_current_user", {"organization": org})
                for org in ("alpha", "beta", "beta", "alpha")
            )
        )
        assert [payload(r)["id"] for r in results] == ["alpha", "beta", "beta", "alpha"]
        for org, repo in [("alpha", "api"), ("beta", "web")]:
            item = payload(
                await app.call_tool("get_work_item", {"item_id": 7, "organization": org})
            )
            assert item["fields"]["System.Title"] == org
            data = payload(
                await app.call_tool("get_repository", {"repository": repo, "organization": org})
            )
            assert data["name"] == repo
        assert payload(await app.call_tool("get_current_user", {}))["id"] == "alpha"
    assert len(clients) == 2 and all(c.http.is_closed for c in clients)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("get_current_user", {}),
        ("get_work_item", {"item_id": 1}),
        ("get_pipeline_run", {"build_id": 1}),
        ("get_release", {"release_id": 1}),
        ("get_repository", {"repository": "api"}),
        ("get_project_rules", {}),
    ],
)
async def test_unknown_organization_never_sends_request(multi, tool, args):
    factory, _, _ = multi
    app = factory(lambda request: pytest.fail("No request for unknown organization"))
    with pytest.raises(ToolError, match="Organization is not configured"):
        await app.call_tool(tool, {**args, "organization": "missing"})


async def test_project_and_repository_must_belong_to_selected_organization(multi):
    factory, _, _ = multi
    app = factory(lambda request: pytest.fail("Wrong scope must fail before network"))
    with pytest.raises(ToolError, match="configured project"):
        await app.call_tool(
            "get_pipeline_run", {"build_id": 1, "project": "alpha-only", "organization": "beta"}
        )
    with pytest.raises(ToolError, match="allowed_repositories"):
        await app.call_tool("get_repository", {"repository": "api", "organization": "beta"})
    with pytest.raises(ToolError, match="configured project"):
        await app.call_tool(
            "get_work_item", {"item_id": 1, "project": "alpha-only", "organization": "beta"}
        )


async def test_native_cursor_bound_to_organization_and_query(multi):
    factory, _, _ = multi
    calls = []

    def handler(request):
        calls.append(request)
        if "continuationToken" in request.url.params:
            assert request.url.params["continuationToken"] == "azure-token"
            return httpx.Response(200, json={"value": [{"id": 2}]})
        return httpx.Response(
            200, json={"value": [{"id": 1}]}, headers={"x-ms-continuationtoken": "azure-token"}
        )

    app = factory(handler)
    first = payload(
        await app.call_tool("list_pipeline_definitions", {"organization": "alpha", "limit": 1})
    )
    token = first["continuation_token"]
    assert token != "azure-token"
    with pytest.raises(ToolError, match="cursor"):
        await app.call_tool(
            "list_pipeline_definitions", {"organization": "beta", "continuation_token": token}
        )
    with pytest.raises(ToolError, match="cursor"):
        await app.call_tool(
            "list_pipeline_definitions",
            {"organization": "alpha", "name": "other", "continuation_token": token},
        )
    assert len(calls) == 1
    last = payload(
        await app.call_tool(
            "list_pipeline_definitions",
            {"organization": "alpha", "limit": 1, "continuation_token": token},
        )
    )
    assert last["items"] == [{"id": 2}] and not last["has_more"]


async def test_local_cursor_and_download_id_do_not_cross_organizations(multi):
    factory, _, _ = multi
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("log.txt", "text")

    def handler(request):
        if request.url.path.endswith("/timeline"):
            return httpx.Response(200, json={"records": [{"id": 1}, {"id": 2}]})
        assert request.url.host == "vsrm.dev.azure.com" and request.url.path.startswith("/alpha/")
        return httpx.Response(200, content=stream.getvalue())

    app = factory(handler)
    first = payload(await app.call_tool("get_pipeline_run_steps", {"build_id": 1, "limit": 1}))
    with pytest.raises(ToolError, match="cursor"):
        await app.call_tool(
            "get_pipeline_run_steps",
            {
                "build_id": 1,
                "organization": "beta",
                "continuation_token": first["continuation_token"],
            },
        )
    downloaded = payload(await app.call_tool("download_release_logs", {"release_id": 1}))
    with pytest.raises(ToolError, match="Unknown download"):
        await app.call_tool(
            "list_download_archive",
            {"download_id": downloaded["download_id"], "organization": "beta"},
        )
    assert (
        payload(
            await app.call_tool("list_download_archive", {"download_id": downloaded["download_id"]})
        )["items"][0]["path"]
        == "log.txt"
    )


async def test_global_write_scope_is_fixed_even_when_default_is_different(multi, monkeypatch):
    factory, config, _ = multi
    config = AppSettings(
        config.default_organization,
        {name: replace(org, rules_file=Path(name)) for name, org in config.organizations.items()},
    )

    def policies(path, default, projects):
        return {
            "Shared": ProjectPolicy(
                name="Shared",
                write_scope=BoardScope("Shared\\Team") if path.name == "beta" else None,
            )
        }

    monkeypatch.setattr(organization_server, "load_policies", policies)
    called = []

    async def update(self, item_id, **kwargs):
        called.append((self.client.settings.organization, item_id))
        return UpdateResult(
            updated=False, validation=ValidationResult(valid=False, scope="existing")
        )

    monkeypatch.setattr(WorkItemService, "update", update)
    app = factory(lambda request: pytest.fail("No HTTP for this routing test"), config)
    tools = {t.name: t for t in await app.list_tools()}
    for name in ("create_task", "create_user_story", "create_my_work_item", "update_work_item"):
        assert "organization" not in tools[name].inputSchema["properties"]
        assert "project" not in tools[name].inputSchema["properties"]
    await app.call_tool("update_work_item", {"item_id": 7, "title": "change"})
    assert called == [("beta", 7)]
    scope = payload(await app.call_tool("get_access_scope", {}))
    assert scope["write_scope"] == {"organization": "beta", "project": "Shared"}
    assert "fake-pat" not in str(scope)


async def test_multiple_write_scopes_fail_before_creating_clients(multi, monkeypatch):
    factory, _, clients = multi
    monkeypatch.setattr(
        organization_server,
        "load_policies",
        lambda *args: {
            "Shared": ProjectPolicy(name="Shared", write_scope=BoardScope("Shared\\Team"))
        },
    )
    with pytest.raises(ConfigurationError, match="ALL organizations"):
        factory(lambda request: pytest.fail("No HTTP"))
    assert not clients
