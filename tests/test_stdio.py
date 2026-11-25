import asyncio
import json
import os
import sys
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ms_ado_mcp.read_tools import READ_TOOL_NAMES


async def test_real_stdio_handshake_tools_prompt_resource_and_validation(tmp_path):
    # Private fixture is created in a temporary directory; no live Azure access is needed.
    rules = tmp_path / "rules.py"
    rules.write_text(
        "import asyncio\n"
        "from ms_ado_mcp.models import Violation\n"
        "from ms_ado_mcp.policy import BoardScope, ProjectPolicy, Rule\n"
        "async def check(item, ctx):\n"
        "    print('checker started')\n"
        "    await asyncio.sleep(0.01)\n"
        "    print('checker finished')\n"
        "    return [Violation(code='demo.owner', message='Owner required')]\n"
        "PROJECTS = [ProjectPolicy(name='Demo', "
        "write_scope=BoardScope('Demo\\\\Team', 'Demo\\\\Sprint'), rules=(\n"
        "    Rule('demo.owner', 'Owner must be filled.', check),\n"
        "))]\n",
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "AZURE_DEVOPS_PAT=stdio-fake-pat\n",
    )
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "default_organization": "primary",
                "organizations": [
                    {
                        "name": "primary",
                        "url": "https://example.invalid/collection",
                        "projects": ["Demo", "Other"],
                        "rules_file": "rules.py",
                    },
                    {
                        "name": "secondary",
                        "url": "https://example.invalid/second",
                        "projects": ["Demo"],
                    },
                ],
                "allowed_repositories": ["primary/Demo/one", "primary/Other/two"],
            }
        )
    )
    # Override any developer's real connection settings in the child.
    env = {**os.environ}
    for key in list(env):
        if key.startswith(("AZURE_DEVOPS_", "ADO_")):
            del env[key]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ms_ado_mcp", "--config-file", str(config)],
        cwd=str(tmp_path.parent),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=15)
        ) as session:
            init = await session.initialize()
            assert "Owner must be filled." in init.instructions
            assert "stdio-fake-pat" not in init.model_dump_json()
            catalog = await session.list_tools()
            tools = {t.name: t for t in catalog.tools}
            assert set(tools) == set(READ_TOOL_NAMES) | {
                "get_project_rules",
                "get_access_scope",
                "get_current_user",
                "search_my_work_items",
                "list_my_active_work_items",
                "create_my_work_item",
                "search_work_items",
                "get_work_item",
                "get_work_item_hierarchy",
                "create_user_story",
                "create_task",
                "update_work_item",
                "validate_work_item",
                "validate_my_active_work_items",
                "validate_my_work_items",
            }
            for name in READ_TOOL_NAMES:
                assert tools[name].annotations.readOnlyHint is True
                assert tools[name].annotations.destructiveHint is False
                assert "url" not in tools[name].inputSchema["properties"]
                assert "output_path" not in tools[name].inputSchema["properties"]
                assert "organization" in tools[name].inputSchema["properties"]
            assert tools["get_access_scope"].annotations.readOnlyHint is True
            scope = await session.call_tool("get_access_scope", {})
            assert not scope.isError
            assert json.loads(scope.content[0].text)["default_organization"] == "primary"
            assert json.loads(scope.content[0].text)["organizations"][0]["projects"] == [
                {"name": "Demo", "repositories": ["one"], "repository_paths": ["primary/Demo/one"]},
                {
                    "name": "Other",
                    "repositories": ["two"],
                    "repository_paths": ["primary/Other/two"],
                },
            ]
            denied = await session.call_tool("list_repositories", {"organization": "unknown"})
            assert denied.isError
            empty = await session.call_tool("list_repositories", {"organization": "secondary"})
            assert not empty.isError and json.loads(empty.content[0].text)["items"] == []
            assert tools["create_task"].annotations.idempotentHint is False
            for name in (
                "create_task",
                "create_user_story",
                "create_my_work_item",
                "update_work_item",
            ):
                assert "project" not in tools[name].inputSchema["properties"]
                assert "organization" not in tools[name].inputSchema["properties"]
                assert "board" not in tools[name].inputSchema["properties"]
            assert tools["create_my_work_item"].annotations.idempotentHint is False
            assert tools["search_my_work_items"].annotations.readOnlyHint is True
            assert tools["list_my_active_work_items"].annotations.readOnlyHint is True
            assert tools["get_current_user"].annotations.readOnlyHint is True
            assert tools["validate_work_item"].annotations.readOnlyHint is True
            assert tools["validate_my_active_work_items"].annotations.readOnlyHint is True
            assert tools["validate_my_work_items"].annotations.readOnlyHint is True
            assert (
                tools["list_my_active_work_items"].inputSchema["properties"]["limit"]["maximum"]
                == 200
            )
            result = await session.call_tool("get_project_rules", {})
            assert not result.isError
            prompt = await session.get_prompt(
                "prepare_work_item", {"kind": "task", "project": "Demo"}
            )
            assert "Owner must be filled." in prompt.messages[0].content.text
            resource = await session.read_resource("ado://rules")
            assert "Owner must be filled." in resource.contents[0].text
            validation = await session.call_tool(
                "validate_work_item",
                {"kind": "task", "title": "Test task"},
            )
            assert not validation.isError
            assert validation.structuredContent["valid"] is False
            result = await session.call_tool("create_task", {"title": "Test task"})
            assert not result.isError
            assert result.structuredContent["created"] is False
            concurrent = await asyncio.gather(
                *(
                    session.call_tool("validate_work_item", {"kind": "task", "title": f"Task {n}"})
                    for n in range(3)
                )
            )
            assert all(not result.isError for result in concurrent)
            assert all(result.structuredContent["valid"] is False for result in concurrent)
            bad_id = await session.call_tool("get_work_item", {"item_id": -1})
            assert bad_id.isError
