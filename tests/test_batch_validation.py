import json
from dataclasses import replace

import httpx
import pytest

from ms_ado_mcp.models import Violation
from ms_ado_mcp.policy import Rule
from ms_ado_mcp.service import creation_timestamp

from .conftest import item


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"created_before": "2026-09-01"},
        {"iteration_path": ""},
        {"iteration_path": "Demo Project"},
        {"iteration_path": "Other\\Sprint"},
        {"iteration_path": "Demo Project\\.."},
        {"created_since": ""},
        {"created_since": "2026-08-01' OR 1=1"},
        {"created_since": "2026-08-01T12:00:00"},
        {"created_since": "2026-09-01", "created_before": "2026-08-01"},
        {"created_since": "2026-08-01", "created_before": "2026-08-01"},
        {"created_since": "2026-08-01", "max_items": 0},
        {"created_since": "2026-08-01", "max_items": 1001},
    ],
)
async def test_unbounded_or_invalid_history_audit_rejected_before_http(make_service, arguments):
    service = make_service(lambda r: pytest.fail("Must not query Azure"))
    with pytest.raises(ValueError):
        await service.validate_my_items(active_only=False, **arguments)


@pytest.mark.parametrize("active_only", [True, False])
async def test_audit_uses_individual_validation_and_reports_errors(
    make_service, policy, active_only
):
    checked = []
    calls = []

    def check(work_item, context):
        checked.append(work_item.id)
        assert context.scope == "existing"
        if work_item.id == 2:
            return [Violation(code="description", message="Description required")]
        return []

    policy = replace(
        policy,
        active_states={"task": ("Doing",)},
        rules=(Rule("required", "Description required", check),),
    )

    def handler(request):
        calls.append(request)
        assert request.method in {"GET", "POST"}  # WIQL and batch POSTs are reads.
        if request.url.path.endswith("/wiql"):
            query = json.loads(request.content)["query"]
            assert "[System.AssignedTo] = @Me" in query
            assert "[System.IterationPath] UNDER 'Demo Project\\PI 5'" in query
            assert "[System.CreatedDate] >= '2026-07-31T21:00:00Z'" in query
            assert "[System.CreatedDate] < '2026-09-01T00:00:00Z'" in query
            assert ("[System.State] IN ('Doing')" in query) == active_only
            return httpx.Response(
                200,
                json={"workItems": [{"id": i} for i in (1, 2, 3)], "asOf": "2026-09-01T00:00:00Z"},
            )
        if request.url.path.endswith("/workitemsbatch"):
            return httpx.Response(200, json={"value": [item(i) for i in (1, 2, 3)]})
        if request.url.path.endswith("/3"):
            return httpx.Response(403, text="private-secret")
        return httpx.Response(200, json=item(int(request.url.path.rsplit("/", 1)[1])))

    service = make_service(handler, policy)
    result = await service.validate_my_items(
        active_only=active_only,
        kinds=["task"],
        iteration_path="Demo Project\\PI 5",
        created_since="2026-08-01T00:00:00+03:00",
        created_before="2026-09-01",
    )
    assert result.complete and result.next_after_id is None
    assert result.selected_count == 3
    assert (result.valid_count, result.invalid_count, result.error_count) == (1, 1, 1)
    assert [r.work_item_id for r in result.results] == [1, 2, 3]
    assert result.results[1].validation.issues[0].code == "description"
    assert "403" in result.results[2].error
    assert "private-secret" not in result.model_dump_json()
    assert sorted(checked) == [1, 2]
    direct = await service.validate_item(item_id=2)
    assert result.results[1].validation == direct
    assert not any(r.method == "PATCH" or "/workitems/$" in r.url.path for r in calls)


async def test_all_states_audit_requires_no_write_scope_and_keeps_missing_rules_invalid(
    make_service, policy
):
    policy = replace(policy, write_scope=None, rules=())

    def handler(request):
        if request.url.path.endswith("/wiql"):
            query = json.loads(request.content)["query"]
            assert "[System.State]" not in query
            assert "[System.CreatedDate] >= '2026-08-01T00:00:00Z'" in query
            return httpx.Response(200, json={"workItems": [{"id": 1}]})
        if request.url.path.endswith("/workitemsbatch"):
            return httpx.Response(200, json={"value": [item()]})
        return httpx.Response(200, json=item())

    result = await make_service(handler, policy).validate_my_items(
        active_only=False,
        created_since="2026-08-01",
    )
    assert result.invalid_count == 1
    assert result.error_count == 0
    assert any(v.code == "policy.missing" for v in result.results[0].validation.issues)


async def test_pagination_processes_more_than_one_page_and_returns_resume_cursor(
    make_service, policy
):
    snapshot = "2026-09-01T00:00:00Z"
    offsets = []

    def handler(request):
        if request.url.path.endswith("/wiql"):
            query = json.loads(request.content)["query"]
            offset = int(query.split("[System.Id] > ")[1].split()[0])
            offsets.append(offset)
            if offset:
                assert f"ASOF '{snapshot}'" in query
            ids = list(range(offset + 1, min(206, offset + int(request.url.params["$top"])) + 1))
            return httpx.Response(
                200, json={"workItems": [{"id": i} for i in ids], "asOf": snapshot}
            )
        if request.url.path.endswith("/workitemsbatch"):
            ids = json.loads(request.content)["ids"]
            return httpx.Response(200, json={"value": [item(i) for i in ids]})
        return httpx.Response(200, json=item(int(request.url.path.rsplit("/", 1)[1])))

    service = make_service(handler, policy)
    first = await service.validate_my_items(
        active_only=False, created_since="2026-08-01", max_items=205
    )
    assert offsets == [0, 200]
    assert first.selected_count == first.valid_count == 205
    assert not first.complete and first.next_after_id == 205
    assert [r.work_item_id for r in first.results] == list(range(1, 206))
    remaining = await service.validate_my_items(
        active_only=False,
        created_since="2026-08-01",
        max_items=205,
        after_id=first.next_after_id,
        as_of=first.as_of,
    )
    assert remaining.complete and remaining.next_after_id is None
    assert [r.work_item_id for r in remaining.results] == [206]


async def test_no_active_items_returns_zero_counts(make_service, policy):
    policy = replace(policy, active_states={"task": ()})
    result = await make_service(
        lambda r: pytest.fail("No matching active types"), policy
    ).validate_my_items(
        active_only=True,
        kinds=["task"],
    )
    assert result.complete
    assert (
        result.selected_count,
        result.valid_count,
        result.invalid_count,
        result.error_count,
    ) == (0, 0, 0, 0)
    assert result.results == []


async def test_arbitrary_per_item_exception_is_not_exposed(make_service, monkeypatch):
    def handler(request):
        if request.url.path.endswith("/wiql"):
            return httpx.Response(200, json={"workItems": [{"id": 1}]})
        return httpx.Response(200, json={"value": [item()]})

    service = make_service(handler)

    async def fail(**kwargs):
        raise RuntimeError("private-secret")

    monkeypatch.setattr(service, "validate_item", fail)
    result = await service.validate_my_items(
        active_only=False, iteration_path="Demo Project\\Sprint 2"
    )
    assert result.error_count == 1 and result.valid_count == 0
    assert "private-secret" not in result.model_dump_json()


def test_date_boundaries_are_utc_and_valid():
    assert creation_timestamp("2026-08-01").isoformat() == "2026-08-01T00:00:00+00:00"
    assert creation_timestamp("2026-08-01T03:00:00+03:00") == creation_timestamp("2026-08-01")
