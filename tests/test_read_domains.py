import io
import zipfile
from dataclasses import replace

import httpx
import pytest

from ms_ado_mcp.builds import Builds
from ms_ado_mcp.client import AzureDevOpsClient, AzureDevOpsError
from ms_ado_mcp.config import ConfigurationError, Settings, load_settings
from ms_ado_mcp.downloads import Downloads
from ms_ado_mcp.git_read import GitRead
from ms_ado_mcp.pull_requests import PullRequests
from ms_ado_mcp.read_api import ReadAPI
from ms_ado_mcp.releases import Releases
from ms_ado_mcp.service import WorkItemService

SHA = "a" * 40
REPO = {"id": "repo", "name": "repo", "project": {"id": "project-id", "name": "Demo Project"}}


@pytest.fixture
async def domains(settings, policy, tmp_path):
    clients = []

    def factory(handler, **options):
        options.setdefault("allowed_repositories", ("Demo Project/repo",))
        config = replace(settings, download_dir=tmp_path / "downloads", **options)

        def guarded(request):
            assert request.method == "GET"
            return handler(request)

        client = AzureDevOpsClient(config, httpx.MockTransport(guarded))
        clients.append(client)
        api = ReadAPI(WorkItemService(client, {policy.name: policy}, policy.name))
        downloads = Downloads(api)
        git = GitRead(api, downloads)
        return (
            api,
            Builds(api, downloads),
            Releases(api, downloads),
            git,
            PullRequests(api, git),
            downloads,
        )

    yield factory
    for client in clients:
        await client.close()


def test_release_hosts():
    for url, expected in [
        ("https://dev.azure.com/example", "https://vsrm.dev.azure.com/example"),
        ("https://example.visualstudio.com", "https://vsrm.dev.azure.com/example"),
        ("https://ado.example/tfs/Collection", "https://ado.example/tfs/Collection"),
    ]:
        assert Settings(url=url, pat="fake").release_base_url == expected
    settings = Settings(
        url="https://ado.example", pat="fake", release_url="https://release.example/c"
    )
    assert settings.release_base_url == "https://release.example/c"
    for url in ["http://example", "https://user:secret@example", "https://example/?sig=secret"]:
        with pytest.raises(ConfigurationError):
            replace(settings, release_url=url)


async def test_build_filters_and_native_pages(domains):
    calls = []

    def handler(request):
        calls.append(request)
        q = request.url.params
        assert q["api-version"] == "6.0"
        assert q["queryOrder"] == "queueTimeDescending"
        assert q["minTime"] == "2026-01-01T00:00:00Z"
        assert q["definitions"] == "9"
        assert q["branchName"] == "refs/heads/main"
        if "continuationToken" not in q:
            return httpx.Response(
                200, json={"value": [{"id": 2}]}, headers={"x-ms-continuationtoken": "a+/="}
            )
        assert q["continuationToken"] == "a+/="
        return httpx.Response(200, json={"value": [{"id": 1}]})

    _, builds, *_ = domains(handler)
    kwargs = dict(since="2026-01-01", definition_id=9, branch="refs/heads/main", limit=1)
    first = await builds.list_pipeline_runs(**kwargs)
    second = await builds.list_pipeline_runs(
        **kwargs, continuation_token=first["continuation_token"]
    )
    assert [i["id"] for i in first["items"] + second["items"]] == [2, 1]
    assert not second["has_more"]
    assert len(calls) == 2


async def test_history_validation_no_network(domains):
    def handler(request):
        pytest.fail("Validation must precede network")

    _, builds, releases, _, prs, _ = domains(handler)
    for operation in [
        builds.list_pipeline_runs(),
        builds.list_pipeline_runs(before="2026-02-01"),
        builds.list_pipeline_runs(since="2026-02-01", before="2026-01-01"),
        builds.list_pipeline_runs(since="2026-01-01", status="bad"),
        releases.list_releases(since=""),
        prs.search_pull_requests(status="completed"),
        builds.list_pipeline_definitions(limit=201),
        builds.get_pipeline_run(1, project="Other"),
    ]:
        with pytest.raises(ValueError):
            await operation


async def test_ambiguous_definitions(domains):
    def handler(request):
        assert request.url.path.endswith("/definitions")
        return httpx.Response(200, json={"value": [{"id": 1, "name": "x"}, {"id": 2, "name": "x"}]})

    _, builds, releases, *_ = domains(handler)
    with pytest.raises(ValueError, match="ambiguous"):
        await builds.list_pipeline_runs(name="x", since="2026-01-01")
    with pytest.raises(ValueError, match="ambiguous"):
        await releases.list_releases(name="x", since="2026-01-01")


async def test_release_filters_and_host(domains):
    def handler(request):
        assert request.url.host == "vsrm.dev.azure.com"
        assert "/_apis/release/releases" in request.url.path
        q = request.url.params
        assert q["statusFilter"] == "active"
        assert q["environmentStatusFilter"] == "8"
        assert q["minCreatedTime"] == "2026-01-01T00:00:00Z"
        assert "minTime" not in q
        return httpx.Response(200, json={"value": []}, headers={"x-ms-continuationtoken": "123"})

    _, _, releases, *_ = domains(handler, url="https://dev.azure.com/example")
    result = await releases.list_releases(
        "2026-01-01", status="active", environment_status=8, definition_environment_id=3
    )
    assert result["continuation_token"] == "123"


async def test_timeline_and_release_attempts(domains):
    records = [
        {"id": "job", "parentId": "stage", "attempt": 2, "log": {"id": 7}},
        {"id": "stage", "attempt": 1},
    ]

    def handler(request):
        if request.url.path.endswith("/timeline"):
            return httpx.Response(200, json={"id": "timeline", "records": records})
        return httpx.Response(
            200,
            json={
                "environments": [
                    {
                        "id": 3,
                        "status": "failed",
                        "deploySteps": [
                            {
                                "id": 4,
                                "attempt": 2,
                                "releaseDeployPhases": [
                                    {
                                        "id": 5,
                                        "deploymentJobs": [
                                            {
                                                "job": {"name": "agent"},
                                                "tasks": [{"id": 6, "status": "failed"}],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
        )

    _, builds, releases, *_ = domains(handler)
    first = await builds.get_pipeline_run_steps(1, limit=1)
    second = await builds.get_pipeline_run_steps(
        1, limit=1, continuation_token=first["continuation_token"]
    )
    assert first["items"] + second["items"] == records
    steps = await releases.get_release_steps(1)
    task = steps["items"][-1]
    assert task["kind"] == "task" and task["attempt"] == 2
    assert task["release_deploy_phase_id"] == 5 and task["environment_id"] == 3


async def test_logs_are_bounded(domains):
    def handler(request):
        assert request.url.params["startLine"] == "2"
        return httpx.Response(200, text="a\nb\nc\n")

    _, builds, releases, *_ = domains(handler)
    for result in [
        await builds.get_pipeline_step_log(1, 2, start_line=2, line_count=2),
        await releases.get_release_task_log(1, 2, 3, 4, start_line=2, line_count=2),
    ]:
        assert result["text"] == "a\nb\n" and result["truncated"]
        assert result["next_start_line"] == 4
    small = await builds.get_pipeline_step_log(1, 2, start_line=2, max_bytes=2)
    assert small["byte_limit_reached"] and small["next_start_line"] is None


async def test_pr_local_date_filter_and_empty_page(domains):
    def handler(request):
        if request.url.path.endswith("/repositories/repo"):
            return httpx.Response(200, json=REPO)
        q = request.url.params
        assert "searchCriteria.minTime" not in q
        assert q["searchCriteria.status"] == "completed"
        if q["$skip"] == "0":
            return httpx.Response(
                200, json={"value": [{"pullRequestId": 1, "creationDate": "2020-01-01"}]}
            )
        return httpx.Response(
            200, json={"value": [{"pullRequestId": 2, "creationDate": "2026-01-01"}]}
        )

    _, _, _, _, prs, _ = domains(handler)
    first = await prs.search_pull_requests(status="completed", since="2026-01-01", limit=1)
    assert first["items"] == [] and first["has_more"]
    second = await prs.search_pull_requests(
        status="completed",
        since="2026-01-01",
        limit=1,
        continuation_token=first["continuation_token"],
    )
    assert second["items"][0]["pullRequestId"] == 2
    with pytest.raises(ValueError, match="cursor"):
        await prs.search_pull_requests(
            status="completed",
            since="2025-01-01",
            limit=1,
            continuation_token=first["continuation_token"],
        )


async def test_my_prs(domains):
    def handler(request):
        if request.url.path.endswith("/repositories/repo"):
            return httpx.Response(200, json=REPO)
        if request.url.path.endswith("connectionData"):
            return httpx.Response(200, json={"authenticatedUser": {"id": "me"}})
        assert request.url.params["searchCriteria.reviewerId"] == "me"
        return httpx.Response(
            200,
            json={
                "value": [
                    {"pullRequestId": 1, "reviewers": [{"id": "me", "vote": 0}]},
                    {"pullRequestId": 2, "reviewers": [{"id": "me", "vote": 10}]},
                ]
            },
        )

    _, _, _, _, prs, _ = domains(handler)
    assert [
        i["pullRequestId"] for i in (await prs.search_pull_requests(personal="review"))["items"]
    ] == [1]


async def test_pr_iteration_pagination(domains):
    calls = []

    def handler(request):
        if request.url.path.endswith("/pullrequests/1"):
            return httpx.Response(200, json={"pullRequestId": 1, "repository": REPO})
        calls.append(request)
        path = request.url.path
        if path.endswith("/repositories/repo"):
            return httpx.Response(200, json=REPO)
        if path.endswith("/iterations"):
            return httpx.Response(200, json={"value": [{"id": 1}, {"id": 2}]})
        assert "/iterations/2/changes" in path
        skip = int(request.url.params["$skip"])
        return httpx.Response(
            200,
            json={
                "changeEntries": [{"changeType": "edit", "id": skip}],
                "nextSkip": 1 if skip == 0 else 0,
            },
        )

    _, _, _, _, prs, _ = domains(handler)
    first = await prs.get_pull_request_changes("repo", 1, limit=1)
    second = await prs.get_pull_request_changes(
        "repo", 1, limit=1, continuation_token=first["continuation_token"]
    )
    assert second["items"][0]["id"] == 1 and not second["has_more"]


@pytest.mark.parametrize(
    "version_type,version", [("branch", "feature/x"), ("tag", "v1"), ("commit", SHA)]
)
async def test_file_versions_and_lfs(domains, version_type, version):
    def handler(request):
        if request.url.path.endswith("/repositories/repo"):
            return httpx.Response(200, json=REPO)
        q = request.url.params
        if "download" not in q:
            assert q["versionDescriptor.version"] == version
            assert q["versionDescriptor.versionType"] == version_type
            return httpx.Response(200, json={"commitId": SHA, "objectId": "blob", "path": "/x"})
        assert q["versionDescriptor.versionType"] == "commit"
        assert q["versionDescriptor.version"] == SHA and q["resolveLfs"] == "false"
        return httpx.Response(
            200, text="version https://git-lfs.github.com/spec/v1\noid sha256:123\n"
        )

    _, _, _, git, _, _ = domains(handler)
    result = await git.get_repository_file("repo", "/x", version, version_type)
    assert result["lfs_pointer"]


async def test_pinned_tree_traversal_filters_and_truncation(domains):
    metadata_calls = 0

    def handler(request):
        nonlocal metadata_calls
        path = request.url.path
        if path.endswith("/repositories/repo"):
            return httpx.Response(200, json=REPO)
        if path.endswith("/items"):
            metadata_calls += 1
            return httpx.Response(
                200, json={"commitId": SHA, "objectId": "tree1", "gitObjectType": "tree"}
            )
        assert "continuationToken" not in request.url.params and "$top" not in request.url.params
        entries = (
            [
                {"relativePath": "a.py", "objectId": "a", "gitObjectType": "blob"},
                {"relativePath": "dir", "objectId": "tree2", "gitObjectType": "tree"},
                {"relativePath": "skip.py", "objectId": "skip", "gitObjectType": "blob"},
            ]
            if path.endswith("tree1")
            else [{"relativePath": "b.py", "objectId": "b", "gitObjectType": "blob"}]
        )
        return httpx.Response(
            200, json={"treeEntries": entries, "truncated": path.endswith("tree2")}
        )

    _, _, _, git, _, _ = domains(handler)
    kwargs = dict(
        repository="repo", version="main", extensions=[".py"], exclude=["*/skip.py"], limit=1
    )
    results, token = [], None
    while True:
        result = await git.search_repository_files(**kwargs, continuation_token=token)
        results += result["items"]
        assert result["commit"] == SHA
        token = result["continuation_token"]
        if not token:
            break
    assert [i["path"] for i in results] == ["/a.py", "/dir/b.py"]
    assert result["incomplete"] and metadata_calls == 1


async def test_cross_project_resource_and_safe_errors(domains, caplog):
    def handler(request):
        return httpx.Response(200, json={"id": "repo", "project": {"name": "Other"}})

    _, _, _, git, _, _ = domains(handler)
    with pytest.raises(AzureDevOpsError, match="Cross-project"):
        await git.get_repository("repo")

    def denied(request):
        return httpx.Response(403, text="test-pat https://host/?sig=secret Authorization: secret")

    api, builds, *_ = domains(denied)
    with pytest.raises(AzureDevOpsError, match="build read: HTTP 403") as error:
        await builds.get_pipeline_run(1)
    assert "test-pat" not in str(error.value) + caplog.text
    assert "sig=secret" not in caplog.text
    with pytest.raises(ValueError):
        api.decode("attacker", "query")


def zip_bytes(name="hello.txt", data=b"hello\nworld\n", compression=zipfile.ZIP_STORED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        archive.writestr(name, data)
    return stream.getvalue()


async def test_signed_download_strips_credentials_and_inspection(domains):
    payload = zip_bytes()

    def handler(request):
        if request.url.host == "ado.example":
            assert "authorization" in request.headers
            return httpx.Response(
                302,
                headers={
                    "location": "https://storage.example/x?sig=secret",
                    "set-cookie": "sensitive=cookie; Domain=storage.example",
                },
            )
        assert request.url.host == "storage.example"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, content=payload)

    _, _, releases, _, _, downloads = domains(handler, download_hosts=("storage.example",))
    result = await releases.download_release_logs(1)
    assert result["size"] == len(payload) and len(result["sha256"]) == 64
    assert "secret" not in str(result)
    listing = await downloads.list_download_archive(result["download_id"])
    assert listing["items"][0]["path"] == "hello.txt"
    text = await downloads.get_download_archive_file(
        result["download_id"], "hello.txt", line_count=1
    )
    assert text["text"] == "hello\n" and text["truncated"]
    with pytest.raises(ValueError):
        await downloads.list_download_archive("../../private")


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/x",
        "http://storage.example/x",
        "https://user:pass@storage.example/x",
        "https://storage.example:444/x",
    ],
)
async def test_download_redirect_blocked(domains, location):
    def handler(request):
        assert request.url.host == "ado.example"
        return httpx.Response(302, headers={"location": location})

    _, _, releases, _, _, _ = domains(handler, download_hosts=("storage.example",))
    with pytest.raises(ValueError, match="blocked"):
        await releases.download_release_logs(1)


@pytest.mark.parametrize(
    "payload",
    [
        zip_bytes("../outside"),
        zip_bytes("C:/outside"),
        zip_bytes("bomb", b"a" * 1000000, zipfile.ZIP_DEFLATED),
        b"not zip",
    ],
)
async def test_unsafe_archives_rejected_and_partial_files_removed(domains, payload, tmp_path):
    _, _, releases, _, _, _ = domains(lambda request: httpx.Response(200, content=payload))
    with pytest.raises(ValueError):
        await releases.download_release_logs(1)
    assert list((tmp_path / "downloads").iterdir()) == []


async def test_stream_limit_and_unsupported_artifact(domains, tmp_path):
    _, _, releases, _, _, _ = domains(
        lambda request: httpx.Response(200, content=b"0123456789"), max_download_bytes=5
    )
    with pytest.raises(ValueError, match="size limit"):
        await releases.download_release_logs(1)
    assert list((tmp_path / "downloads").iterdir()) == []
    _, builds, *_ = domains(
        lambda request: httpx.Response(
            200,
            json={"resource": {"type": "PipelineArtifact", "downloadUrl": "https://evil.example"}},
        )
    )
    with pytest.raises(ValueError, match="Unsupported"):
        await builds.download_pipeline_artifact(1, "artifact")


async def test_status_and_policy_pages_use_supported_parameters(domains):
    def handler(request):
        if request.url.path.endswith("/pullrequests/12"):
            return httpx.Response(200, json={"pullRequestId": 12, "repository": REPO})
        path, q = request.url.path, request.url.params
        if path.endswith("/repositories/repo"):
            return httpx.Response(200, json=REPO)
        assert q["api-version"] == "6.0-preview.1"
        if path.endswith("/statuses"):
            assert "$top" not in q and "$skip" not in q
            return httpx.Response(200, json={"value": [{"id": 1}, {"id": 2}]})
        assert path.endswith("/policy/evaluations")
        assert q["artifactId"] == "vstfs:///CodeReview/CodeReviewId/project-id/12"
        return httpx.Response(200, json={"value": []})

    _, _, _, _, prs, _ = domains(handler)
    first = await prs.get_pull_request_statuses("repo", 12, limit=1)
    second = await prs.get_pull_request_statuses(
        "repo", 12, limit=1, continuation_token=first["continuation_token"]
    )
    third = await prs.get_pull_request_statuses(
        "repo", 12, limit=1, continuation_token=second["continuation_token"]
    )
    assert [i["id"] for i in first["items"] + second["items"]] == [1, 2]
    assert third["source"] == "policies" and not third["has_more"]


async def test_unavailable_checks_stay_incomplete_across_sources(domains):
    def handler(request):
        if request.url.path.endswith("/repositories/repo"):
            return httpx.Response(200, json=REPO)
        if request.url.path.endswith("/statuses"):
            return httpx.Response(404)
        return httpx.Response(200, json={"value": []})

    _, _, _, _, prs, _ = domains(handler)
    first = await prs.get_pull_request_statuses("repo", 1)
    assert first["incomplete"] and first["unavailable"]
    last = await prs.get_pull_request_statuses(
        "repo", 1, continuation_token=first["continuation_token"]
    )
    assert last["incomplete"] and not last["has_more"]


async def test_commit_pagination_pins_branch_and_filters(domains):
    metadata_calls = 0

    def handler(request):
        nonlocal metadata_calls
        path, q = request.url.path, request.url.params
        if path.endswith("/repositories/repo"):
            return httpx.Response(200, json=REPO)
        if path.endswith("/items"):
            metadata_calls += 1
            assert q["versionDescriptor.versionType"] == "tag"
            return httpx.Response(200, json={"commitId": SHA})
        if path.endswith("/commits"):
            assert q["searchCriteria.itemVersion.version"] == SHA
            assert q["searchCriteria.itemVersion.versionType"] == "commit"
            assert q["searchCriteria.fromDate"] == "2026-01-01T00:00:00Z"
            assert q["searchCriteria.itemPath"] == "/src"
            return httpx.Response(
                200, json={"value": [{"commitId": SHA}] if q["$skip"] == "0" else []}
            )
        if path.endswith("/changes"):
            assert "top" in q and "skip" in q and "$top" not in q
            return httpx.Response(200, json={"changes": [{"changeType": "edit"}]})
        return httpx.Response(200, json={"commitId": SHA})

    _, _, _, git, _, _ = domains(handler)
    kwargs = dict(
        repository="repo",
        version="v1",
        version_type="tag",
        since="2026-01-01",
        path="/src",
        limit=1,
    )
    first = await git.list_repository_commits(**kwargs)
    last = await git.list_repository_commits(
        **kwargs, continuation_token=first["continuation_token"]
    )
    assert not last["has_more"] and metadata_calls == 1
    commit = await git.get_repository_commit("repo", SHA)
    assert commit["items"] == [{"changeType": "edit"}]


async def test_repository_refs_threads_and_details(domains):
    def handler(request):
        path = request.url.path
        if path.endswith("/repositories/repo"):
            return httpx.Response(200, json=REPO)
        if path.endswith("/repositories"):
            return httpx.Response(200, json={"value": [REPO]})
        if path.endswith("/refs"):
            q = request.url.params
            assert q["filter"] == "tags/" and q["filterContains"] == "v1"
            return httpx.Response(
                200,
                json={"value": [{"name": "refs/tags/v1"}]},
                headers={"x-ms-continuationtoken": "next"},
            )
        if path.endswith("/threads"):
            return httpx.Response(
                200,
                json={
                    "value": [{"id": 2, "status": "active", "comments": [{"content": "untrusted"}]}]
                },
            )
        return httpx.Response(200, json={"pullRequestId": 12, "repository": REPO})

    _, _, _, git, prs, _ = domains(handler)
    assert len((await git.list_repositories())["items"]) == 1
    assert (await git.list_repository_refs("repo", prefix="tags/", contains="v1"))["has_more"]
    assert (await prs.get_pull_request("repo", 12))["pullRequestId"] == 12
    assert (await prs.get_pull_request_threads("repo", 12))["items"][0]["comments"][0][
        "content"
    ] == "untrusted"


async def test_build_artifact_download_ignores_response_url(domains):
    payload = zip_bytes()

    def handler(request):
        assert request.url.host == "ado.example"
        assert request.url.path.endswith("/build/builds/1/artifacts")
        assert request.url.params["artifactName"] == "drop"
        if request.url.params.get("$format") == "zip":
            return httpx.Response(200, content=payload)
        return httpx.Response(
            200, json={"resource": {"type": "Container", "downloadUrl": "https://evil.example"}}
        )

    _, builds, *_ = domains(handler)
    assert (await builds.download_pipeline_artifact(1, "drop"))["size"] == len(payload)


async def test_git_file_and_directory_downloads(domains):
    payload = zip_bytes()

    def handler(request):
        path, q = request.url.path, request.url.params
        if path.endswith("/repositories/repo"):
            return httpx.Response(200, json=REPO)
        if q.get("$format") == "zip":
            assert q["scopePath"] == "/src" and q["recursionLevel"] == "Full"
            assert q["versionDescriptor.versionType"] == "commit"
            return httpx.Response(200, content=payload)
        if q.get("download") == "true":
            assert q["versionDescriptor.version"] == SHA
            return httpx.Response(200, content=b"hello")
        return httpx.Response(
            200, json={"commitId": SHA, "gitObjectType": "tree" if q["path"] == "/src" else "blob"}
        )

    _, _, _, git, _, _ = domains(handler)
    assert (await git.download_repository_archive("repo", "main", directory="/src"))[
        "commit"
    ] == SHA
    assert (await git.download_repository_file("repo", "/x", "main"))["size"] == 5


async def test_release_artifacts_and_secret_redaction(domains):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": 1,
                "variables": {"private": {"isSecret": True, "value": "secret"}},
                "artifacts": [
                    {
                        "type": "Build",
                        "definitionReference": {
                            "version": {"id": "42"},
                            "project": {"id": "other", "name": "Other"},
                        },
                    }
                ],
            },
        )

    _, _, releases, *_ = domains(handler)
    release = await releases.get_release(1)
    assert "secret" not in str(release)
    artifacts = await releases.list_release_artifacts(1)
    assert artifacts["items"][0]["definitionReference"]["version"]["id"] == "42"


@pytest.mark.parametrize(
    "payload,binary,lfs", [(b"\x00\xff", True, False), (b"first\nsecond\n", False, False)]
)
async def test_file_text_limits_and_binary(domains, payload, binary, lfs):
    def handler(request):
        if request.url.path.endswith("/repositories/repo"):
            return httpx.Response(200, json=REPO)
        if "download" not in request.url.params:
            return httpx.Response(200, json={"commitId": SHA})
        return httpx.Response(200, content=payload)

    _, _, _, git, _, _ = domains(handler)
    result = await git.get_repository_file("repo", "/x", "main", max_bytes=6, line_count=1)
    assert result["binary"] == binary and result["lfs_pointer"] == lfs
    if not binary:
        assert result["truncated"] and result["byte_limit_reached"] and result["text"] == "first\n"


async def test_download_interruption_cleans_partial_and_logs_no_secrets(domains, tmp_path, caplog):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 65536
            raise httpx.ReadError("PAT=test-pat signedUrl=secret")

    _, _, releases, *_ = domains(lambda request: httpx.Response(200, stream=BrokenStream()))
    with pytest.raises(AzureDevOpsError):
        await releases.download_release_logs(1)
    assert list((tmp_path / "downloads").iterdir()) == []
    assert "test-pat" not in caplog.text and "signedUrl" not in caplog.text


async def test_json_limit_redirects_and_invalid_json(domains):
    for response, match in [
        (httpx.Response(200, content=b"x" * (8 * 1024 * 1024 + 1)), "safety limit"),
        (httpx.Response(302, headers={"location": "https://evil.example"}), "302"),
        (httpx.Response(200, text="secret"), "Invalid JSON"),
    ]:
        api, builds, *_ = domains(lambda request, response=response: response)
        with pytest.raises(AzureDevOpsError, match=match):
            await builds.get_pipeline_run(1)


def test_download_configuration_is_local_and_validated(tmp_path, monkeypatch):
    import os

    for key in os.environ:
        if key.startswith(("ADO_", "AZURE_DEVOPS_")):
            monkeypatch.delenv(key)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "AZURE_DEVOPS_URL=https://ado.example/c\nAZURE_DEVOPS_PAT=fake\n"
        "ADO_DOWNLOAD_DIR=files\nADO_MAX_DOWNLOAD_BYTES=123\n"
        "ADO_DOWNLOAD_HOSTS=one.example,two.example\nADO_RELEASE_URL=https://release.example/c\n"
    )
    settings = load_settings(dotenv)
    assert settings.download_dir == tmp_path / "files" and settings.max_download_bytes == 123
    assert settings.download_hosts == ("one.example", "two.example")
    assert settings.release_base_url == "https://release.example/c"
    monkeypatch.setenv("ADO_MAX_DOWNLOAD_BYTES", "secret")
    with pytest.raises(ConfigurationError, match="ADO_MAX_DOWNLOAD_BYTES"):
        load_settings(dotenv)
