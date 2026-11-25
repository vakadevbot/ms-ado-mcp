import os
from dataclasses import replace

import httpx
import pytest

from ms_ado_mcp.client import AzureDevOpsClient
from ms_ado_mcp.config import ConfigurationError, load_settings
from ms_ado_mcp.downloads import Downloads
from ms_ado_mcp.git_read import GitRead
from ms_ado_mcp.policy import BoardScope, ProjectPolicy, load_policies
from ms_ado_mcp.pull_requests import PullRequests
from ms_ado_mcp.read_api import ReadAPI
from ms_ado_mcp.server import build_server
from ms_ado_mcp.service import WorkItemService

ALLOWED_ID = "00000000-0000-0000-0000-000000000001"
DENIED_ID = "00000000-0000-0000-0000-000000000002"
SHA = "a" * 40


def repository(name="allowed", repo_id=ALLOWED_ID, project="Demo Project"):
    return {"id": repo_id, "name": name, "project": {"id": "project-id", "name": project}}


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith(("ADO_", "AZURE_DEVOPS_")):
            monkeypatch.delenv(key)
    path = tmp_path / ".env"
    path.write_text("AZURE_DEVOPS_URL=https://ado.example/c\nAZURE_DEVOPS_PAT=fake\n")
    return path


def test_multiple_projects_and_default(env_file, monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PROJECTS", " First Project, Second Project ")
    monkeypatch.setenv("ADO_ALLOWED_REPOSITORIES", "First Project/api, Second Project/web")
    settings = load_settings(env_file)
    assert settings.projects == ("First Project", "Second Project")
    assert settings.project == "First Project"
    assert settings.repositories_for("second project") == ("web",)
    policies = load_policies(None, settings.project, settings.projects)
    assert list(policies) == ["First Project", "Second Project"]
    assert all(p.write_scope is None for p in policies.values())
    monkeypatch.setenv("AZURE_DEVOPS_PROJECT", "Second Project")
    assert load_settings(env_file).project == "Second Project"


def test_csv_legacy_project_variable(env_file, monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PROJECT", "One, Two")
    settings = load_settings(env_file)
    assert settings.project == "One" and settings.projects == ("One", "Two")


@pytest.mark.parametrize(
    "key,value",
    [
        ("AZURE_DEVOPS_PROJECTS", "A,,B"),
        ("AZURE_DEVOPS_PROJECTS", "A,a"),
        ("AZURE_DEVOPS_PROJECTS", "A/Repo"),
        ("AZURE_DEVOPS_PROJECTS", "*"),
        ("ADO_ALLOWED_REPOSITORIES", "Repo"),
        ("ADO_ALLOWED_REPOSITORIES", "A/R/extra"),
        ("ADO_ALLOWED_REPOSITORIES", "A/*"),
        ("ADO_ALLOWED_REPOSITORIES", "A/.."),
        ("ADO_ALLOWED_REPOSITORIES", "A/R,"),
        ("ADO_ALLOWED_REPOSITORIES", "A/R,a/r"),
    ],
)
def test_malformed_allowlists_fail_closed(env_file, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigurationError):
        load_settings(env_file)


def test_conflicting_project_settings_fail(env_file, monkeypatch):
    monkeypatch.setenv("AZURE_DEVOPS_PROJECTS", "A,B")
    monkeypatch.setenv("AZURE_DEVOPS_PROJECT", "C")
    with pytest.raises(ConfigurationError):
        load_settings(env_file)
    monkeypatch.setenv("AZURE_DEVOPS_PROJECT", "A,B")
    with pytest.raises(ConfigurationError):
        load_settings(env_file)


def test_private_policies_preserved_and_scope_filtered(tmp_path):
    rules = tmp_path / "rules.py"
    rules.write_text(
        "from ms_ado_mcp.policy import ProjectPolicy, BoardScope\n"
        "PROJECTS = [ProjectPolicy(name='One', write_scope=BoardScope('One\\\\Team')), "
        "ProjectPolicy(name='Hidden')]\n"
    )
    policies = load_policies(rules, "Two", ("One", "Two"))
    assert set(policies) == {"One", "Two"}
    assert policies["One"].write_scope.area_path == "One\\Team"
    assert policies["Two"].write_scope is None and not policies["Two"].rules
    assert set(load_policies(rules, None)) == {"One", "Hidden"}
    assert load_policies(rules, "Two", ("Two",))["Two"].write_scope is None


@pytest.fixture
async def access(settings):
    clients = []

    def factory(handler, allowed=("Demo Project/allowed",), projects=()):
        config = replace(settings, allowed_repositories=allowed, projects=projects)

        def guarded(request):
            assert request.method == "GET"
            return handler(request)

        client = AzureDevOpsClient(config, httpx.MockTransport(guarded))
        clients.append(client)
        service = WorkItemService(
            client, {"Demo Project": ProjectPolicy(name="Demo Project")}, config.project
        )
        api = ReadAPI(service)
        git = GitRead(api, Downloads(api))
        return service, git, PullRequests(api, git)

    yield factory
    for client in clients:
        await client.close()


def operations(git, prs, name):
    return [
        git.get_repository(name),
        git.list_repository_refs(name),
        git.list_repository_files(name, "main"),
        git.search_repository_files(name, "main"),
        git.get_repository_file(name, "/x", "main"),
        git.download_repository_file(name, "/x", "main"),
        git.download_repository_archive(name, "main"),
        git.list_repository_commits(name, "main", "2026-01-01"),
        git.get_repository_commit(name, SHA),
        prs.search_pull_requests(repository=name),
        prs.get_pull_request(name, 1),
        prs.get_pull_request_changes(name, 1),
        prs.get_pull_request_threads(name, 1),
        prs.get_pull_request_statuses(name, 1),
    ]


async def test_every_repository_tool_denies_unlisted_names_without_network(access):
    def handler(request):
        pytest.fail("Denied repository must not be requested")

    _, git, prs = access(handler)
    for operation in operations(git, prs, "denied"):
        with pytest.raises(ValueError, match="ADO_ALLOWED_REPOSITORIES"):
            await operation


async def test_empty_allowlist_denies_everything(access):
    def handler(request):
        pytest.fail("Empty repository scope must not request repository data")

    _, git, prs = access(handler, allowed=())
    assert (await git.list_repositories())["items"] == []
    assert (await prs.search_pull_requests())["items"] == []
    for operation in operations(git, prs, ALLOWED_ID):
        with pytest.raises(ValueError, match="ADO_ALLOWED_REPOSITORIES"):
            await operation


async def test_id_cannot_bypass_name_allowlist(access):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.url.path.endswith("/git/repositories")
        return httpx.Response(200, json={"value": [repository(), repository("denied", DENIED_ID)]})

    _, git, prs = access(handler)
    for operation in operations(git, prs, DENIED_ID):
        with pytest.raises(ValueError, match="ADO_ALLOWED_REPOSITORIES"):
            await operation
    assert len(calls) == 14


async def test_allowed_id_and_filtered_repository_pages(access):
    def handler(request):
        if request.url.path.endswith("/repositories"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        repository("denied", DENIED_ID),
                        {**repository(), "parentRepository": repository("denied", DENIED_ID)},
                        repository("second", "second-id"),
                    ]
                },
            )
        assert request.url.path.endswith("/repositories/" + ALLOWED_ID)
        return httpx.Response(200, json=repository())

    _, git, _ = access(handler, allowed=("Demo Project/allowed", "Demo Project/second"))
    first = await git.list_repositories(limit=1)
    last = await git.list_repositories(limit=1, continuation_token=first["continuation_token"])
    assert [i["name"] for i in first["items"] + last["items"]] == ["allowed", "second"]
    assert "parentRepository" not in first["items"][0]
    assert (await git.get_repository(ALLOWED_ID))["name"] == "allowed"


async def test_metadata_rename_and_same_name_other_project_denied(access):
    _, git, _ = access(lambda request: httpx.Response(200, json=repository("renamed")))
    with pytest.raises(ValueError, match="ADO_ALLOWED_REPOSITORIES"):
        await git.get_repository("allowed")

    def handler(request):
        pytest.fail("Project/name pair must be checked before networking")

    _, git, _ = access(handler, allowed=("Other/allowed",), projects=("Demo Project", "Other"))
    with pytest.raises(ValueError, match="ADO_ALLOWED_REPOSITORIES"):
        await git.get_repository("allowed", project="Demo Project")


async def test_pr_search_visits_only_allowed_repositories(access):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/repositories/allowed"):
            return httpx.Response(200, json=repository())
        if request.url.path.endswith("/repositories/second"):
            return httpx.Response(200, json=repository("second", "second-id"))
        assert "/repositories/" in request.url.path and request.url.path.endswith("/pullrequests")
        if request.url.params["$skip"] != "0":
            return httpx.Response(200, json={"value": []})
        second = "/second-id/" in request.url.path
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "pullRequestId": 2 if second else 1,
                        "repository": repository("second", "second-id") if second else repository(),
                    }
                ]
            },
        )

    _, _, prs = access(handler, allowed=("Demo Project/allowed", "Demo Project/second"))
    token, ids = None, []
    for _ in range(5):
        result = await prs.search_pull_requests(limit=1, continuation_token=token)
        ids.extend(p["pullRequestId"] for p in result["items"])
        token = result["continuation_token"]
        if token is None:
            break
    assert ids == [1, 2] and token is None
    assert all("denied" not in path for path in calls)


async def test_fork_source_cannot_bypass_allowlist(access):
    def handler(request):
        if request.url.path.endswith("/repositories/allowed"):
            return httpx.Response(200, json=repository())
        pr = {
            "pullRequestId": 1,
            "repository": repository(),
            "forkSource": {"repository": repository("denied", DENIED_ID)},
        }
        if request.url.path.endswith("/pullrequests"):
            return httpx.Response(200, json={"value": [pr]})
        assert request.url.path.endswith("/pullrequests/1")
        return httpx.Response(200, json=pr)

    _, _, prs = access(handler)
    assert (await prs.search_pull_requests())["items"] == []
    for operation in [
        prs.get_pull_request("allowed", 1),
        prs.get_pull_request_changes("allowed", 1),
        prs.get_pull_request_threads("allowed", 1),
        prs.get_pull_request_statuses("allowed", 1),
    ]:
        with pytest.raises(ValueError, match="source repository"):
            await operation


async def test_project_list_restricts_service_and_write_scope(settings):
    config = replace(settings, projects=("Demo Project", "Other"))
    client = AzureDevOpsClient(
        config, httpx.MockTransport(lambda request: pytest.fail("No network"))
    )
    try:
        hidden = ProjectPolicy(name="Hidden", write_scope=BoardScope("Hidden\\Team"))
        service = WorkItemService(client, {"Hidden": hidden}, config.project)
        assert set(service.policies) == {"Demo Project", "Other"}
        assert service.policy("Other").write_scope is None
        with pytest.raises(ValueError, match="Writes disabled"):
            service.write_policy()
        with pytest.raises(ValueError, match="configured project"):
            service.policy("Hidden")
    finally:
        await client.close()


def test_unknown_repository_project_fails_startup(settings):
    with pytest.raises(ConfigurationError, match="outside the allowed projects"):
        build_server(
            replace(settings, allowed_repositories=("Other/repo",)),
            {"Demo Project": ProjectPolicy(name="Demo Project")},
        )
