import httpx
import pytest

from ms_ado_mcp.client import AzureDevOpsClient
from ms_ado_mcp.config import Settings
from ms_ado_mcp.policy import BoardScope, ProjectPolicy, Rule
from ms_ado_mcp.service import WorkItemService

AUTHOR_ID = "00000000-0000-0000-0000-000000000001"


def authenticated_user():
    return {
        "authenticatedUser": {
            "id": AUTHOR_ID,
            "providerDisplayName": "Me",
            "uniqueName": "me@example.org",
        }
    }


@pytest.fixture
def settings():
    return Settings(url="https://ado.example/collection", pat="test-pat", project="Demo Project")


@pytest.fixture
def policy():
    return ProjectPolicy(
        name="Demo Project",
        write_scope=BoardScope(area_path="Demo Project\\Team"),
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
            "System.AreaPath": "Demo Project\\Team",
            "System.CreatedBy": {"id": AUTHOR_ID, "uniqueName": "me@example.org"},
            **extra,
        },
    }
