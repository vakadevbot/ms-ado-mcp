import json
from dataclasses import replace

import httpx
import pytest

from ms_ado_mcp.client import CHILD, PARENT
from ms_ado_mcp.models import Violation
from ms_ado_mcp.policy import Rule
from ms_ado_mcp.service import literal

from .conftest import item


async def test_active_categories_custom_states_and_pagination(make_service):
    requests = []
    snapshot = "2026-10-01T12:00:00Z"

    def handler(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/states"):
            assert request.url.params["api-version"] == "6.1-preview.1"
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"name": "В анализе", "category": "InProgress"},
                        {"name": "В работе", "category": "InProgress"},
                        {"name": "Закрыто", "category": "Completed"},
                    ]
                },
            )
        body = json.loads(request.content)
        if path.endswith("/wiql"):
            assert request.url.params["api-version"] == "6.1-preview.2"
            assert request.url.params["$top"] == "3"
            query = body["query"]
            assert "[System.TeamProject] = @project" in query
            assert "[System.State] IN ('В анализе', 'В работе')" in query
            assert "Закрыто" not in query
            assert "[System.AssignedTo] = @Me" in query
            assert query.endswith("ORDER BY [System.Id] ASC")
            return httpx.Response(
                200,
                json={
                    "workItems": [{"id": n} for n in [4, 7, 9]],
                    "asOf": snapshot,
                },
            )
        assert body["ids"] == [4, 7]
        assert body["asOf"] == snapshot
        return httpx.Response(200, json={"value": [item(4), item(7)]})

    service = make_service(handler)
    result = await service.search(
        active_only=True,
        kinds=["task"],
        assigned_to="@Me",
        after_id=3,
        limit=2,
    )
    assert [x.id for x in result.items] == [4, 7]
    assert result.next_after_id == 7
    assert result.as_of == snapshot
    assert len(requests) == 3


async def test_explicit_active_states_skip_metadata(make_service, policy):
    seen = []
    configured = replace(policy, active_states={"pi_goal": ("Analysis",)})

    def handler(request):
        seen.append(request)
        query = json.loads(request.content)["query"]
        assert "'PI Goal'" in query and "'Analysis'" in query
        return httpx.Response(200, json={"workItems": [], "asOf": "2026-01-01T00:00:00Z"})

    page = await make_service(handler, configured).search(active_only=True, kinds=["pi_goal"])
    assert page.items == []
    assert len(seen) == 1


async def test_search_title_escaping_scope_and_cursor(make_service):
    def handler(request):
        query = json.loads(request.content)["query"]
        assert """[System.Title] CONTAINS "O'Brien" """ in query
        assert "[System.Id] > 4" in query
        assert "[System.AreaPath] UNDER 'Demo Project\\Team'" in query
        assert "[System.State]" not in query
        assert "ASOF '2026-01-01T00:00:00Z'" in query
        return httpx.Response(200, json={"workItems": []})

    service = make_service(handler)
    result = await service.search(
        text="O'Brien",
        after_id=4,
        area_path="Demo Project\\Team",
        as_of="2026-01-01T00:00:00Z",
    )
    assert result.next_after_id is None
    with pytest.raises(ValueError, match="both"):
        literal("""' OR "x" = "x" """)


async def test_numeric_search_and_unknown_project(make_service):
    calls = []

    def handler(request):
        calls.append(request)
        assert "[System.Id] = 42" in json.loads(request.content)["query"]
        return httpx.Response(200, json={"workItems": []})

    service = make_service(handler)
    await service.search(text="42")
    with pytest.raises(ValueError, match="configured project"):
        await service.search(project="Unapproved", text="42")
    assert len(calls) == 1


async def test_hierarchy_and_existing_validation_receive_linked_fields(make_service, policy):
    contexts = []

    def checker(work_item, context):
        contexts.append(context)
        assert context.parents[0].fields["System.Title"] == "Work item 2"
        assert context.children[0].id == 3
        return []

    policy = replace(policy, rules=(Rule("links", "Check linked items", checker),))

    def handler(request):
        if request.method == "GET":
            assert request.url.params["$expand"] == "Relations"
            return httpx.Response(
                200,
                json={
                    **item(),
                    "relations": [
                        {
                            "rel": PARENT,
                            "url": "https://ado.example/collection/_apis/wit/workItems/2",
                        },
                        {
                            "rel": CHILD,
                            "url": "https://ado.example/collection/_apis/wit/workItems/3",
                        },
                        {"rel": "Hyperlink", "url": "https://example.org"},
                    ],
                },
            )
        ids = json.loads(request.content)["ids"]
        return httpx.Response(200, json={"value": [item(i) for i in ids]})

    service = make_service(handler, policy)
    hierarchy = await service.hierarchy(1)
    assert [x.id for x in hierarchy.parents] == [2]
    assert [x.id for x in hierarchy.children] == [3]
    report = await service.validate_item(item_id=1)
    assert report.valid and report.scope == "existing"
    assert contexts[0].scope == "existing"
    with pytest.raises(ValueError, match="never both"):
        await service.validate_item(item_id=1, title="Replacement")


async def test_invalid_creation_never_writes_and_matches_validation(make_service, policy):
    rule = Rule(
        "required.owner",
        "Owner is required.",
        lambda item, ctx: [Violation(code="required.owner", message="Missing owner")],
    )
    policy = replace(policy, rules=(rule,))

    def unexpected(request):
        pytest.fail("Invalid draft must never make an HTTP request")

    service = make_service(unexpected, policy)
    report = await service.validate_item(kind="task", title="Task")
    result = await service.create("task", "Task")
    assert not result.created
    assert result.validation == report
    assert result.validation.issues[0].code == "required.owner"


async def test_create_sends_validated_defaults_and_atomic_parent_link(make_service, policy):
    checked = []
    calls = []

    def checker(draft, context):
        checked.append(True)
        assert draft.fields["Custom.Team"] == "Backend"
        assert draft.fields["Microsoft.VSTS.Scheduling.OriginalEstimate"] == 4
        assert context.parents[0].id == 10
        return []

    policy = replace(
        policy,
        rules=(Rule("shape", "Required team, estimate and parent", checker),),
        defaults={"task": {"Custom.Team": "Backend"}},
    )

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/workitemsbatch"):
            return httpx.Response(200, json={"value": [item(10, kind="User Story")]})
        assert checked == [True]
        assert request.url.path.endswith("/workitems/$Task")
        assert request.url.params["api-version"] == "6.1-preview.3"
        assert "bypassRules" not in request.url.params
        assert request.headers["content-type"] == "application/json-patch+json"
        operations = json.loads(request.content)
        values = {x["path"]: x["value"] for x in operations}
        assert values["/fields/System.Title"] == "Task"
        assert values["/fields/Custom.Team"] == "Backend"
        assert "/fields/System.TeamProject" not in values
        assert values["/relations/-"] == {
            "rel": PARENT,
            "url": "https://ado.example/collection/Demo%20Project/_apis/wit/workitems/10",
        }
        return httpx.Response(200, json=item(42))

    result = await make_service(handler, policy).create(
        "task",
        "Task",
        "<p>Description</p>",
        fields={"Microsoft.VSTS.Scheduling.OriginalEstimate": 4},
        parent_id=10,
    )
    assert result.created and result.item.id == 42
    assert len(calls) == 2


@pytest.mark.parametrize(
    "name",
    [
        "System.WorkItemType",
        "System.Parent",
        "System.TeamProject",
        "System.Title",
        "system.title",
        "Custom.X/relations/-",
    ],
)
async def test_fields_cannot_override_reserved_arguments(make_service, name):
    def unexpected(request):
        pytest.fail("Reserved fields must fail before HTTP")

    with pytest.raises(ValueError, match="reserved"):
        await make_service(unexpected).create("task", "Task", fields={name: "bypass"})


@pytest.mark.parametrize(
    "rules",
    [
        (),
        (Rule("none", "Broken checker", lambda i, c: None),),
        (Rule("wrong-kind", "Story-only checker", lambda i, c: [], kinds=("user_story",)),),
    ],
)
async def test_missing_or_malformed_rules_fail_closed(make_service, policy, rules):
    policy = replace(policy, rules=rules)
    result = await make_service(
        lambda request: pytest.fail("Must not write"),
        policy,
    ).create("task", "Task")
    assert not result.created and not result.validation.valid
