import base64
import json

import httpx
import pytest

from ms_ado_mcp.client import AzureDevOpsClient, AzureDevOpsError
from ms_ado_mcp.models import WorkItem

from .conftest import item


async def test_auth_version_batch_chunking_and_order(settings):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["api-version"] == "6.1-preview.1"
        assert request.headers["authorization"] == (
            "Basic " + base64.b64encode(b":test-pat").decode()
        )
        body = json.loads(request.content)
        assert len(body["ids"]) <= 200
        assert body["asOf"] == "2026-01-01T00:00:00Z"
        return httpx.Response(200, json={"value": [item(i) for i in reversed(body["ids"])]})

    client = AzureDevOpsClient(settings, httpx.MockTransport(handler))
    result = await client.get_items(
        "Demo Project",
        list(range(1, 402)),
        as_of="2026-01-01T00:00:00Z",
    )
    assert [x.id for x in result] == list(range(1, 402))
    assert len(calls) == 3
    await client.close()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 302])
async def test_http_errors_do_not_echo_body_or_redirect(settings, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="test-pat confidential-data",
            headers={"Location": "https://untrusted.example/"},
        )

    client = AzureDevOpsClient(settings, httpx.MockTransport(handler))
    with pytest.raises(AzureDevOpsError) as error:
        await client.get_item("Demo Project", 1)
    assert str(status) in str(error.value)
    assert "test-pat" not in str(error.value)
    assert "confidential-data" not in str(error.value)
    assert len(calls) == 1
    await client.close()


async def test_creation_timeout_is_not_retried(settings):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("test-pat", request=request)

    client = AzureDevOpsClient(settings, httpx.MockTransport(handler))
    with pytest.raises(AzureDevOpsError, match="may have succeeded") as error:
        await client.create("Demo Project", "Task", WorkItem.model_validate(item()))
    assert "test-pat" not in str(error.value)
    assert len(calls) == 1
    await client.close()


async def test_project_isolation_and_incomplete_batch(settings):
    def handler(request):
        body = json.loads(request.content)
        if body["ids"] == [1]:
            return httpx.Response(
                200,
                json={
                    "value": [item(1, **{"System.TeamProject": "Other"})],
                },
            )
        return httpx.Response(200, json={"value": []})

    client = AzureDevOpsClient(settings, httpx.MockTransport(handler))
    with pytest.raises(AzureDevOpsError, match="another project"):
        await client.get_items("Demo Project", [1])
    with pytest.raises(AzureDevOpsError, match="incomplete"):
        await client.get_items("Demo Project", [2])
    await client.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/collection/_apis/wit/workItems/2",
        "https://ado.example/other/_apis/wit/workItems/2",
        "https://ado.example/collection/_apis/wit/workItems/2?token=foo",
        "https://ado.example/collection/_apis/wit/workItems/-2",
    ],
)
async def test_untrusted_relation_urls_are_rejected(settings, url):
    client = AzureDevOpsClient(settings, httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(AzureDevOpsError):
        client.relation_id(url)
    assert client.relation_id("https://ado.example/collection/_apis/wit/workItems/2") == 2
    await client.close()
