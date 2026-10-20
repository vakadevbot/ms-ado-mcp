import httpx
import pytest

from ms_ado_mcp.client import AzureDevOpsClient
from ms_ado_mcp.config import Settings
from ms_ado_mcp.policy import ProjectPolicy, Rule
from ms_ado_mcp.service import WorkItemService


@pytest.fixture
def settings():
    return Settings(url="https://ado.example/collection", pat="test-pat", project="Demo Project")


@pytest.fixture
def policy():
    return ProjectPolicy(
        name="Demo Project",
        rules=(Rule("test.allow", "Test-only permissive rule.", lambda item, ctx: []),),
    )


@pytest.fixture
async def make_service(settings, policy):
    clients = []

    def factory(handler, project_policy=None):
        selected = project_policy or policy
        client = AzureDevOpsClient(settings, transport=httpx.MockTransport(handler))
        clients.append(client)
        return WorkItemService(client, {selected.name: selected}, selected.name)

    yield factory
    for client in clients:
        await client.close()


def item(item_id=1, kind="Task", **extra):
    return {
        "id": item_id,
        "rev": 1,
        "fields": {
            "System.Title": f"Work item {item_id}",
            "System.WorkItemType": kind,
            "System.TeamProject": "Demo Project",
            **extra,
        },
    }
